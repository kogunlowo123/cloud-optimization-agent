"""Domain models: resources, billing, recommendations, anomalies and reports."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Provider = Literal["aws", "azure", "gcp"]
ResourceType = Literal[
    "vm", "disk", "snapshot", "load_balancer", "public_ip", "database", "bucket", "nat_gateway"
]
Category = Literal["waste", "rightsize", "schedule", "storage_tier", "commitment"]
Level = Literal["low", "medium", "high"]

CATEGORY_ORDER: dict[str, int] = {
    "waste": 0,
    "rightsize": 1,
    "schedule": 2,
    "storage_tier": 3,
    "commitment": 4,
}
CATEGORY_LABELS: dict[str, str] = {
    "waste": "Remove waste",
    "rightsize": "Rightsize",
    "schedule": "Schedule non-production",
    "storage_tier": "Tier cold storage",
    "commitment": "Commit to steady usage",
}
DAYS_PER_MONTH = 30.4375


class Metrics(BaseModel):
    """Utilization over the observation period. Percentages are 0 to 100."""

    model_config = ConfigDict(extra="forbid")

    observed_days: int = Field(default=0, ge=0, le=3650)
    cpu_avg: float | None = Field(default=None, ge=0, le=100)
    cpu_p95: float | None = Field(default=None, ge=0, le=100)
    mem_avg: float | None = Field(default=None, ge=0, le=100)
    mem_p95: float | None = Field(default=None, ge=0, le=100)
    net_gb_per_day: float | None = Field(default=None, ge=0)
    connections_per_day: float | None = Field(default=None, ge=0)
    last_accessed_days_ago: int | None = Field(default=None, ge=0)
    active_hours_per_week: float | None = Field(default=None, ge=0, le=168)


class Resource(BaseModel):
    """One billable cloud resource."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=200)
    name: str = Field(default="", max_length=200)
    type: ResourceType
    provider: Provider
    region: str = Field(default="", max_length=64)
    size: str = Field(default="", max_length=100)
    state: str = Field(default="", max_length=32)
    state_days: int | None = Field(default=None, ge=0)
    created: datetime | None = None
    tags: dict[str, str] = Field(default_factory=dict)
    monthly_cost: float = Field(default=0.0, ge=0)
    size_gb: float | None = Field(default=None, ge=0)
    attached_to: str = Field(default="", max_length=200)
    parent: str = Field(default="", max_length=200)
    metrics: Metrics = Field(default_factory=Metrics)

    @field_validator("created")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return (
            value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
        )

    @field_validator("state")
    @classmethod
    def _lower(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("tags")
    @classmethod
    def _tags(cls, value: dict[str, str]) -> dict[str, str]:
        return {
            str(k).strip().lower()[:64]: str(v).strip()[:128] for k, v in list(value.items())[:50]
        }

    def age_days(self, today: date) -> int | None:
        return None if self.created is None else max(0, (today - self.created.date()).days)


class BillingLine(BaseModel):
    """One day of cost for one resource and service."""

    model_config = ConfigDict(extra="forbid")

    day: date
    resource_id: str = Field(default="", max_length=200)
    service: str = Field(min_length=1, max_length=100)
    cost: float
    tags: dict[str, str] = Field(default_factory=dict)

    @field_validator("cost")
    @classmethod
    def _finite(cls, value: float) -> float:
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("cost must be finite")
        return value

    @field_validator("tags")
    @classmethod
    def _tags(cls, value: dict[str, str]) -> dict[str, str]:
        return {
            str(k).strip().lower()[:64]: str(v).strip()[:128] for k, v in list(value.items())[:50]
        }


class Recommendation(BaseModel):
    """One proposed change with its estimated saving."""

    id: str
    category: Category
    resource_id: str
    resource_name: str
    provider: str
    title: str
    action: str
    rationale: str
    current_monthly: float
    monthly_saving: float
    confidence: Level
    risk: Level
    effort: Level
    checks: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
    blocked: str = ""

    @property
    def annual_saving(self) -> float:
        return round(self.monthly_saving * 12, 2)


class Anomaly(BaseModel):
    """A change in daily cost that the history does not explain."""

    service: str
    kind: Literal["spike", "shift"]
    first_day: date
    last_day: date
    baseline_daily: float
    current_daily: float
    extra_monthly: float
    top_resources: list[dict[str, Any]]


class AllocationRow(BaseModel):
    value: str
    monthly_cost: float
    share: float


class Allocation(BaseModel):
    dimension: str
    rows: list[AllocationRow]
    untagged_cost: float
    untagged_share: float


class Totals(BaseModel):
    resources: int
    monthly_spend: float
    identified_saving: float
    saving_pct: float
    by_category: dict[str, float]
    blocked_saving: float


class Report(BaseModel):
    as_of: date
    billing_days: int
    totals: Totals
    recommendations: list[Recommendation]
    blocked: list[Recommendation]
    anomalies: list[Anomaly]
    allocation: list[Allocation]
    warnings: list[str]
    summary: str = ""


class IngestReport(BaseModel):
    kind: str
    lines: int = 0
    accepted: int = 0
    rejected: int = 0
    errors: list[str] = Field(default_factory=list)
