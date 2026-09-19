# ADR 0002: Recommend only with enough evidence

- Status: Accepted
- Date: 2026-09-19

## Context

Turning off the wrong machine, or shrinking a service that peaks once a month, costs more than the saving. A tool
that guesses from a few days of data will be ignored after its first bad recommendation.

## Decision

Idle and rightsizing checks need a minimum observation window (14 days by default) and use the 95th percentile,
not the average. Rightsizing targets 60% CPU and 75% memory headroom, skips instances above 70% CPU, and requires
at least a 10% price reduction. Memory is preserved when it is not measured. Resources without metrics are counted
and reported, not guessed. Every recommendation carries a confidence level from its observation length, and a
list of checks to do before acting.

## Consequences

- Some real savings are missed, such as a machine that is idle but only observed for a week. The report says how
  many resources were skipped and why.
- Thresholds are policy fields. A team with good monitoring can tighten them.
- Short-lived cost surprises are caught by anomaly detection, which does not need a per-resource history.
