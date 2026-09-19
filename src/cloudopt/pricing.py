"""Illustrative price catalogue: instance sizes, storage tiers and commitment discounts.

The numbers are round planning figures in US dollars, shaped like public list prices but not copied from any
price sheet. Replace them with your negotiated rates using a pricing file (see ``configs/pricing.example.yaml``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cloudopt.errors import ConfigurationError

HOURS_PER_MONTH = 730.0
MAX_FILE_BYTES = 2_000_000


class InstanceType(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    name: str
    family: str
    vcpu: int = Field(gt=0)
    mem_gb: float = Field(gt=0)
    hourly: float = Field(gt=0)
    generation: int = Field(default=1, ge=1)
    successor: str = ""


class StorageTier(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    per_gb_month: float = Field(ge=0)
    retrieval_per_gb: float = Field(default=0.0, ge=0)


def _fleet(
    provider: str,
    family: str,
    generation: int,
    rows: list[tuple[str, int, float, float]],
    successor_family: str = "",
) -> list[InstanceType]:
    return [
        InstanceType(
            provider=provider,
            name=name,
            family=family,
            vcpu=vcpu,
            mem_gb=mem,
            hourly=hourly,
            generation=generation,
            successor=(name.replace(family, successor_family) if successor_family else ""),
        )
        for name, vcpu, mem, hourly in rows
    ]


_INSTANCES: tuple[InstanceType, ...] = tuple(
    _fleet(
        "aws",
        "m5",
        5,
        [
            ("m5.large", 2, 8, 0.096),
            ("m5.xlarge", 4, 16, 0.192),
            ("m5.2xlarge", 8, 32, 0.384),
            ("m5.4xlarge", 16, 64, 0.768),
        ],
        "m7i",
    )
    + _fleet(
        "aws",
        "m7i",
        7,
        [
            ("m7i.large", 2, 8, 0.0882),
            ("m7i.xlarge", 4, 16, 0.1764),
            ("m7i.2xlarge", 8, 32, 0.3528),
            ("m7i.4xlarge", 16, 64, 0.7056),
        ],
    )
    + _fleet(
        "aws",
        "c5",
        5,
        [("c5.large", 2, 4, 0.085), ("c5.xlarge", 4, 8, 0.17), ("c5.2xlarge", 8, 16, 0.34)],
    )
    + _fleet(
        "aws",
        "r5",
        5,
        [("r5.large", 2, 16, 0.126), ("r5.xlarge", 4, 32, 0.252), ("r5.2xlarge", 8, 64, 0.504)],
    )
    + _fleet("aws", "g5", 5, [("g5.xlarge", 4, 16, 1.006), ("g5.2xlarge", 8, 32, 1.212)])
    + _fleet(
        "azure",
        "Standard_D",
        4,
        [
            ("Standard_D2s_v4", 2, 8, 0.096),
            ("Standard_D4s_v4", 4, 16, 0.192),
            ("Standard_D8s_v4", 8, 32, 0.384),
        ],
        "",
    )
    + _fleet(
        "gcp",
        "n2-standard",
        2,
        [
            ("n2-standard-2", 2, 8, 0.097),
            ("n2-standard-4", 4, 16, 0.194),
            ("n2-standard-8", 8, 32, 0.388),
            ("n2-standard-16", 16, 64, 0.776),
        ],
    )
)

_STORAGE: dict[str, dict[str, StorageTier]] = {
    "aws": {
        "standard": StorageTier(per_gb_month=0.023),
        "infrequent": StorageTier(per_gb_month=0.0125, retrieval_per_gb=0.01),
        "archive": StorageTier(per_gb_month=0.004, retrieval_per_gb=0.03),
    },
    "azure": {
        "standard": StorageTier(per_gb_month=0.0184),
        "infrequent": StorageTier(per_gb_month=0.01, retrieval_per_gb=0.01),
        "archive": StorageTier(per_gb_month=0.002, retrieval_per_gb=0.02),
    },
    "gcp": {
        "standard": StorageTier(per_gb_month=0.02),
        "infrequent": StorageTier(per_gb_month=0.01, retrieval_per_gb=0.01),
        "archive": StorageTier(per_gb_month=0.0012, retrieval_per_gb=0.05),
    },
}
_COMMITMENT_DISCOUNT: dict[str, dict[str, float]] = {
    "aws": {"1y": 0.28, "3y": 0.46},
    "azure": {"1y": 0.30, "3y": 0.48},
    "gcp": {"1y": 0.25, "3y": 0.45},
}
_DISK_PER_GB = {"aws": 0.08, "azure": 0.075, "gcp": 0.04}
_SNAPSHOT_PER_GB = {"aws": 0.05, "azure": 0.05, "gcp": 0.026}


@dataclass(frozen=True)
class Pricing:
    """Price lookups used by the analyzers."""

    instances: dict[tuple[str, str], InstanceType] = field(default_factory=dict)
    storage: dict[str, dict[str, StorageTier]] = field(default_factory=dict)
    commitment_discount: dict[str, dict[str, float]] = field(default_factory=dict)
    disk_per_gb: dict[str, float] = field(default_factory=dict)
    snapshot_per_gb: dict[str, float] = field(default_factory=dict)

    @classmethod
    def default(cls) -> Pricing:
        return cls(
            instances={(i.provider, i.name): i for i in _INSTANCES},
            storage={p: dict(t) for p, t in _STORAGE.items()},
            commitment_discount={p: dict(d) for p, d in _COMMITMENT_DISCOUNT.items()},
            disk_per_gb=dict(_DISK_PER_GB),
            snapshot_per_gb=dict(_SNAPSHOT_PER_GB),
        )

    def instance(self, provider: str, name: str) -> InstanceType | None:
        return self.instances.get((provider, name))

    def family(self, provider: str, family: str) -> list[InstanceType]:
        """Sizes in a family, smallest hourly price first."""
        return sorted(
            (i for (p, _), i in self.instances.items() if p == provider and i.family == family),
            key=lambda i: (i.hourly, i.name),
        )

    def cheapest_fit(
        self, provider: str, family: str, vcpu: float, mem_gb: float
    ) -> InstanceType | None:
        """The cheapest size in ``family`` with at least ``vcpu`` vCPUs and ``mem_gb`` of memory."""
        for candidate in self.family(provider, family):
            if candidate.vcpu >= math.ceil(vcpu - 1e-9) and candidate.mem_gb >= mem_gb - 1e-9:
                return candidate
        return None

    def tier(self, provider: str, name: str) -> StorageTier | None:
        return self.storage.get(provider, {}).get(name)


def _read(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ConfigurationError(f"{path.name} is larger than {MAX_FILE_BYTES} bytes")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"cannot read {path.name}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigurationError(f"{path.name} must contain a mapping at the top level")
    return data


def load_pricing(path: Path | None) -> Pricing:
    """The default catalogue, with any entries in ``path`` added or replaced.

    Raises:
        ConfigurationError: If the file is unreadable or contains invalid values.
    """
    pricing = Pricing.default()
    if path is None:
        return pricing
    data = _read(path)
    unknown = sorted(
        set(data)
        - {"instances", "storage", "commitment_discount", "disk_per_gb", "snapshot_per_gb"}
    )
    if unknown:
        raise ConfigurationError(f"{path.name} has unknown sections: {', '.join(unknown)}")
    try:
        for raw in data.get("instances", []):
            item = InstanceType.model_validate(raw)
            pricing.instances[(item.provider, item.name)] = item
        for provider, tiers in (data.get("storage") or {}).items():
            for name, raw in tiers.items():
                pricing.storage.setdefault(provider, {})[name] = StorageTier.model_validate(raw)
        for provider, terms in (data.get("commitment_discount") or {}).items():
            for term, rate in terms.items():
                if (
                    not isinstance(rate, (int, float))
                    or isinstance(rate, bool)
                    or not 0 <= rate < 1
                ):
                    raise ConfigurationError(
                        f"commitment discount for {provider} {term} must be between 0 and 1"
                    )
                pricing.commitment_discount.setdefault(provider, {})[str(term)] = float(rate)
        for section, target in (
            ("disk_per_gb", pricing.disk_per_gb),
            ("snapshot_per_gb", pricing.snapshot_per_gb),
        ):
            for provider, rate in (data.get(section) or {}).items():
                if not isinstance(rate, (int, float)) or isinstance(rate, bool) or rate < 0:
                    raise ConfigurationError(
                        f"{section} for {provider} must be a non-negative number"
                    )
                target[provider] = float(rate)
    except (ValidationError, AttributeError, TypeError) as exc:
        raise ConfigurationError(f"invalid pricing in {path.name}: {exc}") from exc
    return pricing
