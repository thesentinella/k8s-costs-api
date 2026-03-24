"""
Shared cost engine — pure calculation functions.
Each provider function loads its pricing data and calls these helpers.
All monetary values are USD.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Pricing data containers (populated by each provider function)
# ---------------------------------------------------------------------------

@dataclass
class InstancePrice:
    """On-demand hourly price for a single instance."""
    instance_type: str
    vcpu: int
    memory_gb: float
    price_usd_per_hour: float


@dataclass
class StoragePricing:
    ssd_per_gb_month: float
    hdd_per_gb_month: float
    snapshot_per_gb_month: float
    premium_ssd_per_gb_month: Optional[float] = None  # Azure P-series


@dataclass
class NetworkPricing:
    egress_per_gb: float               # internet egress (first/only tier)
    inter_az_per_gb: float             # cross-AZ transfer
    load_balancer_per_hour: float      # per managed LB


@dataclass
class ControlPlanePricing:
    """
    Cloud-managed control plane fees.
    EKS: $0.10/hr per cluster
    GKE: free for 1 zonal cluster, $0.10/hr for regional / additional
    AKS: free control plane
    On-prem: no control plane fee (included in platform licensing)
    """
    hourly_fee: float = 0.0
    free_tiers: int = 0     # number of free clusters (GKE = 1 zonal)


@dataclass
class SupportPricing:
    """
    Monthly floor or percentage of monthly spend, whichever is higher.
    business_pct applies to spend above a threshold in some providers — simplified here
    to a flat percentage.
    """
    basic_floor_usd: float = 0.0
    basic_pct: float = 0.0
    business_floor_usd: float = 0.0
    business_pct: float = 0.0


# ---------------------------------------------------------------------------
# Core calculation functions
# ---------------------------------------------------------------------------

HOURS_PER_MONTH = 730  # industry standard for monthly estimates


def compute_cost(
    instance_price: InstancePrice,
    node_count: int,
) -> float:
    """Total monthly compute cost across all nodes."""
    return instance_price.price_usd_per_hour * HOURS_PER_MONTH * node_count


def control_plane_cost(
    pricing: ControlPlanePricing,
    cluster_count: int = 1,
) -> float:
    """Monthly fee for managed control plane(s)."""
    billable_clusters = max(0, cluster_count - pricing.free_tiers)
    return pricing.hourly_fee * HOURS_PER_MONTH * billable_clusters


def storage_cost(
    pricing: StoragePricing,
    storage_gb_per_node: float,
    node_count: int,
    snapshot_gb: float,
    storage_class: str,
) -> float:
    """Monthly storage cost: PVs + snapshots."""
    total_pv_gb = storage_gb_per_node * node_count

    if storage_class == "premium_ssd" and pricing.premium_ssd_per_gb_month is not None:
        pv_rate = pricing.premium_ssd_per_gb_month
    elif storage_class in ("ssd", "premium_ssd"):
        pv_rate = pricing.ssd_per_gb_month
    else:
        pv_rate = pricing.hdd_per_gb_month

    pv_cost = total_pv_gb * pv_rate
    snap_cost = snapshot_gb * pricing.snapshot_per_gb_month
    return pv_cost + snap_cost


def networking_cost(
    pricing: NetworkPricing,
    egress_gb: float,
    inter_az_gb: float,
    load_balancer_count: int,
) -> float:
    """Monthly networking cost: egress + cross-AZ + load balancers."""
    egress = egress_gb * pricing.egress_per_gb
    inter_az = inter_az_gb * pricing.inter_az_per_gb
    lb = load_balancer_count * pricing.load_balancer_per_hour * HOURS_PER_MONTH
    return egress + inter_az + lb


def support_cost(
    pricing: SupportPricing,
    monthly_spend: float,
    tier: str,
) -> float:
    """Monthly support plan cost."""
    if tier == "none":
        return 0.0
    if tier == "basic":
        return max(pricing.basic_floor_usd, monthly_spend * pricing.basic_pct)
    if tier == "business":
        return max(pricing.business_floor_usd, monthly_spend * pricing.business_pct)
    return 0.0


# ---------------------------------------------------------------------------
# On-prem TCO helpers
# ---------------------------------------------------------------------------

def hardware_amortization_monthly(
    total_hardware_cost_usd: float,
    amortization_years: int,
) -> float:
    """Straight-line monthly hardware amortization."""
    return total_hardware_cost_usd / (amortization_years * 12)


def labor_monthly(annual_labor_usd: float) -> float:
    return annual_labor_usd / 12


def licensing_monthly(annual_licensing_usd: float) -> float:
    return annual_licensing_usd / 12


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def annual(monthly: float) -> float:
    return round(monthly * 12, 2)


def round2(value: float) -> float:
    return round(value, 2)


def datacenter_overhead_monthly(annual_datacenter_cost_usd: float) -> float:
    """Monthly power, cooling and rack/colocation cost."""
    return annual_datacenter_cost_usd / 12


# ---------------------------------------------------------------------------
# GKE-specific: Sustained Use Discount
# ---------------------------------------------------------------------------

def apply_sustained_use_discount(hourly_price: float, usage_hours: float) -> float:
    """
    Google Compute Engine Sustained Use Discount (SUD).
    Applied automatically when an instance runs for more than 25% of the month.
    Tiers are cumulative — each tier applies only to usage in that band.

    Tier thresholds (fraction of HOURS_PER_MONTH):
      0–25%:   100% of base price (no discount)
      25–50%:  80%  of base price
      50–75%:  60%  of base price
      75–100%: 40%  of base price

    Returns the effective monthly cost after SUD for a single instance.
    Reference: https://cloud.google.com/compute/docs/sustained-use-discounts
    """
    h = HOURS_PER_MONTH  # 730

    tiers = [
        (0.25 * h, 1.00),   # first 25% of month at full price
        (0.25 * h, 0.80),   # next 25% at 80%
        (0.25 * h, 0.60),   # next 25% at 60%
        (0.25 * h, 0.40),   # final 25% at 40%
    ]

    remaining = min(usage_hours, h)
    total_cost = 0.0

    for tier_hours, rate in tiers:
        if remaining <= 0:
            break
        billable = min(remaining, tier_hours)
        total_cost += billable * hourly_price * rate
        remaining -= billable

    return total_cost


def compute_cost_with_sud(
    instance_price: "InstancePrice",
    node_count: int,
    usage_hours: float = None,
) -> float:
    """
    Monthly compute cost with GCP Sustained Use Discount applied.
    usage_hours defaults to HOURS_PER_MONTH (full month = max SUD).
    """
    if usage_hours is None:
        usage_hours = HOURS_PER_MONTH
    per_node = apply_sustained_use_discount(instance_price.price_usd_per_hour, usage_hours)
    return per_node * node_count


# ---------------------------------------------------------------------------
# GKE-specific: tiered egress pricing
# ---------------------------------------------------------------------------

def tiered_egress_cost(gb: float, tiers: list[dict]) -> float:
    """
    Calculate egress cost given a list of price tiers.
    Each tier dict has keys: 'up_to_gb' (None = unlimited) and 'price_per_gb'.
    Tiers must be ordered from smallest to largest threshold.

    Example tiers (GCP internet egress from us-east1):
      [
        {"up_to_gb": 1024,  "price_per_gb": 0.085},
        {"up_to_gb": 10240, "price_per_gb": 0.065},
        {"up_to_gb": None,  "price_per_gb": 0.045},
      ]
    """
    remaining = gb
    total = 0.0
    prev_threshold = 0.0

    for tier in tiers:
        if remaining <= 0:
            break
        cap = tier["up_to_gb"]
        rate = tier["price_per_gb"]
        if cap is None:
            total += remaining * rate
            remaining = 0.0
        else:
            band_gb = cap - prev_threshold
            billable = min(remaining, band_gb)
            total += billable * rate
            remaining -= billable
            prev_threshold = cap

    return total
