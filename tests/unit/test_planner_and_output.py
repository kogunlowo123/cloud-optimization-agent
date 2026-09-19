"""Unit tests for the planner, suggested commands, reports and summaries."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

import pytest

from cloudopt.analyzers.sizing import find_rightsizing, find_schedules
from cloudopt.analyzers.waste import find_waste
from cloudopt.commands import NOTICE, commands_for
from cloudopt.config import Policy
from cloudopt.errors import ProviderError, ReportError
from cloudopt.models import Report
from cloudopt.planner import plan, protection_reason
from cloudopt.reporting import render_csv, render_json, render_markdown, write_reports
from cloudopt.service import OptimizationService
from cloudopt.summary import LLMSummaryWriter, SummaryFacts, TemplateSummaryWriter, facts_for
from tests.conftest import TODAY, make_ctx, make_settings, res, vm

IDLE = {"observed_days": 30, "cpu_p95": 1.0, "net_gb_per_day": 0.01}
STEADY = {"observed_days": 60, "cpu_p95": 60, "mem_p95": 70}
NONPROD = {"observed_days": 30, "cpu_p95": 30, "mem_p95": 40, "active_hours_per_week": 40}


def run(resources: list[Any], policy: Policy | None = None) -> tuple[list[Any], list[Any], Any]:
    ctx = make_ctx(resources, policy)
    candidates = [*find_waste(ctx)[0], *find_rightsizing(ctx), *find_schedules(ctx)]
    return plan(ctx, candidates)


class TestPlanner:
    def test_waste_removes_every_other_recommendation_for_the_resource(self) -> None:
        r = vm(
            "i-1",
            "m5.2xlarge",
            tags={"env": "dev"},
            metrics={
                "observed_days": 30,
                "cpu_p95": 1.0,
                "mem_p95": 5,
                "net_gb_per_day": 0.01,
                "active_hours_per_week": 20,
            },
        )
        active, _, totals = run([r])
        assert [a.category for a in active if a.resource_id == "i-1"] == ["waste"]
        assert totals.identified_saving == pytest.approx(r.monthly_cost)

    def test_rightsize_then_schedule_compose_without_double_counting(self) -> None:
        r = vm(
            "i-1",
            "m5.2xlarge",
            tags={"env": "dev"},
            metrics={
                "observed_days": 30,
                "cpu_p95": 22,
                "mem_p95": 30,
                "active_hours_per_week": 40,
            },
        )
        active, _, totals = run([r])
        by_cat = {a.category: a for a in active}
        cost = r.monthly_cost
        expected_rightsize = cost * (1 - 0.1764 / 0.384)
        expected_schedule = (cost - expected_rightsize) * (1 - 55 / 168)
        assert by_cat["rightsize"].monthly_saving == pytest.approx(expected_rightsize, abs=0.01)
        assert by_cat["schedule"].monthly_saving == pytest.approx(expected_schedule, abs=0.01)
        assert by_cat["schedule"].current_monthly == pytest.approx(
            cost - expected_rightsize, abs=0.01
        )
        assert (
            totals.identified_saving
            == pytest.approx(expected_rightsize + expected_schedule, abs=0.02)
            and totals.identified_saving < cost
        )

    def test_protected_resources_are_blocked_not_planned(self) -> None:
        keep = vm("i-keep", tags={"do-not-optimize": "legacy"}, metrics=IDLE)
        free = vm("i-free", metrics=IDLE)
        active, blocked, totals = run([keep, free])
        assert [a.resource_id for a in active] == ["i-free"] and [
            b.resource_id for b in blocked
        ] == ["i-keep"]
        assert blocked[0].blocked == "tagged do-not-optimize=legacy" and blocked[
            0
        ].monthly_saving == pytest.approx(keep.monthly_cost)
        assert totals.blocked_saving == pytest.approx(
            keep.monthly_cost
        ) and totals.identified_saving == pytest.approx(free.monthly_cost)

    def test_protection_rules(self) -> None:
        policy = Policy(protect_tags={"tier": "gold"}, protected_ids=["i-9"])
        assert protection_reason(vm("i-9"), policy) == "listed in protected_ids"
        assert protection_reason(vm("i-1", tags={"tier": "GOLD"}), policy) == "tagged tier=GOLD"
        assert protection_reason(vm("i-1", tags={"tier": "silver"}), policy) == ""
        assert (
            protection_reason(vm("i-1", tags={"do-not-optimize": "x"}), Policy())
            == "tagged do-not-optimize=x"
        )
        assert protection_reason(vm("i-1"), Policy()) == ""

    def test_commitments_cover_only_steady_spend_that_remains(self) -> None:
        steady = [
            vm(f"i-s{n}", "m5.xlarge", tags={"env": "prod"}, metrics=STEADY) for n in range(3)
        ]
        gone = vm(
            "i-gone",
            "m5.xlarge",
            metrics={"observed_days": 60, "cpu_p95": 1.0, "net_gb_per_day": 0.01},
        )
        sched = vm(
            "i-sched", "m5.large", tags={"env": "test"}, metrics={**NONPROD, "observed_days": 60}
        )
        young = vm(
            "i-young", "m5.xlarge", metrics={"observed_days": 10, "cpu_p95": 60, "mem_p95": 70}
        )
        active, _, totals = run([*steady, gone, sched, young])
        commit = next(a for a in active if a.category == "commitment")
        eligible = sum(s.monthly_cost for s in steady)
        assert (
            commit.id == "R-COMMIT-AWS"
            and commit.details["eligible_monthly"] == pytest.approx(eligible, abs=0.01)
            and commit.details["instances"] == 3
        )
        assert commit.monthly_saving == pytest.approx(eligible * 0.8 * 0.28, abs=0.01)
        assert (
            commit.details["commit_hourly"] == pytest.approx(eligible * 0.8 / 730, abs=0.001)
            and commit.confidence == "high"
        )
        assert totals.by_category["commitment"] == commit.monthly_saving

    def test_commitment_uses_rightsized_cost_and_term_discount(self) -> None:
        web = [
            vm(
                f"i-w{n}",
                "m5.2xlarge",
                tags={"env": "prod"},
                metrics={"observed_days": 90, "cpu_p95": 22, "mem_p95": 30},
            )
            for n in range(2)
        ]
        active, _, _ = run(web, Policy(commitment_term="3y", commitment_coverage=0.5))
        rightsized = sum(
            a.current_monthly - a.monthly_saving for a in active if a.category == "rightsize"
        )
        commit = next(a for a in active if a.category == "commitment")
        assert commit.details["eligible_monthly"] == pytest.approx(rightsized, abs=0.01)
        assert (
            commit.monthly_saving == pytest.approx(rightsized * 0.5 * 0.46, abs=0.01)
            and commit.confidence == "high"
        )

    def test_commitments_are_per_provider_and_skip_unknown_discounts(self) -> None:
        a = vm("i-a", "m5.xlarge", metrics=STEADY)
        g = res("g", "vm", provider="gcp", size="n2-standard-8", monthly_cost=283, metrics=STEADY)
        active, _, _ = run([a, g])
        assert {x.id for x in active if x.category == "commitment"} == {
            "R-COMMIT-AWS",
            "R-COMMIT-GCP",
        }
        ctx = make_ctx([a])
        empty = plan(
            ctx.__class__(
                ctx.resources,
                ctx.policy,
                ctx.pricing.__class__(instances=ctx.pricing.instances),
                ctx.today,
            ),
            [],
        )
        assert [x for x in empty[0] if x.category == "commitment"] == []

    def test_totals_add_up_and_never_exceed_spend(self) -> None:
        estate = [
            vm(
                "i-a",
                "m5.2xlarge",
                tags={"env": "dev"},
                metrics={
                    "observed_days": 60,
                    "cpu_p95": 22,
                    "mem_p95": 30,
                    "active_hours_per_week": 30,
                },
            ),
            vm("i-b", metrics=IDLE),
            res("ip", "public_ip", state="unassociated", monthly_cost=3.65),
        ]
        active, _, totals = run(estate)
        assert totals.identified_saving == pytest.approx(
            sum(a.monthly_saving for a in active), abs=0.01
        )
        assert sum(totals.by_category.values()) == pytest.approx(totals.identified_saving, abs=0.01)
        assert (
            totals.identified_saving <= totals.monthly_spend
            and totals.saving_pct
            == pytest.approx(100 * totals.identified_saving / totals.monthly_spend, abs=0.1)
        )
        assert [a.monthly_saving for a in active] == sorted(
            (a.monthly_saving for a in active), reverse=True
        )

    def test_empty_plan(self) -> None:
        active, blocked, totals = run([])
        assert (
            active == []
            and blocked == []
            and totals.monthly_spend == 0
            and totals.saving_pct == 0.0
        )


class TestCommands:
    def rec(self, resource: Any) -> Any:
        active, blocked, _ = run([resource])
        return (active or blocked)[0]

    def test_waste_commands_per_provider(self) -> None:
        aws = self.rec(
            res("vol-1", "disk", state="unattached", state_days=45, size_gb=500, monthly_cost=40)
        )
        cmds = commands_for(aws, res("vol-1", "disk", state="unattached"))
        assert (
            cmds[0] == NOTICE
            and "aws ec2 create-snapshot --volume-id vol-1" in cmds[1]
            and "aws ec2 delete-volume --volume-id vol-1" in cmds[2]
        )
        gcp = res(
            "d1",
            "disk",
            provider="gcp",
            region="us-central1-a",
            state="unattached",
            state_days=45,
            size_gb=500,
            monthly_cost=40,
        )
        assert (
            "gcloud compute disks delete d1 --zone us-central1-a"
            in commands_for(self.rec(gcp), gcp)[-1]
        )
        az = res(
            "/subscriptions/x/disks/d",
            "disk",
            provider="azure",
            name="d",
            region="rg1",
            state="unattached",
            state_days=45,
            size_gb=500,
            monthly_cost=40,
        )
        assert (
            "az disk delete --ids /subscriptions/x/disks/d --yes"
            in commands_for(self.rec(az), az)[-1]
        )

    def test_resize_uses_the_target_size(self) -> None:
        r = vm("i-1", "m5.2xlarge", metrics={"observed_days": 30, "cpu_p95": 22, "mem_p95": 30})
        cmds = commands_for(self.rec(r), r)
        assert any("--instance-type Value=m7i.xlarge" in c for c in cmds) and cmds[1].startswith(
            "aws ec2 stop-instances"
        )

    def test_hostile_inventory_values_are_shell_quoted(self) -> None:
        evil = res(
            "vol-1; rm -rf /",
            "disk",
            name="$(curl evil)\nx",
            state="unattached",
            state_days=45,
            size_gb=500,
            monthly_cost=40,
        )
        cmds = commands_for(self.rec(evil), evil)
        text = "\n".join(cmds)
        assert (
            "'vol-1; rm -rf /'" in text
            and "\n x" not in text
            and "delete-volume --volume-id 'vol-1; rm -rf /'" in text
        )
        assert "vol-1; rm -rf / " not in text.replace("'vol-1; rm -rf /'", "")

    def test_no_commands_for_schedules_databases_and_commitments(self) -> None:
        sched = vm("i-1", "m5.large", tags={"env": "dev"}, metrics=NONPROD)
        active, _, _ = run([sched])
        assert commands_for(active[0], sched) == []
        db = res("db", "database", monthly_cost=100, metrics=IDLE)
        assert commands_for(self.rec(db), db) == []
        commit_ctx = run([vm("i-a", metrics=STEADY)])[0]
        assert (
            commands_for(next(a for a in commit_ctx if a.category == "commitment"), vm("i-a")) == []
        )

    def test_tiering_command(self) -> None:
        bucket = res(
            "logs",
            "bucket",
            size_gb=10_000,
            monthly_cost=230,
            metrics={"last_accessed_days_ago": 400},
        )
        from cloudopt.analyzers.sizing import find_storage_tiering

        ctx = make_ctx([bucket])
        active, _, _ = plan(ctx, find_storage_tiering(ctx))
        assert (
            "put-bucket-lifecycle-configuration --bucket logs" in commands_for(active[0], bucket)[1]
        )


def build_report(**overrides: Any) -> Report:
    service = OptimizationService(make_settings(), TemplateSummaryWriter())
    resources = [
        vm("i-idle", metrics=IDLE, tags={"env": "dev", "team": "data"}),
        vm("i-keep", metrics=IDLE, tags={"do-not-optimize": "x"}),
        res("ip", "public_ip", state="unassociated", monthly_cost=3.65),
    ]
    return service.analyze(
        overrides.get("resources", resources),
        overrides.get("billing", []),
        policy=Policy(),
        pricing=__import__("cloudopt.pricing", fromlist=["Pricing"]).Pricing.default(),
        today=TODAY,
    )


class TestReports:
    def test_markdown_sections_and_money(self) -> None:
        md = render_markdown(build_report())
        for heading in (
            "# Cloud optimization report as of 2026-09-19",
            "## Summary",
            "## Savings",
            "## Recommendations",
            "### Details",
            "## Blocked by protection rules",
            "## Cost anomalies",
            "## Cost allocation",
            "## Data warnings",
        ):
            assert heading in md
        assert (
            "Idle instance: i-idle" in md
            and "No billing data was supplied." in md
            and "```bash" in md
            and NOTICE in md
        )

    def test_no_recommendations(self) -> None:
        md = render_markdown(build_report(resources=[vm("i-ok", metrics=STEADY)]))
        assert ("## Recommendations" in md and "Commit to 1y" not in md) or "Commit" in md

    def test_hostile_text_is_escaped(self) -> None:
        evil = res(
            "i-1",
            "vm",
            name="<script>alert(1)</script> | x\n# injected",
            size="m5.xlarge",
            monthly_cost=100,
            metrics=IDLE,
            tags={"env": "dev"},
        )
        md = render_markdown(build_report(resources=[evil]))
        assert "<script>" not in md and "\n# injected" not in md and "&lt;script&gt;" in md

    def test_csv_shape_and_formula_safety(self) -> None:
        evil = res(
            "=cmd|' /C calc'!A0",
            "vm",
            name="=HYPERLINK(1)",
            size="m5.xlarge",
            monthly_cost=100,
            metrics=IDLE,
        )
        rows = list(csv.reader(io.StringIO(render_csv(build_report(resources=[evil])))))
        assert rows[0][0] == "id" and all(not c.startswith("=") for row in rows for c in row)
        full = list(csv.reader(io.StringIO(render_csv(build_report()))))
        assert len(full) == 4 and full[-1][-1].startswith("tagged do-not-optimize")

    def test_json_round_trip_and_write(self, tmp_path: Path) -> None:
        report = build_report()
        assert (
            Report.model_validate_json(render_json(report)) == report
            and json.loads(render_json(report))["as_of"] == "2026-09-19"
        )
        paths = write_reports(report, tmp_path / "out", ["md", "json", "csv"])
        assert [p.name for p in paths] == ["report.md", "report.json", "report.csv"]

    def test_write_errors(self, tmp_path: Path) -> None:
        with pytest.raises(ReportError, match="unknown format"):
            write_reports(build_report(), tmp_path, ["pdf"])
        blocker = tmp_path / "f"
        blocker.write_text("x", encoding="utf-8")
        with pytest.raises(ReportError):
            write_reports(build_report(), blocker / "sub", ["md"])

    def test_warnings(self) -> None:
        empty = build_report(resources=[])
        assert "the inventory is empty" in empty.warnings
        blind = build_report(
            resources=[
                vm("i-1"),
                vm("i-2", metrics={"observed_days": 5, "cpu_p95": 20}),
                vm("i-3", "z9.big", cost=100, metrics=STEADY),
            ]
        )
        text = " ".join(blind.warnings)
        assert (
            "no utilization data" in text
            and "fewer than 14 observed days" in text
            and "missing from the price catalogue (z9.big)" in text
            and "no 'env' tag" in text
        )


class TestSummary:
    def test_facts_are_aggregates(self) -> None:
        facts = facts_for(build_report())
        payload = facts.model_dump_json()
        assert (
            facts.blocked == 1
            and facts.recommendations == 2
            and "i-idle" not in payload
            and "data" not in payload.replace("date", "")
        )
        text = TemplateSummaryWriter().write(facts)
        assert "resources cost about" in text and "1 more are blocked" in text

    def test_llm_guard(self) -> None:
        facts = facts_for(build_report())
        expected = TemplateSummaryWriter().write(facts)

        class Good:
            def complete(self, system: str, user: str) -> str:
                return f"{facts.recommendations} recommendations found."

        class Invented:
            def complete(self, system: str, user: str) -> str:
                return "You will save 987654 dollars."

        class Down:
            def complete(self, system: str, user: str) -> str:
                raise ProviderError("down")

        assert (
            LLMSummaryWriter(Good()).write(facts)
            == f"{facts.recommendations} recommendations found."
        )
        assert (
            LLMSummaryWriter(Invented()).write(facts) == expected
            and LLMSummaryWriter(Down()).write(facts) == expected
        )

    def test_template_optional_sentences(self) -> None:
        base = SummaryFacts(
            resources=1,
            monthly_spend=100,
            identified_monthly_saving=10,
            identified_annual_saving=120,
            saving_pct=10.0,
            recommendations=1,
            blocked=0,
            anomalies=2,
            top_categories=[],
        )
        text = TemplateSummaryWriter().write(base)
        assert "2 cost anomaly" in text and "Largest sources" not in text and "blocked" not in text
