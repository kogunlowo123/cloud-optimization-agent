# ADR 0001: Build the plan in a fixed order against the remaining cost

- Status: Accepted
- Date: 2026-09-19

## Context

Independent savings estimates overlap. An instance can be idle, oversized and non-production at once. Adding the
three estimates claims more than the instance costs, and a report that promises savings it cannot deliver loses
trust quickly.

## Decision

Analyzers propose changes independently, each expressed as a fraction of the resource's cost. The planner applies
them in a fixed order (waste, rightsize, schedule, storage tier, commitment), and computes each saving against
what the earlier steps left. A resource with a waste recommendation gets no other recommendation. Commitments are
sized last from the cost that remains.

## Consequences

- Totals never exceed spend, and combined changes are priced correctly. A property test checks this on random
  estates.
- The order is a judgment: removing a resource beats shrinking it, and shrinking beats scheduling. Different
  choices would give different individual numbers, but the same rule everywhere makes the numbers comparable.
- Individual recommendations show the cost they were priced against, which can be lower than the resource's
  full cost.
