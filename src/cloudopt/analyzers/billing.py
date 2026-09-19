"""Billing analyzers: cost anomalies and allocation by tag."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import date, timedelta

from cloudopt.config import Policy
from cloudopt.models import (
    DAYS_PER_MONTH,
    Allocation,
    AllocationRow,
    Anomaly,
    BillingLine,
    Resource,
)
from cloudopt.stats import robust_zscore

BASELINE_DAYS = 28
UNTAGGED = "(untagged)"


def _daily(lines: list[BillingLine]) -> dict[date, float]:
    totals: dict[date, float] = defaultdict(float)
    for line in lines:
        totals[line.day] += line.cost
    return totals


def find_anomalies(lines: list[BillingLine], policy: Policy) -> list[Anomaly]:
    """Services whose most recent days cost far more than their own history.

    A service is flagged when its recent cost exceeds the baseline median by both the configured amount
    and the configured percentage, and is ``anomaly_z`` robust deviations away. A perfectly flat history counts as
    infinitely tight, so any real jump from it is flagged.
    """
    by_service: dict[str, list[BillingLine]] = defaultdict(list)
    for line in lines:
        by_service[line.service].append(line)

    anomalies: list[Anomaly] = []
    recent_n = policy.anomaly_recent_days
    for service, service_lines in sorted(by_service.items()):
        daily = _daily(service_lines)
        days = sorted(daily)
        if len(days) < policy.anomaly_min_days:
            continue
        recent_days = days[-recent_n:]
        history_days = days[:-recent_n][-BASELINE_DAYS:]
        if len(history_days) < policy.anomaly_min_days - recent_n:
            continue
        history = [daily[d] for d in history_days]
        baseline = statistics.median(history)
        recent = [daily[d] for d in recent_days]
        current = statistics.fmean(recent)
        limit = max(
            baseline + policy.anomaly_min_extra_daily,
            baseline * (1 + policy.anomaly_min_increase_pct / 100),
        )
        high = [d for d in recent_days if daily[d] >= limit]
        if not high:
            continue
        kind = "shift" if len(high) == recent_n else "spike"
        value = current if kind == "shift" else max(daily[d] for d in high)
        z = robust_zscore(value, history)
        if z == 0.0 and statistics.pstdev(history) == 0.0 and value != baseline:
            z = math.inf
        if z < policy.anomaly_z:
            continue
        peak = value
        anomalies.append(
            Anomaly(
                service=service,
                kind=kind,
                first_day=high[0],
                last_day=recent_days[-1],
                baseline_daily=round(baseline, 2),
                current_daily=round(peak, 2),
                extra_monthly=round((peak - baseline) * DAYS_PER_MONTH, 2),
                top_resources=_attribute(service_lines, recent_days, history_days),
            )
        )
    return sorted(anomalies, key=lambda a: (-a.extra_monthly, a.service))


def _attribute(
    lines: list[BillingLine], recent_days: list[date], history_days: list[date]
) -> list[dict[str, object]]:
    recent = set(recent_days)
    history = set(history_days)
    now: dict[str, float] = defaultdict(float)
    before: dict[str, float] = defaultdict(float)
    for line in lines:
        key = line.resource_id or "(unassigned)"
        if line.day in recent:
            now[key] += line.cost
        elif line.day in history:
            before[key] += line.cost
    deltas = [
        (key, now[key] / len(recent) - before.get(key, 0.0) / max(1, len(history))) for key in now
    ]
    top = sorted((d for d in deltas if d[1] > 0), key=lambda d: (-d[1], d[0]))[:3]
    return [{"resource_id": key, "extra_daily": round(delta, 2)} for key, delta in top]


def allocate(
    lines: list[BillingLine], resources: list[Resource], policy: Policy
) -> list[Allocation]:
    """Monthly cost per value of each allocation tag over the most recent window.

    A tag on the billing line wins, then the tag on the resource, otherwise the cost is untagged.
    """
    if not lines:
        return []
    last = max(line.day for line in lines)
    first = last - timedelta(days=policy.allocation_window_days - 1)
    window = [line for line in lines if first <= line.day <= last]
    span = (last - min(line.day for line in window)).days + 1
    scale = DAYS_PER_MONTH / max(1, span)
    tags_of = {r.id: r.tags for r in resources}
    out: list[Allocation] = []
    for dimension in policy.allocation_tags:
        totals: dict[str, float] = defaultdict(float)
        for line in window:
            value = (
                line.tags.get(dimension)
                or tags_of.get(line.resource_id, {}).get(dimension)
                or UNTAGGED
            )
            totals[value] += line.cost
        grand = sum(totals.values())
        rows = [
            AllocationRow(
                value=value,
                monthly_cost=round(cost * scale, 2),
                share=round(cost / grand, 4) if grand else 0.0,
            )
            for value, cost in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        untagged = totals.get(UNTAGGED, 0.0)
        out.append(
            Allocation(
                dimension=dimension,
                rows=rows,
                untagged_cost=round(untagged * scale, 2),
                untagged_share=round(untagged / grand, 4) if grand else 0.0,
            )
        )
    return out
