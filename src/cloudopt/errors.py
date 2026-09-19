"""Exception hierarchy for cloudopt."""

from __future__ import annotations


class OptError(Exception):
    """Base class for errors raised deliberately by this package."""


class ConfigurationError(OptError):
    """Settings or a configuration file are missing or invalid."""


class DataError(OptError):
    """Inventory, billing or pricing data cannot be read or fails validation."""


class ReportError(OptError):
    """A report could not be rendered or written."""


class ProviderError(OptError):
    """An external model provider returned an error or an unusable response."""


class TransientProviderError(ProviderError):
    """A provider failure worth retrying (timeouts, rate limits, 5xx)."""
