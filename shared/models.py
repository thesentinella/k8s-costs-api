"""
Shared input/output schemas for the K8s cost calculator API.
All provider functions accept ClusterInput and return CostResult.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class Provider(str, Enum):
    EKS = "eks"
    GKE = "gke"
    AKS = "aks"
    ONPREM = "onprem"


class StorageClass(str, Enum):
    SSD = "ssd"
    HDD = "hdd"
    PREMIUM_SSD = "premium_ssd"   # Azure-specific, maps to SSD elsewhere


class SupportTier(str, Enum):
    NONE = "none"
    BASIC = "basic"       # AWS Developer / GCP Standard / Azure Developer
    BUSINESS = "business" # AWS Business / GCP Enhanced / Azure Standard


class OnPremPlatform(str, Enum):
    VANILLA  = "vanilla"    # Upstream K8s  -- no licensing cost
    OPENSHIFT = "openshift" # Red Hat OpenShift -- per-core subscription
    RANCHER  = "rancher"    # SUSE Rancher Prime -- per-node subscription
    TANZU    = "tanzu"      # VMware Tanzu -- per-core subscription


# ---------------------------------------------------------------------------
# Input model
# ---------------------------------------------------------------------------

class ClusterInput(BaseModel):
    """
    Unified input accepted by every provider endpoint.
    Fields that don't apply to a provider are silently ignored.
    """
    # Cluster sizing
    node_count: int = Field(..., ge=1, le=10_000, description="Number of worker nodes")
    instance_type: str = Field(..., description="Provider-specific instance/VM type, e.g. 'm5.xlarge'")
    region: str = Field(..., description="Provider region slug, e.g. 'us-east-1'")

    # Storage
    storage_gb_per_node: float = Field(0.0, ge=0, description="Persistent volume storage per node (GB)")
    storage_class: StorageClass = Field(StorageClass.SSD, description="Storage class")
    snapshot_gb: float = Field(0.0, ge=0, description="Total snapshot/backup storage (GB)")

    # Networking
    egress_gb_per_month: float = Field(0.0, ge=0, description="Outbound data transfer per month (GB)")
    inter_az_gb_per_month: float = Field(0.0, ge=0, description="Cross-AZ traffic per month (GB)")
    load_balancer_count: int = Field(0, ge=0, description="Number of managed load balancers")

    # Control plane
    ha_control_plane: bool = Field(True, description="High-availability control plane (where applicable)")
    support_tier: SupportTier = Field(SupportTier.NONE)

    # On-prem only -- hardware
    hardware_amortization_years: int = Field(5, ge=1, le=10)
    hardware_cost_usd: float = Field(0.0, ge=0, description="Total hardware purchase cost (USD)")

    # On-prem only -- datacenter overhead (power, cooling, rack/colo)
    annual_datacenter_cost_usd: float = Field(
        0.0, ge=0,
        description="Annual power, cooling and rack/colocation cost attributed to this cluster"
    )

    # On-prem only -- labor
    annual_labor_cost_usd: float = Field(0.0, ge=0, description="Annual Ops/SRE labor attributed to cluster")

    # On-prem only -- platform licensing
    annual_licensing_cost_usd: float = Field(
        0.0, ge=0,
        description="Override: explicit annual platform licensing cost. "
                    "Set this OR let onprem_platform drive the estimate."
    )
    onprem_platform: Optional[OnPremPlatform] = Field(
        None,
        description="Platform distribution. Drives the licensing estimate when "
                    "annual_licensing_cost_usd is 0."
    )
    node_vcpu_count: int = Field(
        0, ge=0,
        description="vCPUs per node -- required for per-core licensing (OpenShift, Tanzu)"
    )

    @model_validator(mode="after")
    def onprem_fields_required_when_relevant(self) -> "ClusterInput":
        # Validation is provider-agnostic here; provider functions enforce stricter rules.
        return self


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------

class CostBreakdown(BaseModel):
    """Per-dimension cost in USD/month."""
    compute: float = Field(0.0, description="Node/instance compute cost")
    control_plane: float = Field(0.0, description="Managed control plane fee")
    storage: float = Field(0.0, description="Persistent volumes + snapshots")
    networking: float = Field(0.0, description="Egress + cross-AZ + load balancers")
    support: float = Field(0.0, description="Support plan cost")
    licensing: float = Field(0.0, description="Platform licensing (on-prem only)")
    labor: float = Field(0.0, description="Ops labor (on-prem only)")
    datacenter: float = Field(0.0, description="Power, cooling, rack/colo (on-prem only)")


class CostResult(BaseModel):
    """Unified output returned by every provider endpoint."""
    provider: Provider
    region: str
    instance_type: str
    node_count: int

    # Totals
    monthly_total_usd: float
    annual_total_usd: float

    # Dimension breakdown (monthly)
    breakdown: CostBreakdown

    # Metadata
    currency: str = "USD"
    pricing_date: str = Field(..., description="ISO date of the pricing snapshot used")
    notes: list[str] = Field(default_factory=list, description="Human-readable caveats or assumptions")


class CompareResult(BaseModel):
    """Output of the /compare endpoint -- all providers side by side."""
    results: list[CostResult]
    cheapest_provider: Provider
    most_expensive_provider: Provider
    spread_usd_monthly: float = Field(..., description="Difference between cheapest and most expensive (monthly)")
