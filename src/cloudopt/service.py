"""Application service: load data, run the analyzers, plan, and report."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from cloudopt.analyzers.base import Context
from cloudopt.analyzers.billing import allocate, find_anomalies
from cloudopt.analyzers.sizing import find_rightsizing, find_schedules, find_storage_tiering
from cloudopt.analyzers.waste import find_waste
from cloudopt.config import Policy, Settings
from cloudopt.ingest import parse_billing, parse_inventory, read_lines
from cloudopt.models import DAYS_PER_MONTH, BillingLine, IngestReport, Report, Resource
from cloudopt.planner import plan
from cloudopt.pricing import Pricing
from cloudopt.summary import SummaryWriter, facts_for

RUN_RATE_TOLERANCE = 0.25
UNTAGGED_WARNING_SHARE = 0.2


class OptimizationService:
    """Facade used by the CLI and library callers."""

    def __init__(self, settings: Settings, summary_writer: SummaryWriter) -> None:
        self._settings = settings
        self._summary = summary_writer

    def load(
        self, inventory: Path, billing: Path | None = None
    ) -> tuple[list[Resource], list[BillingLine], list[IngestReport]]:
        """Read the inventory and, optionally, the billing export.

        Raises:
            DataError: If a file cannot be read or has too many records.
        """
        s = self._settings
        resources, inv_report = parse_inventory(
            read_lines(inventory), max_line_bytes=s.max_line_bytes, max_records=s.max_records
        )
        reports = [inv_report]
        lines: list[BillingLine] = []
        if billing is not None:
            lines, bill_report = parse_billing(
                read_lines(billing), max_line_bytes=s.max_line_bytes, max_records=s.max_records
            )
            reports.append(bill_report)
        return resources, lines, reports

    def analyze(
        self,
        resources: list[Resource],
        billing: list[BillingLine],
        *,
        policy: Policy,
        pricing: Pricing,
        today: date | None = None,
    ) -> Report:
        """Find savings, resolve overlaps between them, and describe cost anomalies and allocation."""
        as_of = today or datetime.now(timezone.utc).date()
        ctx = Context(resources, policy, pricing, as_of)
        waste, blind = find_waste(ctx)
        candidates = [
            *waste,
            *find_rightsizing(ctx),
            *find_schedules(ctx),
            *find_storage_tiering(ctx),
        ]
        recommendations, blocked, totals = plan(ctx, candidates)
        report = Report(
            as_of=as_of,
            billing_days=len({line.day for line in billing}),
            totals=totals,
            recommendations=recommendations,
            blocked=blocked,
            anomalies=find_anomalies(billing, policy),
            allocation=allocate(billing, resources, policy),
            warnings=self._warnings(ctx, billing, blind),
        )
        return report.model_copy(update={"summary": self._summary.write(facts_for(report))})

    @staticmethod
    def _warnings(ctx: Context, billing: list[BillingLine], blind: int) -> list[str]:
        p = ctx.policy
        warnings: list[str] = []
        if not ctx.resources:
            warnings.append("the inventory is empty")
        if blind:
            warnings.append(
                f"{blind} resource(s) have no utilization data, so idle detection skipped them"
            )
        short = [
            r
            for r in ctx.resources
            if r.type in ("vm", "database")
            and r.state == "running"
            and 0 < r.metrics.observed_days < p.min_observed_days
        ]
        if short:
            warnings.append(
                f"{len(short)} running resource(s) have fewer than {p.min_observed_days} observed days, so rightsizing and idle checks ignore them"
            )
        unknown = [
            r
            for r in ctx.resources
            if r.type == "vm" and r.size and ctx.pricing.instance(r.provider, r.size) is None
        ]
        if unknown:
            sizes = ", ".join(sorted({r.size for r in unknown})[:5])
            warnings.append(
                f"{len(unknown)} instance(s) use sizes missing from the price catalogue ({sizes}), so they cannot be rightsized"
            )
        untagged = [r for r in ctx.resources if r.monthly_cost > 0 and not r.tags.get(p.env_tag)]
        cost = sum(r.monthly_cost for r in ctx.resources)
        if cost and sum(r.monthly_cost for r in untagged) / cost >= UNTAGGED_WARNING_SHARE:
            warnings.append(
                f"{len(untagged)} resource(s) have no '{p.env_tag}' tag, so non-production savings may be missed"
            )
        if not billing:
            warnings.append(
                "no billing data was supplied, so anomalies and allocation were skipped"
            )
        else:
            days = len({line.day for line in billing})
            spend = sum(line.cost for line in billing)
            run_rate = spend / days * DAYS_PER_MONTH if days else 0.0
            if cost and abs(run_rate - cost) / cost > RUN_RATE_TOLERANCE:
                warnings.append(
                    f"inventory costs (${cost:,.0f} a month) and the billing run rate (${run_rate:,.0f} a month) differ by more than {RUN_RATE_TOLERANCE:.0%}"
                )
        return warnings
