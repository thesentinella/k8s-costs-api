"""
Netlify serverless function: /api/eks
Calculates the monthly and annual cost of running a Kubernetes cluster on AWS EKS.

Deploy path: netlify/functions/eks.py
Netlify Python runtime uses handler(event, context) -> dict
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — makes `shared/` importable when running inside Netlify
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.models import ClusterInput, CostResult, CostBreakdown, Provider, SupportTier
from shared.cost_engine import (
    InstancePrice,
    StoragePricing,
    NetworkPricing,
    ControlPlanePricing,
    SupportPricing,
    compute_cost,
    control_plane_cost,
    storage_cost,
    networking_cost,
    support_cost,
    annual,
    round2,
)

# ---------------------------------------------------------------------------
# Pricing loader
# ---------------------------------------------------------------------------
_PRICING_FILE = ROOT / "shared" / "pricing" / "aws.json"
_pricing_cache: dict | None = None


def _load_pricing() -> dict:
    global _pricing_cache
    if _pricing_cache is None:
        with open(_PRICING_FILE) as f:
            _pricing_cache = json.load(f)
    return _pricing_cache


# ---------------------------------------------------------------------------
# Core calculation
# ---------------------------------------------------------------------------

def calculate_eks(inp: ClusterInput) -> CostResult:
    pricing = _load_pricing()
    pricing_date = pricing["_meta"]["pricing_date"]

    # --- Validate region ---
    regions = pricing["regions"]
    if inp.region not in regions:
        available = ", ".join(regions.keys())
        raise ValueError(
            f"Region '{inp.region}' not found in EKS pricing data. "
            f"Available: {available}"
        )
    region_data = regions[inp.region]

    # --- Validate instance type ---
    instances = region_data["instances"]
    if inp.instance_type not in instances:
        available = ", ".join(instances.keys())
        raise ValueError(
            f"Instance type '{inp.instance_type}' not found for region '{inp.region}'. "
            f"Available: {available}"
        )
    inst_raw = instances[inp.instance_type]
    instance_price = InstancePrice(
        instance_type=inp.instance_type,
        vcpu=inst_raw["vcpu"],
        memory_gb=inst_raw["memory_gb"],
        price_usd_per_hour=inst_raw["price_usd_per_hour"],
    )

    # --- Build pricing structs ---
    stor_raw = region_data["storage"]
    storage_pricing = StoragePricing(
        ssd_per_gb_month=stor_raw["ssd_per_gb_month"],
        hdd_per_gb_month=stor_raw["hdd_per_gb_month"],
        snapshot_per_gb_month=stor_raw["snapshot_per_gb_month"],
    )

    net_raw = region_data["networking"]
    network_pricing = NetworkPricing(
        egress_per_gb=net_raw["egress_per_gb"],
        inter_az_per_gb=net_raw["inter_az_per_gb"],
        load_balancer_per_hour=net_raw["load_balancer_per_hour"],
    )

    cp_raw = pricing["control_plane"]
    cp_pricing = ControlPlanePricing(
        hourly_fee=cp_raw["hourly_fee"],
        free_tiers=cp_raw["free_tiers"],
    )

    sup_raw = pricing["support"]
    support_pricing = SupportPricing(
        basic_floor_usd=sup_raw["developer_floor_usd"],
        basic_pct=sup_raw["developer_pct"],
        business_floor_usd=sup_raw["business_floor_usd"],
        business_pct=sup_raw["business_pct"],
    )

    # --- Calculate each dimension ---
    c_compute = round2(compute_cost(instance_price, inp.node_count))
    c_control_plane = round2(control_plane_cost(cp_pricing, cluster_count=1))
    c_storage = round2(
        storage_cost(
            storage_pricing,
            inp.storage_gb_per_node,
            inp.node_count,
            inp.snapshot_gb,
            inp.storage_class.value,
        )
    )
    c_networking = round2(
        networking_cost(
            network_pricing,
            inp.egress_gb_per_month,
            inp.inter_az_gb_per_month,
            inp.load_balancer_count,
        )
    )

    subtotal = c_compute + c_control_plane + c_storage + c_networking
    c_support = round2(support_cost(support_pricing, subtotal, inp.support_tier.value))

    monthly = round2(subtotal + c_support)

    # --- Notes / caveats ---
    notes = [
        "Prices are on-demand Linux rates; reserved or Spot instances may reduce compute by 30–70%.",
        "EKS charges $0.10/hr per cluster for the managed control plane.",
        "Egress pricing shown is standard internet egress; CloudFront or VPC endpoint transfer may differ.",
    ]
    if inp.support_tier == SupportTier.NONE:
        notes.append("No AWS support plan selected; add Developer ($29/mo min) or Business ($100/mo min) as needed.")

    return CostResult(
        provider=Provider.EKS,
        region=inp.region,
        instance_type=inp.instance_type,
        node_count=inp.node_count,
        monthly_total_usd=monthly,
        annual_total_usd=annual(monthly),
        breakdown=CostBreakdown(
            compute=c_compute,
            control_plane=c_control_plane,
            storage=c_storage,
            networking=c_networking,
            support=c_support,
        ),
        pricing_date=pricing_date,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Netlify handler
# ---------------------------------------------------------------------------

CORS_HEADERS = {
    "Access-Control-Allow-Origin": os.getenv("ALLOWED_ORIGIN", "*"),
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Content-Type": "application/json",
}


def handler(event: dict, context) -> dict:
    # Handle CORS preflight
    if event.get("httpMethod") == "OPTIONS":
        return {"statusCode": 204, "headers": CORS_HEADERS, "body": ""}

    if event.get("httpMethod") != "POST":
        return {
            "statusCode": 405,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": "Method not allowed. Use POST."}),
        }

    # Parse body
    try:
        raw = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            import base64
            raw = base64.b64decode(raw).decode()
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        return {
            "statusCode": 400,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": f"Invalid JSON: {e}"}),
        }

    # Validate input
    try:
        inp = ClusterInput(**payload)
    except Exception as e:
        return {
            "statusCode": 422,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": f"Validation error: {e}"}),
        }

    # Calculate
    try:
        result = calculate_eks(inp)
    except ValueError as e:
        return {
            "statusCode": 400,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": str(e)}),
        }
    except Exception as e:
        return {
            "statusCode": 500,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": f"Internal error: {e}"}),
        }

    return {
        "statusCode": 200,
        "headers": CORS_HEADERS,
        "body": result.model_dump_json(),
    }
