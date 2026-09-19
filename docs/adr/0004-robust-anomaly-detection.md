# ADR 0004: Detect cost anomalies against the service's own history

- Status: Accepted
- Date: 2026-09-19

## Context

Billing is noisy. A fixed percentage threshold either floods a large service with false alarms or misses a
meaningful jump in it. A mean and standard deviation are distorted by the anomalies themselves.

## Decision

Compare the last few days of a service with the median of the previous 28 using a robust deviation score, and also
require an absolute and a percentage increase. A perfectly flat history is treated as infinitely tight. Attribute
the change to the resources whose daily cost grew most. Distinguish a spike (some recent days) from a shift (all
of them).

## Consequences

- A machine that adds a few percent to a big service is still caught if the history is steady, and ordinary noise
  is not.
- Detection needs at least two weeks of daily billing and assumes no missing days.
- Thresholds are policy fields, and a test checks that random noise produces no anomalies.
