"""Planner: turn candidate recommendations into a plan whose savings add up.

Recommendations for one resource interact. A deleted instance cannot also be rightsized, and a rightsized
instance that is then scheduled saves less than either alone. The planner applies changes in a fixed order and
computes each saving against what is left after the earlier ones, so the total never double counts. Commitments
come last and only cover spend that is still running and steady.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from cloudopt.analyzers.base import Context, make_rec
from cloudopt.commands import commands_for
from cloudopt.config import Policy
from cloudopt.models import CATEGORY_ORDER, Recommendation, Resource, Totals
from cloudopt.pricing import HOURS_PER_MONTH, Pricing


def protection_reason(resource: Resource, policy: Policy) -> str:
    """Why this resource must not be changed, or an empty string."""
    if resource.id in policy.protected_ids:
        return "listed in protected_ids"
    for key, wanted in policy.protect_tags.items():
        value = resource.tags.get(key)
        if value is not None and (wanted == "*" or value.lower() == wanted.lower()):
            return f"tagged {key}={value}"
    return ""


def plan(
    ctx: Context,
    candidates: list[Recommendation],
) -> tuple[list[Recommendation], list[Recommendation], Totals]:
    """Order, de-duplicate and price the candidates.

    Returns the active plan, the recommendations blocked by protection, and the totals.
    """
    policy = ctx.policy
    by_resource: dict[str, list[Recommendation]] = defaultdict(list)
    for rec in candidates:
        by_resource[rec.resource_id].append(rec)

    residual = {r.id: r.monthly_cost for r in ctx.resources}
    active: list[Recommendation] = []
    blocked: list[Recommendation] = []
    scheduled: set[str] = set()
    removed: set[str] = set()

    for resource_id, recs in sorted(by_resource.items()):
        resource = ctx.by_id[resource_id]
        recs = sorted(recs, key=lambda r: (CATEGORY_ORDER[r.category], r.id))
        reason = protection_reason(resource, policy)
        if reason:
            for rec in recs:
                blocked.append(_with_commands(rec.model_copy(update={"blocked": reason}), resource))
            if any(r.category == "waste" for r in recs):
                removed.add(
                    resource_id
                )  # do not commit to capacity that looks idle, even if it may not be touched
            continue
        if any(r.category == "waste" for r in recs):
            recs = [r for r in recs if r.category == "waste"][:1]
        for rec in recs:
            fraction = float(rec.details["fraction"])
            before = residual[resource_id]
            saving = round(before * fraction, 2)
            residual[resource_id] = round(before - saving, 2)
            active.append(
                _with_commands(
                    rec.model_copy(
                        update={"current_monthly": round(before, 2), "monthly_saving": saving}
                    ),
                    resource,
                )
            )
            if rec.category == "waste":
                removed.add(resource_id)
            if rec.category == "schedule":
                scheduled.add(resource_id)

    active.extend(_commitments(ctx, residual, removed, scheduled))
    active.sort(key=lambda r: (-r.monthly_saving, CATEGORY_ORDER[r.category], r.id))
    blocked.sort(key=lambda r: (-r.monthly_saving, r.id))

    by_category: dict[str, float] = defaultdict(float)
    for rec in active:
        by_category[rec.category] += rec.monthly_saving
    spend = round(sum(r.monthly_cost for r in ctx.resources), 2)
    saving = round(sum(by_category.values()), 2)
    totals = Totals(
        resources=len(ctx.resources),
        monthly_spend=spend,
        identified_saving=saving,
        saving_pct=round(100 * saving / spend, 1) if spend else 0.0,
        by_category={
            k: round(v, 2)
            for k, v in sorted(by_category.items(), key=lambda kv: CATEGORY_ORDER[kv[0]])
        },
        blocked_saving=round(sum(r.monthly_saving for r in blocked), 2),
    )
    return active, blocked, totals


def _with_commands(rec: Recommendation, resource: Resource) -> Recommendation:
    return rec.model_copy(update={"commands": commands_for(rec, resource)})


def _commitments(
    ctx: Context, residual: dict[str, float], removed: set[str], scheduled: set[str]
) -> list[Recommendation]:
    """One commitment recommendation per provider, sized from steady spend that remains after the other changes."""
    policy = ctx.policy
    pricing: Pricing = ctx.pricing
    today: date = ctx.today
    steady: dict[str, list[Resource]] = defaultdict(list)
    for r in ctx.resources:
        if (
            r.type != "vm"
            or r.state != "running"
            or r.id in removed
            or r.id in scheduled
            or residual.get(r.id, 0) <= 0
        ):
            continue
        if r.metrics.observed_days < policy.commitment_min_observed_days:
            continue
        steady[r.provider].append(r)

    out: list[Recommendation] = []
    for provider, vms in sorted(steady.items()):
        discount = pricing.commitment_discount.get(provider, {}).get(policy.commitment_term)
        if not discount:
            continue
        eligible = sum(residual[r.id] for r in vms)
        commit = round(eligible * policy.commitment_coverage, 2)
        saving = round(commit * discount, 2)
        if saving <= 0:
            continue
        observed = min(r.metrics.observed_days for r in vms)
        placeholder = vms[0]
        rec = make_rec(
            "commitment",
            placeholder,
            title=f"Commit to {policy.commitment_term} of steady {provider} compute",
            action=f"Buy a {policy.commitment_term} compute commitment of about ${commit / HOURS_PER_MONTH:,.2f} an hour ({policy.commitment_coverage:.0%} of ${eligible:,.0f} a month of steady spend)",
            rationale=(
                f"{len(vms)} running instances have been observed for at least {observed} days. Sizing runs after "
                f"deletions, rightsizing and schedules so the commitment covers only what stays. The {provider} "
                f"{policy.commitment_term} discount is assumed to be {discount:.0%}."
            ),
            fraction=1.0,
            confidence="high" if observed >= 2 * policy.commitment_min_observed_days else "medium",
            risk="medium",
            effort="low",
            checks=[
                "Finish rightsizing and deletions before buying, so you do not commit to capacity you plan to remove",
                "Confirm the workloads will run for the full term, and prefer a flexible commitment type",
                "Check the discount against your actual contract and start with the shorter term",
            ],
            details={
                "provider": provider,
                "instances": len(vms),
                "eligible_monthly": round(eligible, 2),
                "commit_monthly": commit,
                "commit_hourly": round(commit / HOURS_PER_MONTH, 4),
                "discount": discount,
                "term": policy.commitment_term,
                "as_of": today.isoformat(),
            },
        )
        out.append(
            rec.model_copy(
                update={
                    "id": f"R-COMMIT-{provider.upper()}",
                    "resource_id": f"fleet:{provider}",
                    "resource_name": f"{provider} compute fleet",
                    "current_monthly": round(eligible, 2),
                    "monthly_saving": saving,
                }
            )
        )
    return out
