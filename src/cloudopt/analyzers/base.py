"""Shared context and helpers for analyzers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from cloudopt.config import Policy
from cloudopt.models import Category, Level, Recommendation, Resource
from cloudopt.pricing import Pricing


@dataclass
class Context:
    """Inputs every analyzer sees."""

    resources: list[Resource]
    policy: Policy
    pricing: Pricing
    today: date
    by_id: dict[str, Resource] = field(init=False)

    def __post_init__(self) -> None:
        self.by_id = {r.id: r for r in self.resources}

    def is_nonprod(self, resource: Resource) -> bool:
        return resource.tags.get(self.policy.env_tag, "").lower() in self.policy.nonprod_envs

    def is_prod(self, resource: Resource) -> bool:
        env = resource.tags.get(self.policy.env_tag, "").lower()
        return env in {"prod", "production", "prd"}


def confidence_for(observed_days: int, minimum: int) -> Level:
    """More observed days, more confidence. Fewer than the minimum is low."""
    if observed_days >= max(28, 2 * minimum):
        return "high"
    if observed_days >= minimum:
        return "medium"
    return "low"


def rec_id(category: str, resource_id: str) -> str:
    return "R-" + hashlib.sha256(f"{category}|{resource_id}".encode()).hexdigest()[:8].upper()


def make_rec(
    category: Category,
    resource: Resource,
    *,
    title: str,
    action: str,
    rationale: str,
    fraction: float,
    confidence: Level,
    risk: Level,
    effort: Level,
    checks: list[str],
    details: dict[str, Any] | None = None,
) -> Recommendation:
    """A recommendation whose standalone saving is ``fraction`` of the resource's monthly cost.

    The planner recomputes the saving against what is left after earlier recommendations.
    """
    fraction = max(0.0, min(1.0, fraction))
    return Recommendation(
        id=rec_id(category, resource.id),
        category=category,
        resource_id=resource.id,
        resource_name=resource.name or resource.id,
        provider=resource.provider,
        title=title,
        action=action,
        rationale=rationale,
        current_monthly=round(resource.monthly_cost, 2),
        monthly_saving=round(resource.monthly_cost * fraction, 2),
        confidence=confidence,
        risk=risk,
        effort=effort,
        checks=checks,
        details={**(details or {}), "fraction": round(fraction, 6)},
    )
