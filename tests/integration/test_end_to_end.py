"""End-to-end tests: the simulated estate through the service and the CLI."""

from __future__ import annotations

import json
import random
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from cloudopt.cli import main
from cloudopt.config import Policy, load_policy
from cloudopt.container import build_service
from cloudopt.errors import ConfigurationError, DataError
from cloudopt.models import Report
from cloudopt.pricing import Pricing, load_pricing
from cloudopt.service import OptimizationService
from cloudopt.simulate import simulate, write_records
from tests.conftest import CONFIGS, TODAY, make_service, make_settings


@pytest.fixture(scope="module")
def feeds(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("feeds")
    write_records(simulate(today=TODAY), directory)
    return directory


def analyze(
    feeds: Path,
    service: OptimizationService | None = None,
    policy: Policy | None = None,
    pricing: Pricing | None = None,
) -> Report:
    svc = service or make_service()
    resources, billing, _ = svc.load(feeds / "inventory.jsonl", feeds / "billing.jsonl")
    return svc.analyze(
        resources,
        billing,
        policy=policy or Policy(),
        pricing=pricing or Pricing.default(),
        today=TODAY,
    )


class TestSimulatedEstate:
    def test_headline_numbers(self, feeds: Path) -> None:
        r = analyze(feeds)
        assert (
            r.totals.resources == 59
            and len(r.recommendations) == 39
            and len(r.blocked) == 1
            and r.billing_days == 30
        )
        assert r.totals.monthly_spend == pytest.approx(
            8025.36, abs=0.5
        ) and r.totals.identified_saving == pytest.approx(3397.62, abs=1)
        assert set(r.totals.by_category) == {
            "waste",
            "rightsize",
            "schedule",
            "storage_tier",
            "commitment",
        }

    def test_savings_add_up_and_never_double_count(self, feeds: Path) -> None:
        r = analyze(feeds)
        assert r.totals.identified_saving == pytest.approx(
            sum(x.monthly_saving for x in r.recommendations), abs=0.05
        )
        assert sum(r.totals.by_category.values()) == pytest.approx(
            r.totals.identified_saving, abs=0.05
        )
        assert r.totals.identified_saving < r.totals.monthly_spend
        per_resource: dict[str, float] = {}
        for rec in r.recommendations:
            if not rec.resource_id.startswith("fleet:"):
                per_resource[rec.resource_id] = (
                    per_resource.get(rec.resource_id, 0) + rec.monthly_saving
                )
        costs = {}
        resources, _, _ = make_service().load(feeds / "inventory.jsonl")
        for res in resources:
            costs[res.id] = res.monthly_cost
        assert all(saved <= costs[rid] + 0.01 for rid, saved in per_resource.items())

    def test_expected_findings_by_type(self, feeds: Path) -> None:
        titles = [x.title for x in analyze(feeds).recommendations]
        joined = "\n".join(titles)
        for expected in (
            "Idle instance: dev-sandbox-01",
            "Idle instance: idle-dev",
            "Unattached disk: vol-lost3",
            "Orphaned snapshot: snap-orphan4",
            "Unassociated public address: eip-2",
            "Load balancer with no traffic: alb-old-1",
            "NAT gateway with no traffic: nat-dev",
            "Resize web-01 from m5.2xlarge to m7i.xlarge",
            "Schedule staging-01",
            "Move s3-logs-archive to archive storage",
            "Move s3-backups-old to infrequent storage",
            "Disk of a long-stopped instance: vol-old1",
        ):
            assert expected in joined, expected
        assert (
            "prod-db" not in joined
            and "db-orders" not in joined
            and "alb-prod" not in joined
            and "s3-assets" not in joined
            and "nat-prod" not in joined
        )

    def test_the_protected_instance_is_blocked_and_reported(self, feeds: Path) -> None:
        r = analyze(feeds)
        assert [b.resource_name for b in r.blocked] == ["legacy-billing"] and r.blocked[
            0
        ].blocked == "tagged do-not-optimize=legacy-billing"
        assert not any(x.resource_id == "i-legacy" for x in r.recommendations)
        commit = next(x for x in r.recommendations if x.id == "R-COMMIT-AWS")
        assert commit.details["instances"] == 11

    def test_the_short_lived_gpu_is_an_anomaly_not_a_waste_finding(self, feeds: Path) -> None:
        r = analyze(feeds)
        assert not any(x.resource_id == "i-gpu" for x in r.recommendations)
        [anomaly] = r.anomalies
        assert (
            anomaly.service == "compute"
            and anomaly.kind == "shift"
            and anomaly.top_resources[0]["resource_id"] == "i-gpu"
            and anomaly.extra_monthly > 800
        )
        assert any("fewer than 14 observed days" in w for w in r.warnings)

    def test_allocation(self, feeds: Path) -> None:
        r = analyze(feeds)
        team = next(a for a in r.allocation if a.dimension == "team")
        assert team.rows[0].value == "platform" and 0 < team.untagged_share < 0.05
        env = next(a for a in r.allocation if a.dimension == "env")
        assert env.untagged_share > 0.1 and sum(row.share for row in env.rows) == pytest.approx(
            1.0, abs=0.001
        )

    def test_pricing_and_policy_change_the_answer(self, feeds: Path) -> None:
        base = analyze(feeds)
        cheaper = analyze(feeds, pricing=load_pricing(CONFIGS / "pricing.example.yaml"))
        assert cheaper.totals.identified_saving != base.totals.identified_saving
        strict = analyze(feeds, policy=Policy(commitment_term="3y"))
        assert strict.totals.by_category["commitment"] > base.totals.by_category["commitment"]
        none = analyze(
            feeds,
            policy=Policy(
                protected_ids=[
                    r.resource_id
                    for r in base.recommendations
                    if not r.resource_id.startswith("fleet:")
                ]
            ),
        )
        assert none.recommendations == [] or all(
            x.category == "commitment" for x in none.recommendations
        )

    def test_deterministic(self, feeds: Path) -> None:
        assert analyze(feeds) == analyze(feeds)

    def test_invariants_hold_across_random_estates(self, tmp_path: Path) -> None:
        for seed in (1, 2, 3, 11, 42):
            directory = tmp_path / f"s{seed}"
            write_records(simulate(today=TODAY, seed=seed), directory)
            r = analyze(directory)
            assert 0 <= r.totals.identified_saving <= r.totals.monthly_spend
            assert all(
                x.monthly_saving >= 0 and x.monthly_saving <= x.current_monthly + 0.01
                for x in r.recommendations
            )
            assert len({x.id for x in r.recommendations}) == len(r.recommendations)

    def test_no_billing_still_finds_savings(self, feeds: Path) -> None:
        svc = make_service()
        resources, _, _ = svc.load(feeds / "inventory.jsonl")
        r = svc.analyze(resources, [], policy=Policy(), pricing=Pricing.default(), today=TODAY)
        assert (
            r.recommendations
            and r.anomalies == []
            and r.allocation == []
            and any("no billing data" in w for w in r.warnings)
        )

    def test_inventory_and_billing_disagreement_is_flagged(self, feeds: Path) -> None:
        svc = make_service()
        resources, billing, _ = svc.load(feeds / "inventory.jsonl", feeds / "billing.jsonl")
        inflated = [b.model_copy(update={"cost": b.cost * 3}) for b in billing]
        r = svc.analyze(
            resources, inflated, policy=Policy(), pricing=Pricing.default(), today=TODAY
        )
        assert any("billing run rate" in w for w in r.warnings)

    def test_load_errors(self, tmp_path: Path) -> None:
        with pytest.raises(DataError, match="cannot read"):
            make_service().load(tmp_path / "missing.jsonl")

    def test_hostile_inventory_cannot_inject_into_reports_or_commands(self, tmp_path: Path) -> None:
        evil = {
            "id": "vol-1; rm -rf /",
            "name": "<img src=x onerror=alert(1)> | pipe\n# injected",
            "type": "disk",
            "provider": "aws",
            "monthly_cost": 40,
            "state": "unattached",
            "state_days": 45,
            "size_gb": 500,
            "tags": {"team": "=cmd|' /C calc'!A0"},
        }
        path = tmp_path / "inv.jsonl"
        path.write_text(json.dumps(evil) + "\n", encoding="utf-8")
        svc = make_service()
        resources, _, _ = svc.load(path)
        report = svc.analyze(resources, [], policy=Policy(), pricing=Pricing.default(), today=TODAY)
        from cloudopt.reporting import render_csv, render_markdown

        md = render_markdown(report)
        assert "<img" not in md and "\n# injected" not in md and "'vol-1; rm -rf /'" in md
        assert all(
            not cell.startswith("=")
            for line in render_csv(report).splitlines()
            for cell in line.split(",")
        )


class TestLlmSummary:
    def _service(self, reply: str, seen: list[dict[str, object]]) -> OptimizationService:
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

        settings = make_settings(llm_provider="openai", openai_api_key=SecretStr("k"))
        return build_service(
            settings, http_client=httpx.Client(transport=httpx.MockTransport(handler))
        )

    def test_grounded_reply_and_prompt_contents(self, feeds: Path) -> None:
        seen: list[dict[str, object]] = []
        report = analyze(feeds, self._service("There are 59 resources.", seen))
        assert report.summary == "There are 59 resources."
        sent = json.dumps(seen[0])
        for word in ("web-01", "i-gpu", "legacy-billing", "platform"):
            assert word not in sent
        assert "8025" in sent

    def test_invented_numbers_fall_back(self, feeds: Path) -> None:
        report = analyze(feeds, self._service("You will save 999999 a month.", []))
        assert "999999" not in report.summary and "resources cost about" in report.summary

    @pytest.mark.parametrize("provider", ["openai", "anthropic"])
    def test_missing_keys_are_a_configuration_error(self, provider: str) -> None:
        with pytest.raises(ConfigurationError, match="API_KEY"):
            build_service(
                make_settings(llm_provider=provider, openai_api_key=None, anthropic_api_key=None)
            )


class TestCli:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        for name in ("CLOUDOPT_LLM_PROVIDER", "CLOUDOPT_PRICING_FILE", "CLOUDOPT_POLICY_FILE"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("CLOUDOPT_LOG_LEVEL", "CRITICAL")
        monkeypatch.chdir(tmp_path)

    def base(self, feeds: Path) -> list[str]:
        return [
            "analyze",
            "--inventory",
            str(feeds / "inventory.jsonl"),
            "--billing",
            str(feeds / "billing.jsonl"),
            "--today",
            "2026-09-19",
        ]

    def test_simulate_then_analyze(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        feeds = tmp_path / "feeds"
        assert main(["simulate", "--out", str(feeds), "--today", "2026-09-19"]) == 0
        capsys.readouterr()
        out = tmp_path / "out"
        assert main([*self.base(feeds), "--out", str(out)]) == 0
        printed = capsys.readouterr()
        assert (
            "39 recommendations would save about" in printed.out
            and "loaded 59 inventory records" in printed.err
        )
        assert {p.name for p in out.iterdir()} == {"report.md", "report.json", "report.csv"}
        assert (
            Report.model_validate_json(
                (out / "report.json").read_text(encoding="utf-8")
            ).totals.resources
            == 59
        )

    def test_output_formats(self, feeds: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(self.base(feeds)) == 0
        assert capsys.readouterr().out.startswith("# Cloud optimization report as of 2026-09-19")
        assert main([*self.base(feeds), "--format", "json"]) == 0
        assert json.loads(capsys.readouterr().out)["totals"]["resources"] == 59
        assert main([*self.base(feeds), "--format", "csv"]) == 0
        assert capsys.readouterr().out.startswith("id,category")

    def test_config_files(self, feeds: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert (
            main(
                [
                    *self.base(feeds),
                    "--pricing",
                    str(CONFIGS / "pricing.example.yaml"),
                    "--policy",
                    str(CONFIGS / "policy.example.yaml"),
                    "--format",
                    "json",
                ]
            )
            == 0
        )
        assert (
            load_policy(CONFIGS / "policy.example.yaml").commitment_coverage == 0.8
            and json.loads(capsys.readouterr().out)["recommendations"]
        )

    def test_gate(self, feeds: Path) -> None:
        assert main([*self.base(feeds), "--out", "o", "--fail-above-saving-pct", "50"]) == 0
        assert main([*self.base(feeds), "--out", "o", "--fail-above-saving-pct", "20"]) == 1

    def test_catalog(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["catalog"]) == 0
        out = capsys.readouterr().out
        assert "m5.xlarge" in out and "n2-standard-8" in out and "Standard_D4s_v4" in out

    def test_errors_exit_two(
        self, feeds: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["analyze", "--inventory", str(tmp_path / "missing.jsonl")]) == 2
        bad = tmp_path / "bad.yaml"
        bad.write_text("min_observed_days: 0\n", encoding="utf-8")
        assert main([*self.base(feeds), "--policy", str(bad)]) == 2
        assert main([*self.base(feeds), "--pricing", str(bad)]) == 2
        assert main([*self.base(feeds), "--out", str(tmp_path / "o"), "--format", "pdf"]) == 2
        assert capsys.readouterr().err.count("error:") == 4

    def test_bad_arguments(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as info:
            main(["analyze", "--inventory", str(tmp_path / "x"), "--today", "yesterday"])
        assert info.value.code == 2
        with pytest.raises(SystemExit):
            main(["analyze"])

    def test_bad_lines_are_reported_without_content(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "inv.jsonl"
        path.write_text(
            json.dumps({"id": "a", "type": "vm", "provider": "aws"})
            + "\nnot json password=Sup3rSecret\n",
            encoding="utf-8",
        )
        assert main(["analyze", "--inventory", str(path), "--format", "json"]) == 0
        err = capsys.readouterr().err
        assert "1 rejected" in err and "Sup3rSecret" not in err

    def test_llm_provider_without_key(
        self, feeds: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("CLOUDOPT_LLM_PROVIDER", "openai")
        monkeypatch.delenv("CLOUDOPT_OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert main(self.base(feeds)) == 2
        assert "API_KEY" in capsys.readouterr().err

    def test_settings_from_environment(
        self, feeds: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("CLOUDOPT_PRICING_FILE", str(CONFIGS / "pricing.example.yaml"))
        assert main([*self.base(feeds), "--format", "json"]) == 0
        env_total = json.loads(capsys.readouterr().out)["totals"]["identified_saving"]
        monkeypatch.delenv("CLOUDOPT_PRICING_FILE")
        assert main([*self.base(feeds), "--format", "json"]) == 0
        assert json.loads(capsys.readouterr().out)["totals"]["identified_saving"] != env_total


def test_random_noise_does_not_create_anomalies() -> None:
    from cloudopt.analyzers.billing import find_anomalies
    from tests.conftest import bill

    rng = random.Random(5)
    for _ in range(20):
        costs = [100 * (1 + rng.uniform(-0.04, 0.04)) for _ in range(31)]
        assert find_anomalies(bill("compute", costs), Policy()) == []
    assert TODAY - timedelta(days=1) > TODAY - timedelta(days=2)
