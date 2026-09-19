"""Unit tests for models, pricing, policy, ingestion and statistics."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from cloudopt.config import Policy, Settings, load_policy
from cloudopt.errors import ConfigurationError, DataError
from cloudopt.ingest import parse_billing, parse_inventory, read_lines
from cloudopt.models import BillingLine, Metrics, Recommendation, Resource
from cloudopt.pricing import Pricing, load_pricing
from cloudopt.security import csv_safe, md_cell, md_code, redact
from cloudopt.stats import percentile, robust_zscore
from tests.conftest import CONFIGS, TODAY, res


class TestModels:
    def test_resource_normalisation(self) -> None:
        r = Resource.model_validate(
            {
                "id": "x",
                "type": "vm",
                "provider": "aws",
                "state": " RUNNING ",
                "tags": {" Env ": " prod ", "Team": "a"},
                "created": "2026-01-01T00:00:00",
            }
        )
        assert (
            r.state == "running"
            and r.tags == {"env": "prod", "team": "a"}
            and r.created is not None
            and r.created.tzinfo == timezone.utc
        )
        assert r.age_days(TODAY) == 261 and res(created=None).age_days(TODAY) is None
        assert res(created=datetime(2027, 1, 1, tzinfo=timezone.utc)).age_days(TODAY) == 0

    @pytest.mark.parametrize(
        "bad",
        [
            {"id": ""},
            {"type": "spaceship"},
            {"provider": "oracle"},
            {"monthly_cost": -1},
            {"surprise": 1},
            {"metrics": {"cpu_p95": 120}},
            {"metrics": {"observed_days": -1}},
            {"size_gb": -5},
        ],
    )
    def test_resource_rejects_bad_input(self, bad: dict[str, Any]) -> None:
        with pytest.raises(ValueError):
            res(**bad)

    def test_tag_limits(self) -> None:
        many = {f"k{i}": "v" * 300 for i in range(80)}
        r = res(tags=many)
        assert len(r.tags) == 50 and all(len(v) <= 128 for v in r.tags.values())

    def test_billing_line_validation(self) -> None:
        assert BillingLine(day=date(2026, 1, 1), service="compute", cost=-2.5).cost == -2.5
        for bad in (float("nan"), float("inf")):
            with pytest.raises(ValueError):
                BillingLine(day=date(2026, 1, 1), service="compute", cost=bad)
        with pytest.raises(ValueError):
            BillingLine(day=date(2026, 1, 1), service="", cost=1)

    def test_recommendation_annual_saving(self) -> None:
        rec = Recommendation(
            id="R",
            category="waste",
            resource_id="r",
            resource_name="r",
            provider="aws",
            title="t",
            action="a",
            rationale="x",
            current_monthly=10,
            monthly_saving=3.333,
            confidence="high",
            risk="low",
            effort="low",
        )
        assert rec.annual_saving == 40.0 and Metrics().observed_days == 0


class TestPricing:
    def test_default_catalogue(self) -> None:
        p = Pricing.default()
        assert (
            p.instance("aws", "m5.xlarge") is not None
            and p.instance("aws", "nope") is None
            and p.instance("gcp", "m5.xlarge") is None
        )
        assert [i.name for i in p.family("aws", "m5")] == [
            "m5.large",
            "m5.xlarge",
            "m5.2xlarge",
            "m5.4xlarge",
        ]
        assert (
            p.instance("aws", "m5.large") is not None
            and p.instance("aws", "m5.large").successor == "m7i.large"
        )  # type: ignore[union-attr]
        assert p.tier("aws", "archive") is not None and p.tier("aws", "warm") is None

    @pytest.mark.parametrize(
        ("vcpu", "mem", "expected"),
        [
            (1, 4, "m5.large"),
            (2, 8, "m5.large"),
            (2.1, 8, "m5.xlarge"),
            (2, 9, "m5.xlarge"),
            (16, 64, "m5.4xlarge"),
            (0.1, 0.1, "m5.large"),
        ],
    )
    def test_cheapest_fit(self, vcpu: float, mem: float, expected: str) -> None:
        fit = Pricing.default().cheapest_fit("aws", "m5", vcpu, mem)
        assert fit is not None and fit.name == expected

    def test_no_fit_and_unknown_family(self) -> None:
        p = Pricing.default()
        assert (
            p.cheapest_fit("aws", "m5", 64, 8) is None
            and p.cheapest_fit("aws", "m5", 2, 500) is None
            and p.cheapest_fit("aws", "zz", 1, 1) is None
        )

    def test_example_pricing_file_overrides_and_adds(self) -> None:
        p = load_pricing(CONFIGS / "pricing.example.yaml")
        assert p.instance("aws", "m5.xlarge").hourly == 0.15  # type: ignore[union-attr]
        assert p.instance("aws", "m5.large") == Pricing.default().instance("aws", "m5.large")
        assert (
            p.commitment_discount["aws"]["1y"] == 0.31
            and p.tier("aws", "standard").per_gb_month == 0.021
        )  # type: ignore[union-attr]

    def test_no_file_gives_defaults(self) -> None:
        assert load_pricing(None).instances == Pricing.default().instances

    @pytest.mark.parametrize(
        ("content", "message"),
        [
            ("- a\n", "mapping"),
            ("surprise: 1\n", "unknown sections"),
            (
                "instances: [{provider: aws, name: x, family: x, vcpu: 0, mem_gb: 1, hourly: 1}]\n",
                "invalid pricing",
            ),
            ("instances: [{provider: aws, name: x}]\n", "invalid pricing"),
            ("commitment_discount: {aws: {1y: 1.5}}\n", "between 0 and 1"),
            ("commitment_discount: {aws: {1y: yes}}\n", "between 0 and 1"),
            ("disk_per_gb: {aws: -1}\n", "non-negative"),
            ("storage: {aws: {standard: 5}}\n", "invalid pricing"),
        ],
    )
    def test_invalid_pricing(self, tmp_path: Path, content: str, message: str) -> None:
        path = tmp_path / "p.yaml"
        path.write_text(content, encoding="utf-8")
        with pytest.raises(ConfigurationError, match=message):
            load_pricing(path)

    def test_unreadable_and_oversized(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="cannot read"):
            load_pricing(tmp_path / "missing.yaml")
        big = tmp_path / "big.yaml"
        big.write_text("a: " + "x" * 2_100_000, encoding="utf-8")
        with pytest.raises(ConfigurationError, match="larger"):
            load_pricing(big)
        empty = tmp_path / "empty.yaml"
        empty.write_text("", encoding="utf-8")
        assert load_pricing(empty).instances == Pricing.default().instances


class TestPolicy:
    def test_defaults_and_normalisation(self) -> None:
        p = Policy(
            env_tag=" ENV ",
            allocation_tags=[" Team ", ""],
            nonprod_envs=["DEV"],
            protect_tags={"Keep": "*"},
        )
        assert (
            p.env_tag == "env"
            and p.allocation_tags == ["team"]
            and p.nonprod_envs == ["dev"]
            and p.protect_tags == {"keep": "*"}
        )

    @pytest.mark.parametrize(
        "bad",
        [
            {"target_cpu_util": 0},
            {"commitment_coverage": 1.5},
            {"cold_days_archive": 30, "cold_days_infrequent": 90},
            {"commitment_term": "5y"},
            {"surprise": 1},
            {"anomaly_min_days": 2},
        ],
    )
    def test_invalid(self, bad: dict[str, Any]) -> None:
        with pytest.raises(ValueError):
            Policy(**bad)

    def test_load_policy(self, tmp_path: Path) -> None:
        assert load_policy(None) == Policy()
        assert load_policy(CONFIGS / "policy.example.yaml").min_observed_days == 14
        for content, message in (
            ("- a\n", "mapping"),
            ("min_observed_days: 0\n", "invalid policy"),
            ("surprise: 1\n", "invalid policy"),
        ):
            path = tmp_path / "p.yaml"
            path.write_text(content, encoding="utf-8")
            with pytest.raises(ConfigurationError, match=message):
                load_policy(path)
        empty = tmp_path / "e.yaml"
        empty.write_text("", encoding="utf-8")
        assert load_policy(empty) == Policy()
        with pytest.raises(ConfigurationError, match="cannot read"):
            load_policy(tmp_path / "missing.yaml")

    def test_settings(self) -> None:
        with pytest.raises(ValueError, match="retry_max_wait"):
            Settings(_env_file=None, retry_min_wait=5.0, retry_max_wait=1.0)  # type: ignore[call-arg]
        assert Settings(_env_file=CONFIGS.parent / ".env.example").llm_provider == "none"  # type: ignore[call-arg]


class TestIngest:
    def _inv(self, **kw: Any) -> str:
        record = {"id": "i-1", "type": "vm", "provider": "aws", "monthly_cost": 10}
        record.update(kw)
        return json.dumps(record)

    def test_inventory_counts_dedupes_and_never_echoes(self) -> None:
        lines = [
            self._inv(),
            self._inv(monthly_cost=99),
            "not json password=Sup3rSecret",
            "[1]",
            self._inv(id="i-2", type="spaceship"),
            "",
            self._inv(id="i-3"),
        ]
        resources, report = parse_inventory(lines)
        assert [r.id for r in resources] == ["i-1", "i-3"] and resources[0].monthly_cost == 10
        assert report.accepted == 2 and report.rejected == 4 and report.lines == 6
        assert any(
            "duplicate resource id" in e for e in report.errors
        ) and "Sup3rSecret" not in json.dumps(report.model_dump())

    def test_validation_messages_name_the_field_not_the_value(self) -> None:
        _, report = parse_inventory([self._inv(monthly_cost=-5)])
        assert report.errors == ["line 1: monthly_cost: Input should be greater than or equal to 0"]

    def test_limits(self) -> None:
        _, report = parse_inventory(["x" * 500, self._inv()], max_line_bytes=100)
        assert report.accepted == 1 and "exceeds 100 bytes" in report.errors[0]
        with pytest.raises(DataError, match="limit"):
            parse_inventory([self._inv(id=f"i-{n}") for n in range(5)], max_records=3)
        _, capped = parse_inventory(["nope"] * 40)
        assert len(capped.errors) == 20

    def test_billing(self) -> None:
        good = json.dumps(
            {
                "day": "2026-09-01",
                "service": "compute",
                "cost": 5,
                "resource_id": "i-1",
                "tags": {"Team": "a"},
            }
        )
        lines, report = parse_billing(
            [good, json.dumps({"day": "yesterday", "service": "compute", "cost": 1}), "{"]
        )
        assert (
            len(lines) == 1
            and lines[0].tags == {"team": "a"}
            and report.rejected == 2
            and report.kind == "billing"
        )

    def test_read_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "f.jsonl"
        path.write_bytes(b'{"a": 1}\n\xff\xfe\n')
        assert len(read_lines(path)) == 2
        with pytest.raises(DataError, match="cannot read"):
            read_lines(tmp_path / "missing.jsonl")


class TestStatsAndSecurity:
    def test_percentile(self) -> None:
        assert percentile([], 0.5) == 0.0 and percentile([7], 0.9) == 7
        assert percentile([1, 2, 3, 4, 5], 0.5) == 3 and percentile([1, 2, 3, 4], 0.5) == 2.5
        assert (
            percentile([10, 20], 0) == 10
            and percentile([10, 20], 1) == 20
            and percentile([10, 20], 5) == 20
        )

    def test_robust_zscore(self) -> None:
        assert robust_zscore(10, [10, 11, 9, 10, 12]) == pytest.approx(0.0, abs=0.01)
        assert robust_zscore(100, [10, 11, 9, 10, 12]) > 10
        assert robust_zscore(5, [1, 2]) == 0.0 and robust_zscore(9, [5, 5, 5, 5]) == 0.0
        assert robust_zscore(9, [5, 5, 5, 6, 4, 5, 5]) > 3

    def test_output_helpers(self) -> None:
        assert (
            md_cell("a|b\n<x>&") == "a\\|b &lt;x&gt;&amp;"
            and md_code("a `b` | c") == "`a 'b' \\| c`"
        )
        assert csv_safe("=1+1") == "'=1+1" and csv_safe("plain") == "plain"
        assert "hunter2xyz" not in redact("tool -password hunter2xyz")
