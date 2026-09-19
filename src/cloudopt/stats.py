"""Small statistical helpers. Standard library only."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence


def percentile(values: Sequence[float], q: float) -> float:
    """The ``q`` quantile (0 to 1) by linear interpolation. Returns 0.0 for an empty sequence."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = min(max(q, 0.0), 1.0) * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def robust_zscore(value: float, sample: Sequence[float]) -> float:
    """Distance of ``value`` from the sample median in robust deviations (median absolute deviation).

    Falls back to the standard deviation when the deviation is zero, and returns 0.0 for fewer than
    three points or a sample with no spread at all.
    """
    if len(sample) < 3:
        return 0.0
    median = statistics.median(sample)
    mad = statistics.median(abs(x - median) for x in sample)
    if mad > 0:
        return 0.6745 * (value - median) / mad
    spread = statistics.pstdev(sample)
    return (value - median) / spread if spread > 0 else 0.0
