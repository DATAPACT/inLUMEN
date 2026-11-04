# ADR 0003: Port types derived from artifact contracts

- Status: Accepted
- Date: 2026-11-04

## Context

Pipelines often take several files as input. Two situations look the same on
the canvas but need different handling:

- **Files with different roles** (patients + visits, a table + an image
  folder). They need different processing, so each is its own branch, and the
  branches meet at an explicit join.
- **Files of the same kind** (40 daily CSVs). These form one collection and
  call for data parallelism (scatter/gather), not 40 hand-drawn branches.

Until now every port defaulted to `any`. A multi-file Source always published
one directory bundle. Whether a downstream Task received a table or a bundle
was guessed from keywords in its label.

## Decision

1. **The artifact contract is the source of truth; ports display it.** A port
   type is a kind, optionally followed by `[]` for a collection: `table`,
   `table[]`, `json`, `any`, `any[]`. A directory with mixed members is a
   `bundle`, and one with undeclared members is a `directory`. A port declared
   `any` takes the type implied by its node's attached files or declared or
   generated output artifacts. No second type system is added on the ports.
2. **Cardinality is part of the type.** It is what separates branching
   (different kinds, separate ports) from data parallelism (`kind[]` plus
   Parallel Map).
3. **Gradual typing.** `any` stays legal. A kind mismatch is an error. A
   cardinality mismatch, or a bundle whose contents cannot be checked, is a
   warning. The editor never refuses a connection: it warns, and validation
   reports the issue.
4. **A Source chooses how to publish several files** (`files_as` parameter):
   - `collection` (the default) publishes one port carrying a directory
     bundle;
   - `separate` publishes one port per file, with ids derived from file stems.
5. **A typed `table` / `table[]` Task output decides the generated output
   shape** instead of label keywords. Untyped outputs keep the heuristics.

The rules live in `shared/port_types.json` and are implemented twice:
`shared/port_types.py`, used by the backend and codegen, and
`frontend/src/features/flow/portTypes.ts`. Both are tested against the
spec's cases. `scripts/sync_shared.py` distributes the copies.

## Consequences

- Fan-in is expressed with named input ports (one producer per port), the same
  rule Argo export already enforces.
- Switching a Source between modes changes its output ports. Connections from
  removed ports are flagged and must be reconnected.
- Next steps, not covered by this ADR:
  - run independent branches in parallel in the local and Dagster run path;
  - give Parallel Map real per-item execution with gather;
  - implement Merge/Join and Condition semantics.
