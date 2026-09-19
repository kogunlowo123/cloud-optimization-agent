"""Configuration: environment settings and the optimization policy."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from cloudopt.errors import ConfigurationError

MAX_FILE_BYTES = 2_000_000


class Settings(BaseSettings):
    """Runtime settings from ``CLOUDOPT_*`` environment variables and ``.env``."""

    model_config = SettingsConfigDict(
        env_prefix="CLOUDOPT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    pricing_file: Path | None = None
    policy_file: Path | None = None
    max_line_bytes: int = Field(default=100_000, ge=1000)
    max_records: int = Field(default=3_000_000, ge=1)

    llm_provider: Literal["none", "openai", "anthropic"] = "none"
    openai_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("CLOUDOPT_OPENAI_API_KEY", "OPENAI_API_KEY")
    )
    openai_base_url: str = "https://api.openai.com/v1"
    openai_chat_model: str = "gpt-4o-mini"
    anthropic_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("CLOUDOPT_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
    )
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_model: str = "claude-sonnet-5"
    anthropic_max_tokens: int = Field(default=500, gt=0)

    http_timeout_seconds: float = Field(default=30.0, gt=0)
    retry_attempts: int = Field(default=3, ge=1)
    retry_min_wait: float = Field(default=0.5, ge=0)
    retry_max_wait: float = Field(default=8.0, ge=0)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "WARNING"
    log_json: bool = True

    @model_validator(mode="after")
    def _check_consistency(self) -> Settings:
        if self.retry_max_wait < self.retry_min_wait:
            raise ValueError("retry_max_wait must be >= retry_min_wait")
        return self


class Policy(BaseModel):
    """What counts as waste, how aggressive rightsizing is, and what must never be touched."""

    model_config = ConfigDict(extra="forbid")

    env_tag: str = "env"
    owner_tag: str = "owner"
    allocation_tags: list[str] = Field(default_factory=lambda: ["team", "app", "env"])
    nonprod_envs: list[str] = Field(
        default_factory=lambda: ["dev", "test", "staging", "qa", "sandbox"]
    )
    protect_tags: dict[str, str] = Field(default_factory=lambda: {"do-not-optimize": "*"})
    protected_ids: list[str] = Field(default_factory=list)

    min_observed_days: int = Field(default=14, ge=1)
    idle_cpu_p95: float = Field(default=5.0, ge=0, le=100)
    idle_net_gb_per_day: float = Field(default=0.5, ge=0)
    target_cpu_util: float = Field(default=60.0, gt=0, le=100)
    target_mem_util: float = Field(default=75.0, gt=0, le=100)
    rightsize_max_cpu_p95: float = Field(default=70.0, gt=0, le=100)
    rightsize_min_saving_pct: float = Field(default=10.0, ge=0, le=100)
    unattached_days: int = Field(default=7, ge=0)
    snapshot_age_days: int = Field(default=90, ge=1)
    stopped_days: int = Field(default=30, ge=1)
    idle_connections_per_day: float = Field(default=1.0, ge=0)
    business_hours_per_week: float = Field(default=55.0, gt=0, le=168)
    schedule_max_active_hours: float = Field(default=60.0, gt=0, le=168)
    cold_days_infrequent: int = Field(default=90, ge=1)
    cold_days_archive: int = Field(default=365, ge=1)
    commitment_coverage: float = Field(default=0.8, gt=0, le=1)
    commitment_min_observed_days: int = Field(default=30, ge=1)
    commitment_term: Literal["1y", "3y"] = "1y"
    anomaly_min_days: int = Field(default=14, ge=7)
    anomaly_z: float = Field(default=4.0, gt=0)
    anomaly_min_extra_daily: float = Field(default=5.0, ge=0)
    anomaly_min_increase_pct: float = Field(default=10.0, ge=0)
    anomaly_recent_days: int = Field(default=3, ge=1)
    allocation_window_days: int = Field(default=30, ge=1)

    @model_validator(mode="after")
    def _normalise(self) -> Policy:
        self.env_tag = self.env_tag.strip().lower()
        self.owner_tag = self.owner_tag.strip().lower()
        self.allocation_tags = [t.strip().lower() for t in self.allocation_tags if t.strip()]
        self.nonprod_envs = [e.strip().lower() for e in self.nonprod_envs]
        self.protect_tags = {k.strip().lower(): v for k, v in self.protect_tags.items()}
        if self.cold_days_archive <= self.cold_days_infrequent:
            raise ValueError("cold_days_archive must be greater than cold_days_infrequent")
        return self


def load_policy(path: Path | None) -> Policy:
    """Load the policy from YAML, or the defaults when ``path`` is ``None``.

    Raises:
        ConfigurationError: If the file is unreadable or contains invalid values.
    """
    if path is None:
        return Policy()
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ConfigurationError(f"{path.name} is larger than {MAX_FILE_BYTES} bytes")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"cannot read {path.name}: {exc}") from exc
    if data is None:
        return Policy()
    if not isinstance(data, dict):
        raise ConfigurationError(f"{path.name} must contain a mapping at the top level")
    try:
        return Policy.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "value"
        raise ConfigurationError(
            f"invalid policy in {path.name} ({where}: {first['msg']})"
        ) from exc
