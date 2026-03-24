"""
Unit tests for the EKS calculator and shared cost engine.
Run with: pytest tests/test_eks.py -v
"""
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.models import ClusterInput, StorageClass, SupportTier
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
    HOURS_PER_MONTH,
)
from netlify.functions.eks import calculate_eks


# ---------------------------------------------------------------------------
# Cost engine unit tests
# ---------------------------------------------------------------------------

class TestComputeCost:
    def test_single_node(self):
        inst = InstancePrice("m5.xlarge", 4, 16, 0.192)
        assert compute_cost(inst, 1) == pytest.approx(0.192 * HOURS_PER_MONTH)

    def test_ten_nodes(self):
        inst = InstancePrice("m5.xlarge", 4, 16, 0.192)
        assert compute_cost(inst, 10) == pytest.approx(0.192 * HOURS_PER_MONTH * 10)

    def test_zero_price(self):
        inst = InstancePrice("free.test", 2, 4, 0.0)
        assert compute_cost(inst, 100) == 0.0


class TestControlPlaneCost:
    def test_eks_single_cluster(self):
        cp = ControlPlanePricing(hourly_fee=0.10, free_tiers=0)
        expected = 0.10 * HOURS_PER_MONTH
        assert control_plane_cost(cp, 1) == pytest.approx(expected)

    def test_gke_free_first_cluster(self):
        cp = ControlPlanePricing(hourly_fee=0.10, free_tiers=1)
        assert control_plane_cost(cp, 1) == 0.0

    def test_gke_second_cluster_billed(self):
        cp = ControlPlanePricing(hourly_fee=0.10, free_tiers=1)
        assert control_plane_cost(cp, 2) == pytest.approx(0.10 * HOURS_PER_MONTH)

    def test_aks_free_control_plane(self):
        cp = ControlPlanePricing(hourly_fee=0.0, free_tiers=0)
        assert control_plane_cost(cp, 1) == 0.0


class TestStorageCost:
    def setup_method(self):
        self.pricing = StoragePricing(
            ssd_per_gb_month=0.10,
            hdd_per_gb_month=0.045,
            snapshot_per_gb_month=0.05,
        )

    def test_ssd_no_snapshots(self):
        cost = storage_cost(self.pricing, 100, 5, 0, "ssd")
        assert cost == pytest.approx(100 * 5 * 0.10)

    def test_hdd_with_snapshots(self):
        cost = storage_cost(self.pricing, 500, 3, 200, "hdd")
        pv = 500 * 3 * 0.045
        snap = 200 * 0.05
        assert cost == pytest.approx(pv + snap)

    def test_zero_storage(self):
        assert storage_cost(self.pricing, 0, 10, 0, "ssd") == 0.0


class TestNetworkingCost:
    def setup_method(self):
        self.pricing = NetworkPricing(
            egress_per_gb=0.09,
            inter_az_per_gb=0.01,
            load_balancer_per_hour=0.0225,
        )

    def test_egress_only(self):
        cost = networking_cost(self.pricing, 1000, 0, 0)
        assert cost == pytest.approx(1000 * 0.09)

    def test_all_dimensions(self):
        cost = networking_cost(self.pricing, 500, 200, 2)
        expected = (500 * 0.09) + (200 * 0.01) + (2 * 0.0225 * HOURS_PER_MONTH)
        assert cost == pytest.approx(expected)

    def test_zero_networking(self):
        assert networking_cost(self.pricing, 0, 0, 0) == 0.0


class TestSupportCost:
    def setup_method(self):
        self.pricing = SupportPricing(
            basic_floor_usd=29,
            basic_pct=0.03,
            business_floor_usd=100,
            business_pct=0.10,
        )

    def test_none_tier(self):
        assert support_cost(self.pricing, 10_000, "none") == 0.0

    def test_basic_floor_applies(self):
        assert support_cost(self.pricing, 100, "basic") == pytest.approx(29)

    def test_basic_pct_applies(self):
        assert support_cost(self.pricing, 2000, "basic") == pytest.approx(60)

    def test_business_floor_applies(self):
        assert support_cost(self.pricing, 500, "business") == pytest.approx(100)

    def test_business_pct_applies(self):
        assert support_cost(self.pricing, 5000, "business") == pytest.approx(500)


# ---------------------------------------------------------------------------
# EKS integration tests
# ---------------------------------------------------------------------------

