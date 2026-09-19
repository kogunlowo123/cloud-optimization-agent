"""Shared fixtures and builders."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx

from cloudopt.analyzers.base import Context
from cloudopt.config import Policy, Settings
from cloudopt.container import build_service
from cloudopt.models import BillingLine, Resource
from cloudopt.pricing import Pricing
from cloudopt.providers.http import JsonClient
from cloudopt.service import OptimizationService

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIGS = REPO_ROOT / "configs"
TODAY = date(2026, 9, 19)


def res(rid: str = "r1", rtype: str = "vm", **fields: Any) -> Resource:
    """A resource with sensible defaults. ``metrics`` may be given as a dict."""
    data: dict[str, Any] = {
        "id": rid,
        "type": rtype,
        "provider": "aws",
        "monthly_cost": 100.0,
        "state": "running",
    }
    data.update(fields)
    return Resource.model_validate(data)


def vm(
    rid: str = "i-1", size: str = "m5.xlarge", *, cost: float | None = None, **fields: Any
) -> Resource:
    pricing = Pricing.default()
    item = pricing.instance(fields.get("provider", "aws"), size)
    price = cost if cost is not None else (item.hourly * 730 if item else 100.0)
    return res(rid, "vm", size=size, monthly_cost=price, **fields)


def make_ctx(
    resources: Sequence[Resource], policy: Policy | None = None, pricing: Pricing | None = None
) -> Context:
    return Context(list(resources), policy or Policy(), pricing or Pricing.default(), TODAY)


def bill(
    service: str,
    costs: Sequence[float],
    *,
    end: date = TODAY - timedelta(days=1),
    resource_id: str = "r1",
    tags: dict[str, str] | None = None,
) -> list[BillingLine]:
    """Daily lines for ``service`` ending on ``end``, one per cost."""
    start = end - timedelta(days=len(costs) - 1)
    return [
        BillingLine(
            day=start + timedelta(days=i),
            resource_id=resource_id,
            service=service,
            cost=c,
            tags=tags or {},
        )
        for i, c in enumerate(costs)
    ]


def make_settings(**overrides: object) -> Settings:
    """Settings that ignore the developer's environment."""
    base: dict[str, object] = {
        "retry_min_wait": 0.0,
        "retry_max_wait": 0.0,
        "log_level": "CRITICAL",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def make_service(**overrides: object) -> OptimizationService:
    return build_service(make_settings(**overrides))


def json_client(
    handler: Callable[[httpx.Request], httpx.Response], attempts: int = 2
) -> JsonClient:
    """A JsonClient backed by an in-process mock transport."""
    return JsonClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        attempts=attempts,
        min_wait=0.0,
        max_wait=0.0,
    )
