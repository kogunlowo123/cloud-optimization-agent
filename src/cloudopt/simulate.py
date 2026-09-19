"""Deterministic synthetic estate for demos and tests.

A small multi-cloud estate with the usual problems: an oversized production fleet, idle development machines,
forgotten disks, snapshots, addresses and load balancers, cold storage in the hot tier, non-production compute
that runs around the clock, steady production compute that could be committed, a protected legacy machine, and
a GPU instance that started running eight days ago and quietly added a cost spike. Thirty days of billing
accompany the inventory. All figures are invented.
"""

from __future__ import annotations

import json
import random
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from cloudopt.pricing import HOURS_PER_MONTH, Pricing

Records = dict[str, list[dict[str, Any]]]
BILLING_DAYS = 30
SERVICE_OF = {
    "vm": "compute",
    "database": "database",
    "disk": "storage",
    "snapshot": "storage",
    "bucket": "storage",
    "load_balancer": "networking",
    "public_ip": "networking",
    "nat_gateway": "networking",
}


def simulate(*, today: date, seed: int = 7) -> Records:
    """Records keyed by ``inventory`` and ``billing``."""
    rng = random.Random(seed)  # noqa: S311  (reproducible synthetic data, not security)
    pricing = Pricing.default()
    inventory: list[dict[str, Any]] = []

    def created(days_ago: int) -> str:
        return datetime.combine(
            today - timedelta(days=days_ago), time(9, 0), tzinfo=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

    def add(
        rid: str,
        rtype: str,
        provider: str,
        *,
        cost: float,
        tags: dict[str, str] | None = None,
        **fields: Any,
    ) -> None:
        inventory.append(
            {
                "id": rid,
                "name": fields.pop("name", rid),
                "type": rtype,
                "provider": provider,
                "monthly_cost": round(cost, 2),
                "tags": tags or {},
                **fields,
            }
        )

    def vm(rid: str, provider: str, size: str, tags: dict[str, str] | None, **fields: Any) -> None:
        item = pricing.instance(provider, size)
        assert item is not None
        add(
            rid,
            "vm",
            provider,
            cost=0.0 if fields.get("state") == "stopped" else item.hourly * HOURS_PER_MONTH,
            size=size,
            state=fields.pop("state", "running"),
            region=fields.pop("region", "us-east-1"),
            created=created(fields.pop("age", 400)),
            tags=tags,
            **fields,
        )

    prod = {"env": "prod", "team": "platform", "app": "web"}
    for n in range(1, 7):
        vm(
            f"i-web{n:02d}",
            "aws",
            "m5.2xlarge",
            prod,
            name=f"web-{n:02d}",
            metrics={
                "observed_days": 30,
                "cpu_avg": 14,
                "cpu_p95": 22,
                "mem_avg": 20,
                "mem_p95": 30,
            },
        )
    api = {"env": "prod", "team": "platform", "app": "api"}
    for n in range(1, 5):
        vm(
            f"i-api{n:02d}",
            "aws",
            "c5.2xlarge",
            api,
            name=f"api-{n:02d}",
            metrics={
                "observed_days": 30,
                "cpu_avg": 40,
                "cpu_p95": 55,
                "mem_avg": 45,
                "mem_p95": 60,
            },
        )
    add(
        "db-orders",
        "database",
        "aws",
        cost=620,
        size="db.r5.2xlarge",
        state="running",
        region="us-east-1",
        tags={"env": "prod", "team": "orders", "app": "orders"},
        metrics={
            "observed_days": 30,
            "cpu_p95": 12,
            "connections_per_day": 400,
            "net_gb_per_day": 4,
        },
    )
    for n in range(1, 4):
        vm(
            f"i-dev{n:02d}",
            "aws",
            "m5.xlarge",
            {"env": "dev", "team": "data", "app": "etl"},
            name=f"dev-sandbox-{n:02d}",
            metrics={
                "observed_days": 30,
                "cpu_avg": 0.8,
                "cpu_p95": 2.1,
                "mem_p95": 8,
                "net_gb_per_day": 0.05,
            },
        )
    for n, env in enumerate(("staging", "staging", "test", "test"), start=1):
        vm(
            f"i-test{n:02d}",
            "aws",
            "m5.large",
            {"env": env, "team": "qa", "app": "web"},
            name=f"{env}-{n:02d}",
            metrics={
                "observed_days": 30,
                "cpu_avg": 20,
                "cpu_p95": 35,
                "mem_p95": 50,
                "active_hours_per_week": 40,
            },
        )
    vm(
        "i-unlabelled",
        "aws",
        "m5.xlarge",
        None,
        name="unlabelled-worker",
        metrics={"observed_days": 30, "cpu_avg": 25, "cpu_p95": 40, "mem_p95": 55},
    )
    vm(
        "i-legacy",
        "aws",
        "m5.xlarge",
        {"env": "prod", "team": "finance", "app": "billing", "do-not-optimize": "legacy-billing"},
        name="legacy-billing",
        metrics={"observed_days": 30, "cpu_p95": 1.5, "net_gb_per_day": 0.02},
    )
    vm(
        "i-stopped",
        "aws",
        "m5.large",
        {"env": "dev", "team": "data", "app": "etl"},
        name="old-etl",
        state="stopped",
        state_days=90,
        metrics={"observed_days": 30},
    )
    vm(
        "i-gpu",
        "aws",
        "g5.2xlarge",
        {"env": "dev", "team": "ml", "app": "experiments"},
        name="ml-experiment-07",
        state_days=8,
        age=8,
        metrics={
            "observed_days": 8,
            "cpu_avg": 0.5,
            "cpu_p95": 1.0,
            "mem_p95": 5,
            "net_gb_per_day": 0.01,
        },
    )
    for n in range(1, 4):
        vm(
            f"vm-az{n}",
            "azure",
            "Standard_D4s_v4",
            {"env": "prod", "team": "platform", "app": "portal"},
            name=f"portal-{n}",
            region="eastus",
            metrics={"observed_days": 30, "cpu_p95": 62, "mem_p95": 68},
        )
    for n in range(1, 3):
        vm(
            f"gce-batch{n}",
            "gcp",
            "n2-standard-8",
            {"env": "prod", "team": "data", "app": "batch"},
            name=f"batch-{n}",
            region="us-central1-a",
            metrics={"observed_days": 30, "cpu_p95": 65, "mem_p95": 70},
        )
    vm(
        "gce-idle",
        "gcp",
        "n2-standard-4",
        {"env": "dev", "team": "data", "app": "etl"},
        name="idle-dev",
        region="us-central1-a",
        metrics={"observed_days": 30, "cpu_p95": 3.0, "net_gb_per_day": 0.1},
    )

    for n in range(1, 4):
        add(
            f"vol-web{n}",
            "disk",
            "aws",
            cost=100 * 0.08,
            size="gp3",
            state="attached",
            attached_to=f"i-web{n:02d}",
            size_gb=100,
            tags=prod,
        )
    for n, days in enumerate((45, 60, 120, 200, 35), start=1):
        add(
            f"vol-lost{n}",
            "disk",
            "aws",
            cost=500 * 0.08,
            size="gp3",
            state="unattached",
            state_days=days,
            size_gb=500,
            tags={"env": "dev", "team": "data"},
            created=created(days + 100),
        )
    for n in range(1, 3):
        add(
            f"vol-old{n}",
            "disk",
            "aws",
            cost=200 * 0.08,
            size="gp3",
            state="attached",
            attached_to="i-stopped",
            size_gb=200,
            tags={"env": "dev", "team": "data"},
        )
    for n, age in enumerate((200, 260, 300, 340, 380, 420), start=1):
        add(
            f"snap-orphan{n}",
            "snapshot",
            "aws",
            cost=300 * 0.05,
            state="available",
            size_gb=300,
            parent=f"vol-deleted{n}",
            created=created(age),
            tags={"team": "data"},
        )
    for n in range(1, 4):
        add(
            f"snap-live{n}",
            "snapshot",
            "aws",
            cost=100 * 0.05,
            state="available",
            size_gb=100,
            parent=f"vol-web{n}",
            created=created(120),
            tags={"env": "prod", "team": "platform"},
        )
    for n in range(1, 5):
        add(
            f"eip-{n}",
            "public_ip",
            "aws",
            cost=3.65,
            state="unassociated",
            state_days=40 + n,
            tags={"team": "platform"},
        )
    add(
        "alb-old-1",
        "load_balancer",
        "aws",
        cost=22,
        state="active",
        metrics={"observed_days": 30, "connections_per_day": 0},
        tags={"env": "dev", "team": "platform"},
    )
    add(
        "alb-old-2",
        "load_balancer",
        "aws",
        cost=22,
        state="active",
        metrics={"observed_days": 30, "connections_per_day": 0.2},
        tags={"env": "test", "team": "platform"},
    )
    add(
        "alb-prod",
        "load_balancer",
        "aws",
        cost=45,
        state="active",
        metrics={"observed_days": 30, "connections_per_day": 2_400_000},
        tags=prod,
    )
    add(
        "nat-dev",
        "nat_gateway",
        "aws",
        cost=33,
        state="active",
        metrics={"observed_days": 30, "net_gb_per_day": 0.02},
        tags={"env": "dev", "team": "platform"},
    )
    add(
        "nat-prod",
        "nat_gateway",
        "aws",
        cost=33 + 90,
        state="active",
        metrics={"observed_days": 30, "net_gb_per_day": 80},
        tags=prod,
    )
    add(
        "s3-logs-archive",
        "bucket",
        "aws",
        cost=40_000 * 0.023,
        size_gb=40_000,
        state="active",
        metrics={"last_accessed_days_ago": 400},
        tags={"team": "security", "app": "logging"},
    )
    add(
        "s3-backups-old",
        "bucket",
        "aws",
        cost=8_000 * 0.023,
        size_gb=8_000,
        state="active",
        metrics={"last_accessed_days_ago": 120},
        tags={"team": "data", "app": "backups"},
    )
    add(
        "s3-assets",
        "bucket",
        "aws",
        cost=500 * 0.023,
        size_gb=500,
        state="active",
        metrics={"last_accessed_days_ago": 0},
        tags={"env": "prod", "team": "platform", "app": "web"},
    )

    billing: list[dict[str, Any]] = []
    for offset in range(BILLING_DAYS, 0, -1):
        day = today - timedelta(days=offset)
        for item in inventory:
            if item["monthly_cost"] <= 0:
                continue
            daily = item["monthly_cost"] / 30.4375
            start_offset = (item.get("state_days") or 0) if item["id"] == "i-gpu" else 0
            if item["id"] == "i-gpu" and offset > start_offset:
                continue
            noise = 1 + rng.uniform(-0.03, 0.03)
            billing.append(
                {
                    "day": day.isoformat(),
                    "resource_id": item["id"],
                    "service": SERVICE_OF[item["type"]],
                    "cost": round(daily * noise, 4),
                    "tags": item["tags"],
                }
            )
    return {"inventory": inventory, "billing": billing}


def write_records(records: Records, directory: Path) -> list[Path]:
    """Write ``inventory.jsonl`` and ``billing.jsonl`` into ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, items in records.items():
        path = directory / f"{name}.jsonl"
        path.write_text("".join(json.dumps(item) + "\n" for item in items), encoding="utf-8")
        paths.append(path)
    return paths