def _base_input(**overrides) -> ClusterInput:
    defaults = dict(
        node_count=10,
        instance_type="m5.xlarge",
        region="us-east-1",
        storage_gb_per_node=100,
        storage_class=StorageClass.SSD,
        snapshot_gb=500,
        egress_gb_per_month=1000,
        inter_az_gb_per_month=500,
        load_balancer_count=2,
        support_tier=SupportTier.NONE,
    )
    defaults.update(overrides)
    return ClusterInput(**defaults)


class TestCalculateEKS:
    def test_basic_calculation_runs(self):
        result = calculate_eks(_base_input())
        assert result.provider.value == "eks"
        assert result.monthly_total_usd > 0
        assert result.annual_total_usd == pytest.approx(result.monthly_total_usd * 12, rel=1e-4)

    def test_compute_dominates_small_cluster(self):
        result = calculate_eks(_base_input(
            node_count=5,
            storage_gb_per_node=0,
            snapshot_gb=0,
            egress_gb_per_month=0,
            inter_az_gb_per_month=0,
            load_balancer_count=0,
        ))
        assert result.breakdown.compute > result.breakdown.storage
        assert result.breakdown.compute > result.breakdown.networking

    def test_control_plane_fee_always_present(self):
        result = calculate_eks(_base_input())
        expected_cp = 0.10 * HOURS_PER_MONTH
        assert result.breakdown.control_plane == pytest.approx(expected_cp, rel=1e-3)

    def test_eu_region_higher_than_us(self):
        us = calculate_eks(_base_input(region="us-east-1", instance_type="m5.xlarge"))
        eu = calculate_eks(_base_input(region="eu-west-1", instance_type="m5.xlarge"))
        assert eu.monthly_total_usd > us.monthly_total_usd

    def test_business_support_adds_cost(self):
        no_sup = calculate_eks(_base_input(support_tier=SupportTier.NONE))
        with_sup = calculate_eks(_base_input(support_tier=SupportTier.BUSINESS))
        assert with_sup.monthly_total_usd > no_sup.monthly_total_usd
        assert with_sup.breakdown.support > 0

    def test_invalid_region_raises(self):
        with pytest.raises(ValueError, match="Region"):
            calculate_eks(_base_input(region="mars-1"))

    def test_invalid_instance_raises(self):
        with pytest.raises(ValueError, match="Instance type"):
            calculate_eks(_base_input(instance_type="p99.galactic"))

    def test_notes_are_non_empty(self):
        result = calculate_eks(_base_input())
        assert len(result.notes) > 0

    def test_pricing_date_present(self):
        result = calculate_eks(_base_input())
        assert result.pricing_date != ""

    def test_zero_optional_fields(self):
        """A minimal cluster with only required fields should still calculate cleanly."""
        result = calculate_eks(ClusterInput(
            node_count=3,
            instance_type="t3.medium",
            region="us-east-1",
        ))
        assert result.monthly_total_usd > 0
        assert result.breakdown.storage == 0.0
        assert result.breakdown.networking == 0.0


# ---------------------------------------------------------------------------
# Handler integration tests (HTTP layer)
# ---------------------------------------------------------------------------

class TestHandler:
    def _call(self, body: dict, method: str = "POST") -> dict:
        from netlify.functions.eks import handler
        import json
        return handler({"httpMethod": method, "body": json.dumps(body), "isBase64Encoded": False}, None)

    def test_valid_request_returns_200(self):
        resp = self._call({"node_count": 5, "instance_type": "m5.xlarge", "region": "us-east-1"})
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert "monthly_total_usd" in body

    def test_options_returns_204(self):
        from netlify.functions.eks import handler
        resp = handler({"httpMethod": "OPTIONS"}, None)
        assert resp["statusCode"] == 204

    def test_get_returns_405(self):
        from netlify.functions.eks import handler
        import json
        resp = handler({"httpMethod": "GET", "body": "{}"}, None)
        assert resp["statusCode"] == 405

    def test_invalid_region_returns_400(self):
        resp = self._call({"node_count": 3, "instance_type": "m5.xlarge", "region": "fake-1"})
        assert resp["statusCode"] == 400

    def test_missing_required_field_returns_422(self):
        resp = self._call({"node_count": 3})  # missing instance_type and region
        assert resp["statusCode"] == 422

    def test_cors_headers_present(self):
        resp = self._call({"node_count": 5, "instance_type": "m5.xlarge", "region": "us-east-1"})
        assert "Access-Control-Allow-Origin" in resp["headers"]
