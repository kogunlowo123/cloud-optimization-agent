"""Unit tests for waste, sizing, schedule, tiering, anomaly and allocation analyzers."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from cloudopt.analyzers.billing import UNTAGGED, allocate, find_anomalies
from cloudopt.analyzers.sizing import find_rightsizing, find_schedules, find_storage_tiering
from cloudopt.analyzers.waste import find_waste
from cloudopt.config import Policy
from cloudopt.models import BillingLine
from cloudopt.pricing import Pricing
from tests.conftest import TODAY, bill, make_ctx, res, vm

STEADY = {"observed_days": 30, "cpu_avg": 30, "cpu_p95": 45, "mem_p95": 50}


def waste(*resources: Any, policy: Policy | None = None) -> tuple[list[Any], int]:
    return find_waste(make_ctx(resources, policy))


class TestWaste:
    def test_idle_vm_fires_and_reports_confidence(self) -> None:
        recs, blind = waste(
            vm(
                metrics={"observed_days": 30, "cpu_p95": 2.0, "net_gb_per_day": 0.05},
                tags={"env": "dev"},
            )
        )
        assert (
            blind == 0
            and len(recs) == 1
            and recs[0].category == "waste"
            and recs[0].details["fraction"] == 1.0
        )
        assert (
            recs[0].confidence == "high"
            and recs[0].risk == "medium"
            and recs[0].monthly_saving == recs[0].current_monthly
        )

    @pytest.mark.parametrize(
        "metrics",
        [
            {"observed_days": 30, "cpu_p95": 9.0, "net_gb_per_day": 0.05},
            {"observed_days": 30, "cpu_p95": 2.0, "net_gb_per_day": 5},
            {"observed_days": 5, "cpu_p95": 2.0, "net_gb_per_day": 0.05},
            {
                "observed_days": 30,
                "cpu_p95": 2.0,
                "net_gb_per_day": 0.05,
                "connections_per_day": 500,
            },
        ],
    )
    def test_busy_or_unproven_vms_are_left_alone(self, metrics: dict[str, Any]) -> None:
        assert waste(vm(metrics=metrics))[0] == []

    def test_prod_and_database_risk_is_higher(self) -> None:
        prod, _ = waste(vm(tags={"env": "prod"}, metrics={"observed_days": 30, "cpu_p95": 1.0}))
        db, _ = waste(res("db", "database", metrics={"observed_days": 30, "cpu_p95": 1.0}))
        assert prod[0].risk == "high" and db[0].risk == "high" and "database" in db[0].title

    def test_missing_metrics_are_counted_not_guessed(self) -> None:
        recs, blind = waste(
            vm("a"), vm("b", state="stopped"), res("lb", "load_balancer"), res("nat", "nat_gateway")
        )
        assert recs == [] and blind == 3

    def test_threshold_comes_from_policy(self) -> None:
        metrics = {"observed_days": 30, "cpu_p95": 9.0, "net_gb_per_day": 0.05}
        assert len(waste(vm(metrics=metrics), policy=Policy(idle_cpu_p95=10))[0]) == 1

    def test_unattached_disk_keeps_a_snapshot(self) -> None:
        disk = res("d", "disk", state="unattached", state_days=45, size_gb=500, monthly_cost=40)
        recs, _ = waste(disk)
        assert (
            len(recs) == 1
            and recs[0].details["fraction"] == pytest.approx(1 - 500 * 0.05 / 40)
            and recs[0].confidence == "high"
        )
        assert (
            waste(res("d", "disk", state="unattached", state_days=3, size_gb=500, monthly_cost=40))[
                0
            ]
            == []
        )
        assert waste(res("d", "disk", state="unattached", monthly_cost=40))[0] == []
        assert (
            waste(res("d", "disk", state="attached", state_days=90, size_gb=10, monthly_cost=40))[0]
            == []
        )
        assert (
            waste(res("d", "disk", state="unattached", state_days=8, size_gb=500, monthly_cost=40))[
                0
            ][0].confidence
            == "medium"
        )

    def test_snapshot_cost_never_exceeds_the_disk_cost_share(self) -> None:
        recs, _ = waste(
            res("d", "disk", state="unattached", state_days=45, size_gb=1000, monthly_cost=10)
        )
        assert recs[0].details["fraction"] == pytest.approx(0.1)

    def test_disk_of_a_long_stopped_vm(self) -> None:
        stopped = vm("i-s", state="stopped", state_days=90, cost=0)
        disk = res("d", "disk", state="attached", attached_to="i-s", size_gb=100, monthly_cost=8)
        recs, _ = waste(stopped, disk)
        assert (
            len(recs) == 1
            and "long-stopped" in recs[0].title
            and recs[0].details["instance"] == "i-s"
        )
        assert waste(vm("i-s", state="stopped", state_days=10, cost=0), disk)[0] == []
        assert waste(vm("i-s", state="running"), disk)[0] == []

    def test_orphaned_snapshot(self) -> None:
        old = res(
            "s",
            "snapshot",
            state="available",
            created="2025-01-01T00:00:00Z",
            parent="gone",
            monthly_cost=15,
        )
        assert len(waste(old)[0]) == 1
        assert (
            waste(
                res(
                    "s", "snapshot", created="2025-01-01T00:00:00Z", parent="live", monthly_cost=15
                ),
                res("live", "disk", state="attached", monthly_cost=5),
            )[0]
            == []
        )
        assert (
            len(
                waste(
                    res(
                        "s",
                        "snapshot",
                        created="2025-01-01T00:00:00Z",
                        parent="dead",
                        monthly_cost=15,
                    ),
                    res("dead", "disk", state="deleted", monthly_cost=0.01),
                )[0]
            )
            == 1
        )
        assert (
            waste(
                res("s", "snapshot", created="2026-09-01T00:00:00Z", parent="gone", monthly_cost=15)
            )[0]
            == []
        )
        assert waste(res("s", "snapshot", parent="gone", monthly_cost=15))[0] == []
        assert (
            len(waste(res("s", "snapshot", created="2025-01-01T00:00:00Z", monthly_cost=15))[0])
            == 1
        )

    def test_addresses_load_balancers_and_gateways(self) -> None:
        assert len(waste(res("ip", "public_ip", state="unassociated", monthly_cost=3.65))[0]) == 1
        assert waste(res("ip", "public_ip", state="attached", monthly_cost=3.65))[0] == []
        lb = waste(
            res(
                "lb",
                "load_balancer",
                monthly_cost=22,
                metrics={"observed_days": 30, "connections_per_day": 0.2},
            )
        )[0]
        assert len(lb) == 1 and lb[0].details["connections_per_day"] == 0.2
        assert (
            waste(
                res(
                    "lb",
                    "load_balancer",
                    monthly_cost=22,
                    metrics={"observed_days": 30, "connections_per_day": 500},
                )
            )[0]
            == []
        )
        assert (
            waste(
                res(
                    "lb",
                    "load_balancer",
                    monthly_cost=22,
                    metrics={"observed_days": 3, "connections_per_day": 0},
                )
            )[0]
            == []
        )
        nat = waste(
            res(
                "nat",
                "nat_gateway",
                monthly_cost=33,
                metrics={"observed_days": 30, "net_gb_per_day": 0.02},
            )
        )[0]
        assert len(nat) == 1 and nat[0].effort == "medium"
        assert (
            waste(
                res(
                    "nat",
                    "nat_gateway",
                    monthly_cost=33,
                    metrics={"observed_days": 30, "net_gb_per_day": 40},
                )
            )[0]
            == []
        )

    def test_free_resources_are_skipped(self) -> None:
        assert waste(res("ip", "public_ip", state="unassociated", monthly_cost=0))[0] == []

    def test_ids_are_stable(self) -> None:
        a, _ = waste(vm(metrics={"observed_days": 30, "cpu_p95": 1}))
        b, _ = waste(vm(metrics={"observed_days": 30, "cpu_p95": 1}))
        assert a[0].id == b[0].id and a[0].id.startswith("R-")


class TestRightsizing:
    def test_oversized_fleet_moves_to_a_smaller_newer_size(self) -> None:
        r = vm(
            "i-w",
            "m5.2xlarge",
            tags={"env": "prod"},
            metrics={"observed_days": 30, "cpu_p95": 22, "mem_p95": 30},
        )
        recs = find_rightsizing(make_ctx([r]))
        assert len(recs) == 1
        rec = recs[0]
        assert (
            rec.details["to"] == "m7i.xlarge"
            and rec.details["newer_generation"]
            and rec.risk == "medium"
        )
        assert (
            rec.details["fraction"] == pytest.approx(1 - 0.1764 / 0.384)
            and rec.confidence == "high"
        )

    def test_memory_can_block_a_downsize(self) -> None:
        r = vm("i-w", "m5.2xlarge", metrics={"observed_days": 30, "cpu_p95": 22, "mem_p95": 60})
        assert find_rightsizing(make_ctx([r])) == []
        loose = find_rightsizing(make_ctx([r], Policy(rightsize_min_saving_pct=5)))
        assert loose[0].details["to"] == "m7i.2xlarge"
        assert (
            find_rightsizing(
                make_ctx(
                    [
                        vm(
                            "i-w",
                            "m5.2xlarge",
                            metrics={"observed_days": 30, "cpu_p95": 22, "mem_p95": 95},
                        )
                    ],
                    Policy(rightsize_min_saving_pct=1),
                )
            )
            == []
        )

    def test_missing_memory_data_keeps_the_memory_size(self) -> None:
        r = vm("i-w", "m5.2xlarge", metrics={"observed_days": 30, "cpu_p95": 10})
        rec = find_rightsizing(make_ctx([r], Policy(rightsize_min_saving_pct=5)))
        assert rec and Pricing.default().instance("aws", rec[0].details["to"]).mem_gb >= 32  # type: ignore[union-attr]

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"metrics": {"observed_days": 30, "cpu_p95": 80, "mem_p95": 50}},
            {"metrics": {"observed_days": 30, "cpu_p95": 2, "mem_p95": 5}},
            {"metrics": {"observed_days": 7, "cpu_p95": 22, "mem_p95": 30}},
            {"metrics": {"observed_days": 30, "cpu_p95": 60, "mem_p95": 50}},
            {"metrics": {"observed_days": 30, "mem_p95": 30}},
            {"state": "stopped", "metrics": {"observed_days": 30, "cpu_p95": 22, "mem_p95": 30}},
        ],
    )
    def test_no_recommendation_when_data_or_headroom_does_not_allow_it(
        self, kwargs: dict[str, Any]
    ) -> None:
        assert find_rightsizing(make_ctx([vm("i-w", "m5.2xlarge", **kwargs)])) == []

    def test_unknown_sizes_and_free_vms_are_skipped(self) -> None:
        assert (
            find_rightsizing(
                make_ctx(
                    [
                        vm(
                            "i-w",
                            "z9.mega",
                            cost=500,
                            metrics={"observed_days": 30, "cpu_p95": 22, "mem_p95": 30},
                        )
                    ]
                )
            )
            == []
        )
        assert (
            find_rightsizing(
                make_ctx(
                    [
                        vm(
                            "i-w",
                            "m5.2xlarge",
                            cost=0,
                            metrics={"observed_days": 30, "cpu_p95": 22, "mem_p95": 30},
                        )
                    ]
                )
            )
            == []
        )

    def test_minimum_saving_percentage(self) -> None:
        r = vm("i-w", "m5.4xlarge", metrics={"observed_days": 30, "cpu_p95": 62, "mem_p95": 70})
        assert find_rightsizing(make_ctx([r])) == []
        tiny = Pricing.default()
        r2 = vm("i-x", "m5.xlarge", metrics={"observed_days": 30, "cpu_p95": 30, "mem_p95": 40})
        assert len(find_rightsizing(make_ctx([r2], Policy(rightsize_min_saving_pct=1), tiny))) == 1
        assert find_rightsizing(make_ctx([r2], Policy(rightsize_min_saving_pct=90))) == []

    def test_same_family_when_there_is_no_successor(self) -> None:
        r = res(
            "i-a",
            "vm",
            provider="gcp",
            size="n2-standard-8",
            monthly_cost=283,
            metrics={"observed_days": 30, "cpu_p95": 25, "mem_p95": 30},
        )
        rec = find_rightsizing(make_ctx([r]))
        assert (
            rec
            and rec[0].details["to"] == "n2-standard-4"
            and not rec[0].details["newer_generation"]
        )


class TestSchedules:
    def nonprod(self, hours: float | None = 40, env: str = "dev", **kw: Any) -> Any:
        return vm(
            "i-n",
            "m5.large",
            tags={"env": env},
            metrics={"observed_days": 30, "cpu_p95": 30, "active_hours_per_week": hours},
            **kw,
        )

    def test_business_hours_schedule(self) -> None:
        recs = find_schedules(make_ctx([self.nonprod()]))
        assert (
            len(recs) == 1
            and recs[0].details["fraction"] == pytest.approx(1 - 55 / 168)
            and recs[0].risk == "low"
        )

    def test_heavier_use_keeps_more_hours(self) -> None:
        rec = find_schedules(make_ctx([self.nonprod(hours=58)]))[0]
        assert rec.details["scheduled_hours_per_week"] == 58 and rec.details[
            "fraction"
        ] == pytest.approx(1 - 58 / 168)

    @pytest.mark.parametrize("resource", [None, "prod", "busy", "nodata", "stopped"])
    def test_not_scheduled(self, resource: str | None) -> None:
        cases = {
            None: vm(
                "i-a",
                "m5.large",
                tags={},
                metrics={"observed_days": 30, "active_hours_per_week": 20},
            ),
            "prod": self.nonprod(env="prod"),
            "busy": self.nonprod(hours=100),
            "nodata": self.nonprod(hours=None),
            "stopped": self.nonprod(state="stopped"),
        }
        assert find_schedules(make_ctx([cases[resource]])) == []

    def test_databases_can_be_scheduled(self) -> None:
        db = res(
            "db",
            "database",
            monthly_cost=200,
            tags={"env": "test"},
            metrics={"observed_days": 30, "active_hours_per_week": 30},
        )
        assert len(find_schedules(make_ctx([db]))) == 1

    def test_business_hours_setting(self) -> None:
        rec = find_schedules(
            make_ctx([self.nonprod(hours=10)], Policy(business_hours_per_week=40))
        )[0]
        assert rec.details["fraction"] == pytest.approx(1 - 40 / 168)


class TestStorageTiering:
    def bucket(
        self, idle: int | None, gb: float = 10_000, provider: str = "aws", cost: float | None = None
    ) -> Any:
        return res(
            "b",
            "bucket",
            provider=provider,
            size_gb=gb,
            monthly_cost=gb * 0.023 if cost is None else cost,
            metrics={"last_accessed_days_ago": idle} if idle is not None else {},
        )

    def test_tiers_by_idle_time(self) -> None:
        ia = find_storage_tiering(make_ctx([self.bucket(120)]))[0]
        arch = find_storage_tiering(make_ctx([self.bucket(400)]))[0]
        assert (
            ia.details["tier"] == "infrequent"
            and arch.details["tier"] == "archive"
            and arch.monthly_saving > ia.monthly_saving
        )
        net = 10_000 * (0.023 - 0.0125) - 10_000 * 0.05 * 0.01
        assert ia.details["net_monthly"] == pytest.approx(net)

    @pytest.mark.parametrize(
        "bucket", [{"idle": 30}, {"idle": None}, {"idle": 400, "gb": 0}, {"idle": 120, "gb": 20}]
    )
    def test_recent_unknown_or_tiny_data_is_left_alone(self, bucket: dict[str, Any]) -> None:
        assert find_storage_tiering(make_ctx([self.bucket(**bucket)])) == []

    def test_fraction_is_capped(self) -> None:
        rec = find_storage_tiering(make_ctx([self.bucket(400, cost=1.0)]))[0]
        assert rec.details["fraction"] <= 0.95

    def test_missing_tier_prices_skip_the_bucket(self) -> None:
        assert (
            find_storage_tiering(make_ctx([self.bucket(400)], Policy(), Pricing(storage={}))) == []
        )

    def test_uses_the_policy_thresholds(self) -> None:
        assert (
            find_storage_tiering(
                make_ctx(
                    [self.bucket(120)], Policy(cold_days_infrequent=200, cold_days_archive=500)
                )
            )
            == []
        )


POLICY = Policy()


class TestAnomalies:
    def flat(self, value: float = 100.0, days: int = 28) -> list[float]:
        return [value] * days

    def test_shift_from_a_flat_baseline(self) -> None:
        lines = bill("compute", [*self.flat(), 130, 130, 130])
        [a] = find_anomalies(lines, POLICY)
        assert (
            a.kind == "shift"
            and a.baseline_daily == 100
            and a.current_daily == pytest.approx(130)
            and a.extra_monthly == pytest.approx(30 * 30.4375, rel=1e-3)
        )
        assert a.first_day == TODAY - timedelta(days=3) and a.last_day == TODAY - timedelta(days=1)

    def test_single_day_spike(self) -> None:
        [a] = find_anomalies(bill("compute", [*self.flat(), 100, 100, 200]), POLICY)
        assert a.kind == "spike" and a.current_daily == 200 and a.first_day == a.last_day

    def test_noisy_baseline_needs_a_real_jump(self) -> None:
        noisy = [100 + (i % 7) * 4 for i in range(28)]
        assert find_anomalies(bill("compute", [*noisy, 112, 118, 121]), POLICY) == []
        assert len(find_anomalies(bill("compute", [*noisy, 180, 190, 185]), POLICY)) == 1

    def test_small_or_cheap_changes_are_ignored(self) -> None:
        assert find_anomalies(bill("compute", [*self.flat(), 104, 104, 104]), POLICY) == []
        assert find_anomalies(bill("compute", [*self.flat(1.0), 3, 3, 3]), POLICY) == []
        assert find_anomalies(bill("compute", [*self.flat(), 80, 80, 80]), POLICY) == []

    def test_short_history_is_ignored(self) -> None:
        assert find_anomalies(bill("compute", [100] * 8 + [500, 500, 500]), POLICY) == []

    def test_thresholds_are_policy(self) -> None:
        lines = bill("compute", [*self.flat(), 108, 108, 108])
        assert find_anomalies(lines, POLICY) == []
        assert (
            len(
                find_anomalies(lines, Policy(anomaly_min_increase_pct=5, anomaly_min_extra_daily=2))
            )
            == 1
        )

    def test_attribution_names_the_resource_that_grew(self) -> None:
        steady = bill("compute", [90] * 31, resource_id="steady")
        gpu = bill("compute", [40] * 3, resource_id="new-gpu")
        [a] = find_anomalies(steady + gpu, POLICY)
        assert a.top_resources[0]["resource_id"] == "new-gpu" and a.top_resources[0][
            "extra_daily"
        ] == pytest.approx(40, abs=0.5)

    def test_services_are_independent_and_sorted_by_impact(self) -> None:
        lines = (
            bill("compute", [*self.flat(), 200, 200, 200])
            + bill("storage", [*self.flat(), 150, 150, 150])
            + bill("networking", self.flat(50, 31))
        )
        assert [a.service for a in find_anomalies(lines, POLICY)] == ["compute", "storage"]

    def test_unassigned_lines_are_attributed(self) -> None:
        lines = [
            BillingLine(
                day=TODAY - timedelta(days=31 - i), service="compute", cost=100 if i < 28 else 200
            )
            for i in range(31)
        ]
        assert find_anomalies(lines, POLICY)[0].top_resources[0]["resource_id"] == "(unassigned)"


class TestAllocation:
    def test_billing_tag_wins_then_resource_tag_then_untagged(self) -> None:
        resources = [
            res("a", tags={"team": "platform"}),
            res("b", tags={}),
            res("c", tags={"team": "data"}),
        ]
        lines = [
            BillingLine(
                day=TODAY, resource_id="a", service="compute", cost=10, tags={"team": "override"}
            ),
            BillingLine(day=TODAY, resource_id="a", service="compute", cost=20),
            BillingLine(day=TODAY, resource_id="b", service="compute", cost=30),
            BillingLine(day=TODAY, resource_id="c", service="compute", cost=40),
            BillingLine(day=TODAY, resource_id="zzz", service="compute", cost=0),
        ]
        team = next(
            a
            for a in allocate(lines, resources, Policy(allocation_tags=["team"]))
            if a.dimension == "team"
        )
        values = {r.value: r.monthly_cost for r in team.rows}
        assert values == pytest.approx(
            {
                "data": 40 * 30.4375,
                "(untagged)": 30 * 30.4375,
                "platform": 20 * 30.4375,
                "override": 10 * 30.4375,
            },
            abs=0.01,
        )
        assert (
            team.untagged_share == pytest.approx(0.3)
            and team.untagged_cost == pytest.approx(30 * 30.4375, abs=0.01)
            and team.rows[0].value == "data"
        )

    def test_window_and_scaling(self) -> None:
        old = BillingLine(
            day=TODAY - timedelta(days=90), service="compute", cost=999, tags={"team": "old"}
        )
        recent = [
            BillingLine(
                day=TODAY - timedelta(days=i), service="compute", cost=2, tags={"team": "a"}
            )
            for i in range(10)
        ]
        [team] = allocate(
            [old, *recent], [], Policy(allocation_tags=["team"], allocation_window_days=30)
        )
        assert (
            [r.value for r in team.rows] == ["a"]
            and team.rows[0].monthly_cost == pytest.approx(20 * 30.4375 / 10, abs=0.01)
            and team.rows[0].share == 1.0
        )

    def test_empty_billing_and_zero_cost(self) -> None:
        assert allocate([], [], Policy()) == []
        [row] = allocate(
            [BillingLine(day=TODAY, service="s", cost=0)], [], Policy(allocation_tags=["team"])
        )
        assert row.untagged_share == 0.0 and UNTAGGED == "(untagged)"
