# ADR 0003: Advise only, and honour protection tags

- Status: Accepted
- Date: 2026-09-19

## Context

Cost tools that act on their own eventually delete something important. Inventory data also comes from outside the
tool and may contain hostile text, so anything derived from it needs care.

## Decision

The tool reads files and never calls a cloud API. It prints suggested provider commands for a person to review,
with a notice and with every inventory value shell-quoted after control characters are removed. Resources tagged
`do-not-optimize` (or listed by id) are never planned. Their recommendations are listed as blocked with the
saving they would have given, so the decision is visible. Resources that look idle are also kept out of
commitments even when protected.

## Consequences

- Nothing can be changed by running the tool, and hostile names cannot turn a suggested command into a
  different command.
- Reviewers can see the cost of protection choices.
- Acting on the plan is manual or belongs to a separate, reviewed automation.
