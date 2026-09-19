"""Sizing analyzers: rightsizing, non-production schedules and storage tiering."""

from __future__ import annotations

from cloudopt.analyzers.base import Context, confidence_for, make_rec
from cloudopt.models import Recommendation

HOURS_PER_WEEK = 168.0
# Share of cold data expected to be read back in a month. Used to charge retrieval fees against a tier move.
RETRIEVAL_ASSUMPTION = {"infrequent": 0.05, "archive": 0.01}


def find_rightsizing(ctx: Context) -> list[Recommendation]:
    """Smaller (or newer, cheaper) sizes that still keep the target headroom at the 95th percentile."""
    p = ctx.policy
    out: list[Recommendation] = []
    for r in ctx.resources:
        if r.type != "vm" or r.state != "running" or r.monthly_cost <= 0:
            continue
        m = r.metrics
        current = ctx.pricing.instance(r.provider, r.size)
        if current is None or m.cpu_p95 is None or m.observed_days < p.min_observed_days:
            continue
        if m.cpu_p95 > p.rightsize_max_cpu_p95 or m.cpu_p95 < p.idle_cpu_p95:
            continue
        need_vcpu = current.vcpu * m.cpu_p95 / p.target_cpu_util
        need_mem = (
            current.mem_gb * (m.mem_p95 / p.target_mem_util)
            if m.mem_p95 is not None
            else current.mem_gb
        )
        families = [current.family]
        if current.successor:
            successor = ctx.pricing.instance(r.provider, current.successor)
            if successor is not None:
                families.append(successor.family)
        options = [
            c
            for f in families
            if (c := ctx.pricing.cheapest_fit(r.provider, f, need_vcpu, need_mem)) is not None
        ]
        if not options:
            continue
        best = min(options, key=lambda c: (c.hourly, c.name))
        saving_pct = 100 * (1 - best.hourly / current.hourly)
        if best.name == current.name or saving_pct < p.rightsize_min_saving_pct:
            continue
        upgraded = best.family != current.family
        out.append(
            make_rec(
                "rightsize",
                r,
                title=f"Resize {r.name or r.id} from {current.name} to {best.name}",
                action=f"Change the instance type to {best.name}"
                + (" (newer generation)" if upgraded else ""),
                rationale=(
                    f"CPU peaks at {m.cpu_p95:g}% (95th percentile)"
                    + (f" and memory at {m.mem_p95:g}%" if m.mem_p95 is not None else "")
                    + f" over {m.observed_days} days. {best.name} keeps the load under {p.target_cpu_util:g}% CPU."
                ),
                fraction=1 - best.hourly / current.hourly,
                confidence=confidence_for(m.observed_days, p.min_observed_days),
                risk="medium" if ctx.is_prod(r) else "low",
                effort="medium",
                checks=[
                    "Confirm peak periods such as month-end and batch jobs fall inside the observation window",
                    "Resize in a maintenance window because the instance restarts",
                    "Watch CPU and memory for a week and keep the old size ready to restore",
                ],
                details={
                    "from": current.name,
                    "to": best.name,
                    "cpu_p95": m.cpu_p95,
                    "mem_p95": m.mem_p95,
                    "newer_generation": upgraded,
                },
            )
        )
    return out


def find_schedules(ctx: Context) -> list[Recommendation]:
    """Non-production compute that runs all week but is used a fraction of it."""
    p = ctx.policy
    out: list[Recommendation] = []
    for r in ctx.resources:
        if (
            r.type not in ("vm", "database")
            or r.state != "running"
            or r.monthly_cost <= 0
            or not ctx.is_nonprod(r)
        ):
            continue
        m = r.metrics
        if m.active_hours_per_week is None or m.observed_days < p.min_observed_days:
            continue
        if m.active_hours_per_week > p.schedule_max_active_hours:
            continue
        kept = max(p.business_hours_per_week, m.active_hours_per_week)
        fraction = 1 - kept / HOURS_PER_WEEK
        if fraction <= 0.05:
            continue
        env = r.tags.get(p.env_tag, "")
        out.append(
            make_rec(
                "schedule",
                r,
                title=f"Schedule {r.name or r.id} ({env})",
                action=f"Stop it outside about {kept:g} hours a week (weekday business hours) and start it again on a schedule",
                rationale=f"It is used about {m.active_hours_per_week:g} hours a week but runs all {HOURS_PER_WEEK:g}.",
                fraction=fraction,
                confidence=confidence_for(m.observed_days, p.min_observed_days),
                risk="low",
                effort="low",
                checks=[
                    "Tell the team the hours it will be available",
                    "Provide a way to start it on demand",
                    "Check for overnight jobs or scheduled tests that need it",
                ],
                details={
                    "active_hours_per_week": m.active_hours_per_week,
                    "scheduled_hours_per_week": kept,
                },
            )
        )
    return out


def find_storage_tiering(ctx: Context) -> list[Recommendation]:
    """Buckets whose data has not been read in a long time."""
    p = ctx.policy
    out: list[Recommendation] = []
    for r in ctx.resources:
        if r.type != "bucket" or not r.size_gb or r.metrics.last_accessed_days_ago is None:
            continue
        idle = r.metrics.last_accessed_days_ago
        tier_name = (
            "archive"
            if idle >= p.cold_days_archive
            else "infrequent"
            if idle >= p.cold_days_infrequent
            else ""
        )
        if not tier_name:
            continue
        standard = ctx.pricing.tier(r.provider, "standard")
        target = ctx.pricing.tier(r.provider, tier_name)
        if standard is None or target is None:
            continue
        base = r.monthly_cost if r.monthly_cost > 0 else r.size_gb * standard.per_gb_month
        expected_read = RETRIEVAL_ASSUMPTION[tier_name]
        net = (
            r.size_gb * (standard.per_gb_month - target.per_gb_month)
            - r.size_gb * expected_read * target.retrieval_per_gb
        )
        if net < 1.0 or base <= 0:
            continue
        out.append(
            make_rec(
                "storage_tier",
                r,
                title=f"Move {r.name or r.id} to {tier_name} storage",
                action=f"Add a lifecycle rule that moves objects to the {tier_name} tier",
                rationale=f"Nothing in the bucket has been read for {idle} days. {r.size_gb:,.0f} GB at {standard.per_gb_month} to {target.per_gb_month} per GB-month, allowing for {expected_read:.0%} being read back.",
                fraction=min(0.95, net / base),
                confidence="high" if idle >= 2 * p.cold_days_infrequent else "medium",
                risk="low",
                effort="low",
                checks=[
                    "Check minimum storage durations and early-deletion fees for the tier",
                    "Check retrieval time for the archive tier against your recovery needs",
                ],
                details={
                    "tier": tier_name,
                    "size_gb": r.size_gb,
                    "net_monthly": round(net, 2),
                    "idle_days": idle,
                },
            )
        )
    return out
