# ADR 0004: Parallel Map runs per item, closed by Collect

- Status: Accepted
- Date: 2026-11-04

## Context

Parallel Map was a pass-through at runtime: the whole collection flowed once
through the following steps, so they had to loop over files themselves. The
graph also didn't say where per-item processing ended, so an aggregation step
looked like just another step after the map.

## Decision

1. **Collect closes a Parallel Map.**
   - It is a new Flow template: `item` → `items`.
   - Every step between a Map and its Collect is the map's *body* and runs
     once per item.
   - Steps that need all items (aggregate, merge, save one file) come after
     the Collect.
   - Scope rules live in `shared/map_scope.py`, mirrored in
     `frontend/src/features/flow/mapScope.ts`:
     - each Map reaches exactly one Collect;
     - body steps take input only from the Map or other body steps, and feed
       only body steps or the Collect;
     - maps do not nest.
   - Violations are graph validation errors. While drafting on the canvas they
     are warnings.
2. **Body steps are written for one item.**
   - Codegen plans the Map's output as one representative item, so generated
     code handles a single file.
   - Collect's output declares an `item`, so its port type is `kind[]`.
   - Compiler-owned Map and Collect functions are used when checking generated
     code.
3. **An engine-neutral runtime.**
   - Map publishes `items/<key>/<file>` plus `_items.json`.
   - Each body step is *lifted* at packaging time (`backend/map_runtime.py`):
     - the platform's `inlumen_item_runner.py` becomes its entrypoint, and the
       step's own code runs once per item in its own process;
     - items run up to the Map's `max_concurrency`, under its
       `failure_policy`. `stop` fails the step; `continue` records failed
       items in `_items.json` and keeps going.
     - the step's engine-facing contract becomes collection in → collection
       out, and the per-item contract stays inside the wrapper.
   - Collect gathers the successful items.
   - Because the wrapper is just the entrypoint, Dagster, Argo and local runs
     all behave the same, with no engine-specific mapping.

## Consequences

- Body steps run stage by stage: each per-item step processes every item
  before the next step starts. Items do not stream through the whole body.
  This keeps one Dagster asset per step, with its logs and lineage.
- Existing graphs with a Parallel Map and no Collect now fail validation until
  a Collect is added.
- A body step needs a declared artifact contract: generated code, or a Task
  package with `inlumen.task.json`.
- Not yet supported: side inputs into a map (e.g. a lookup table used by every
  item), nested maps, and maps over table rows instead of files.
