# ADR 0002: Native background pipeline execution

- Status: Accepted
- Date: 2026-08-26
- Issue: [#107](https://github.com/DATAPACT/inLUMEN/issues/107)

## Context

inLUMEN can design pipelines, persist node implementations, validate runtime
packages, and export Dagster and Argo deployment artifacts. Running a pipeline
currently requires leaving inLUMEN. The product needs a background Run workflow
that remains observable when the browser or gateway request goes away and that
can later export exactly what was tested.

The existing `inlumen.run-result@1` contract intentionally describes only a
terminal result. Expanding that result with active states would blur the
difference between a durable run resource and its final outcome.

## Decision

inLUMEN is the execution control plane, not a new orchestration engine. A
dedicated runner service owns durable run state and delegates execution through
an adapter. Dagster is the first production adapter. The gateway submits work
and returns `202 Accepted`; neither the browser nor the gateway HTTP request owns
the execution lifetime.

The runner publishes `inlumen.pipeline-run@1` for the complete lifecycle:
`queued`, `preparing`, `running`, `cancelling`, `succeeded`, `partial`, `failed`,
and `cancelled`. A terminal record contains an `inlumen.run-result@1` result.

Every submission captures an immutable graph plus the deterministic deployment
bundle containing persisted runtime packages, input bytes, connector adapters,
Run Spec, and configuration. The lifecycle record exposes both graph and bundle
SHA-256 digests. Export uses stored snapshot material rather than the mutable
design canvas.

The runner stores lifecycle metadata durably and exposes cursor-based events.
The single-replica implementation stores immutable bundle and output payloads in
a separate filesystem artifact store rather than SQLite. Horizontally scaled
deployments move those payloads to object storage. Neo4j stores concise
pipeline/run summaries and durable references, not event streams or artifact
bytes.

The Dagster adapter delegates materialization to the private execution service.
That service builds a content-addressed image and runs it as a constrained
one-off unprivileged container with read-only code and Source inputs, writable
workspace and output mounts, resource limits, dropped Linux capabilities, and
no Docker socket.
The returned logs and output artifacts come from that real materialization.

Resource allocation is owned by the execution platform rather than exposed as
pipeline parameters or deployment environment tuning. The execution service
selects a reviewed `lightweight`, `standard`, or `ml_cpu` profile from bundle
model metadata and dependency evidence, then clamps that profile to the
resources available on the Docker host. It reserves host capacity for the
operating system and control-plane services and admits runs through a FIFO queue
so concurrent workloads cannot collectively overcommit the host. The selected
profile, effective CPU and memory allocation, and queue position are published
as run progress.

Admission is intentionally process-local while the execution service is a
single replica. A horizontally scaled execution service must replace it with a
shared scheduler or cluster-native resource admission; independently scheduling
each replica would lose the host-wide capacity guarantee.

## API boundary

- `POST /api/pipeline-runs` returns `202` and a stable `run_id`.
- `GET /api/pipeline-runs` lists recent durable runs.
- `GET /api/pipeline-runs/{run_id}` returns current lifecycle state.
- `GET /api/pipeline-runs/{run_id}/events?after=<cursor>` returns incremental
  events without holding the request open.
- `DELETE /api/pipeline-runs/{run_id}` idempotently requests cancellation.
- `GET /api/pipeline-runs/{run_id}/bundle` downloads the exact tested snapshot.
- `GET /api/pipeline-runs/{run_id}/outputs/{path}` downloads a produced artifact.

The initial client polls. A streaming transport may be added later without
changing event identities or run semantics.

## Security and recovery

The runner is private and authenticated independently from the public gateway.
User code will execute only in adapter-owned isolated containers without Docker
daemon access. Secret values are injected ephemerally and must not appear in
snapshots, events, logs, or results.

Browser and gateway restarts do not affect runs. Runner restart reconciliation
first requests termination of any container carrying the run identity and then
marks the interrupted run failed with a stable recovery error rather than
pretending that it completed.

## Consequences

Native execution introduces a private service and durable state, but keeps
execution isolated from the gateway and keeps engine concerns behind an adapter.
The pipeline-run resource and terminal result remain independently versioned.
The Dagster export compiler and native adapter must consume the same immutable
snapshot contracts to prevent “tested” and “exported” pipeline drift.
