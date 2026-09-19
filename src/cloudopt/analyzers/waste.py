"""Waste: resources that cost money and do nothing."""

from __future__ import annotations

from cloudopt.analyzers.base import Context, confidence_for, make_rec
from cloudopt.models import Recommendation, Resource


def _snapshot_share(ctx: Context, resource: Resource) -> float:
    """Fraction of a disk's cost that a final snapshot would keep costing."""
    if not resource.size_gb or resource.monthly_cost <= 0:
        return 0.0
    price = ctx.pricing.snapshot_per_gb.get(resource.provider, 0.05)
    return min(0.9, resource.size_gb * price / resource.monthly_cost)


def find_waste(ctx: Context) -> tuple[list[Recommendation], int]:
    """Recommendations to remove idle or orphaned resources.

    Returns the recommendations and the number of resources skipped because they have no utilization data.
    """
    p = ctx.policy
    out: list[Recommendation] = []
    blind = 0
    for r in ctx.resources:
        if r.monthly_cost <= 0:
            continue
        if r.type in ("vm", "database"):
            m = r.metrics
            if r.state != "running":
                continue
            if m.cpu_p95 is None or m.observed_days < 1:
                blind += 1
                continue
            connections = m.connections_per_day if m.connections_per_day is not None else 0.0
            net = m.net_gb_per_day if m.net_gb_per_day is not None else 0.0
            if (
                m.observed_days >= p.min_observed_days
                and m.cpu_p95 < p.idle_cpu_p95
                and net < p.idle_net_gb_per_day
                and connections < 10
            ):
                noun = "database" if r.type == "database" else "instance"
                out.append(
                    make_rec(
                        "waste",
                        r,
                        title=f"Idle {noun}: {r.name or r.id}",
                        action=f"Stop the {noun}, keep a snapshot, and delete it after a grace period",
                        rationale=f"CPU stayed under {p.idle_cpu_p95:g}% at the 95th percentile for {m.observed_days} days, with about {net:.2f} GB a day of network traffic.",
                        fraction=1.0,
                        confidence=confidence_for(m.observed_days, p.min_observed_days),
                        risk="high" if r.type == "database" or ctx.is_prod(r) else "medium",
                        effort="low",
                        checks=[
                            "Ask the owner whether the workload is seasonal, on standby or used by a batch job",
                            "Take a snapshot or image before stopping",
                            "Stop first and delete only after a grace period with no complaints",
                        ],
                        details={
                            "cpu_p95": m.cpu_p95,
                            "net_gb_per_day": net,
                            "observed_days": m.observed_days,
                        },
                    )
                )
        elif r.type == "disk":
            days = r.state_days
            attached = ctx.by_id.get(r.attached_to)
            if (
                r.state in ("unattached", "available")
                and days is not None
                and days >= p.unattached_days
            ):
                out.append(
                    make_rec(
                        "waste",
                        r,
                        title=f"Unattached disk: {r.name or r.id}",
                        action="Snapshot the disk, then delete it",
                        rationale=f"The disk has been unattached for {days} days.",
                        fraction=1.0 - _snapshot_share(ctx, r),
                        confidence="high" if days >= 30 else "medium",
                        risk="low",
                        effort="low",
                        checks=[
                            "Confirm no one plans to reattach it",
                            "Keep the snapshot for the retention period you use for deleted volumes",
                        ],
                        details={
                            "unattached_days": days,
                            "snapshot_share": round(_snapshot_share(ctx, r), 3),
                        },
                    )
                )
            elif (
                attached is not None
                and attached.type == "vm"
                and attached.state == "stopped"
                and (attached.state_days or 0) >= p.stopped_days
            ):
                out.append(
                    make_rec(
                        "waste",
                        r,
                        title=f"Disk of a long-stopped instance: {r.name or r.id}",
                        action="Snapshot the disk and delete it, or delete the instance and its disk together",
                        rationale=f"The instance {attached.name or attached.id} has been stopped for {attached.state_days} days but its disk still bills.",
                        fraction=1.0 - _snapshot_share(ctx, r),
                        confidence="medium",
                        risk="medium",
                        effort="low",
                        checks=[
                            "Confirm the instance is not needed for disaster recovery",
                            "Take a snapshot before deleting",
                        ],
                        details={"stopped_days": attached.state_days, "instance": attached.id},
                    )
                )
        elif r.type == "snapshot":
            age = r.age_days(ctx.today)
            parent = ctx.by_id.get(r.parent) if r.parent else None
            orphaned = r.parent == "" or parent is None or parent.state == "deleted"
            if age is not None and age >= p.snapshot_age_days and orphaned:
                out.append(
                    make_rec(
                        "waste",
                        r,
                        title=f"Orphaned snapshot: {r.name or r.id}",
                        action="Delete the snapshot after confirming it is not a required backup",
                        rationale=f"The snapshot is {age} days old and its source resource no longer exists.",
                        fraction=1.0,
                        confidence="medium",
                        risk="medium",
                        effort="low",
                        checks=[
                            "Check retention and legal-hold requirements",
                            "Check whether an image or restore process depends on it",
                        ],
                        details={"age_days": age},
                    )
                )
        elif r.type == "public_ip":
            if r.state in ("unattached", "unassociated", "available") and (
                r.state_days is None or r.state_days >= 1
            ):
                out.append(
                    make_rec(
                        "waste",
                        r,
                        title=f"Unassociated public address: {r.name or r.id}",
                        action="Release the address",
                        rationale="The address is not attached to anything but is billed.",
                        fraction=1.0,
                        confidence="high",
                        risk="low",
                        effort="low",
                        checks=[
                            "Check whether any DNS record, allowlist or partner still points at the address"
                        ],
                    )
                )
        elif r.type == "load_balancer":
            m = r.metrics
            if m.connections_per_day is None or m.observed_days < 1:
                blind += 1
            elif (
                m.observed_days >= p.min_observed_days
                and m.connections_per_day < p.idle_connections_per_day
            ):
                out.append(
                    make_rec(
                        "waste",
                        r,
                        title=f"Load balancer with no traffic: {r.name or r.id}",
                        action="Delete the load balancer after confirming nothing routes to it",
                        rationale=f"About {m.connections_per_day:g} connections a day over {m.observed_days} days.",
                        fraction=1.0,
                        confidence=confidence_for(m.observed_days, p.min_observed_days),
                        risk="medium",
                        effort="low",
                        checks=[
                            "Check DNS records and health-check configurations that reference it"
                        ],
                        details={"connections_per_day": m.connections_per_day},
                    )
                )
        elif r.type == "nat_gateway":
            m = r.metrics
            if m.net_gb_per_day is None or m.observed_days < 1:
                blind += 1
            elif m.observed_days >= p.min_observed_days and m.net_gb_per_day < 0.1:
                out.append(
                    make_rec(
                        "waste",
                        r,
                        title=f"NAT gateway with no traffic: {r.name or r.id}",
                        action="Delete the gateway and its route entries",
                        rationale=f"About {m.net_gb_per_day:.2f} GB a day over {m.observed_days} days.",
                        fraction=1.0,
                        confidence=confidence_for(m.observed_days, p.min_observed_days),
                        risk="medium",
                        effort="medium",
                        checks=[
                            "Confirm no private subnet routes through it",
                            "Confirm no scheduled job needs outbound access",
                        ],
                        details={"net_gb_per_day": m.net_gb_per_day},
                    )
                )
    return out, blind
