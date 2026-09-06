# Hardening and operations

This change set strengthens the existing editor, generation service and runner.
It does not deploy the application or change live workspace data. Use the
[production guide](production-multi-user.md) for the deployment topology.

## Behavior and compatibility

| Area | Implemented behavior |
| --- | --- |
| Workspace access | Owner, editor, runner and viewer permissions are enforced at the gateway and internal Flask adapters. Unknown roles fail closed. Raw Cypher is unavailable over HTTP; internal agent queries require an in-process capability. Trusted template literals become driver parameters. |
| Graph edits | Each adapter request uses one Neo4j transaction and workspace revision lock. Graph reads return `ETag`; writes carrying `If-Match` return 409 on a stale revision. Malformed whole-graph documents return 422 before synchronization. |
| Editor recovery | Writes are serialized, rejected saves stay visible, and polling cannot overwrite an unsaved draft. Late responses from previous workspaces or older reads cannot advance the revision incorrectly. Draft download and explicit reload are available. Invalid browser drafts are retained under their original key and, when storage permits, a `:recovery` key. |
| Generated execution | Import/definitions checks and Dagster materialization always use a Docker container, including calls without an execution ID. The runtime is non-root with a read-only root filesystem, dropped capabilities and bounded resources. Connector secrets are omitted from model prefetch. Model caches are separated by workspace. |
| Durable work | Database leases enforce one worker owner and per-workspace/global admission. Heartbeats extend ownership. Recovery runs periodically; interrupted generation is resumable. Runner recovery reads durable execution receipts and never resubmits a side-effecting pipeline automatically. Deletion markers stop stale workers from recreating cleared jobs. |
| Artifacts | Generated files are uploaded under content-addressed object names before their graph pointers are published in one transaction. Version snapshots receive unique object names; failed copies abort publication. Snapshot copying checks both source and destination workspace ownership. Runner files use temporary-file replacement and fsync. Receipt redaction recalculates output hashes and sizes. |
| AI limits | Code generation bounds repair attempts, request count, output tokens and total generation time. A reported-cost threshold stops subsequent calls. Four initial intent evaluation cases cover normalization, audio analysis, conditional routing and parallel documents. |
| Frontend | TypeScript strictness is enabled. Editor, inspector, sidebar, versions and ZIP tooling load in separate chunks. Canvas keyboard movement persists positions, toolbar controls wrap on tablets, and a rendering error boundary preserves drafts. Browser tests cover conflict recovery, draft download and serious/critical accessibility violations. |
| Maintenance | Backend dependencies have a hashed lock; codegen/runner use frozen locks and Python 3.11. Frontend dependencies are updated, infrastructure and production base images are pinned by digest. Shared lease/diagnostic modules and frontend fallback definitions have generated-copy checks. Request logs contain IDs, route templates, status and duration without request bodies or query strings. |

Permission policy:

| Role | Read | Edit graph/configuration | Run/cancel pipelines | Clear whole workspace/run history |
| --- | --- | --- | --- | --- |
| owner | yes | yes | yes | yes |
| editor | yes | yes | yes | no |
| runner | yes | no | yes | no |
| viewer | yes | no | no | no |

Existing clients that omit `If-Match` retain compatibility and do not receive
optimistic conflict protection. New clients should read `/api/pipeline/graph`,
retain its `ETag`, and send that value on each graph mutation, advancing it from
each successful response. A 409 requires a reload and user reconciliation;
do not retry it blindly. The browser performs this automatically for canvas
edits. Agent turns still comprise several ordered tool transactions, so the
revision boundary is an individual adapter request, not an entire AI turn.

The adapter creates a uniqueness constraint for workspace revisions on first
database access. Its Neo4j account therefore needs schema-write permission at
startup. Allow this before routing traffic. Workspace data uses the existing
server-derived labels; no data migration or graph rewrite is required.

## Worker deployment and recovery

Production uses PostgreSQL. Multiple codegen/runner replicas must share the
database and runner artifact filesystem. SQLite remains for a single local
process and tests. The lease duration is 60 seconds with a 15-second heartbeat;
recovery checks run every 20 seconds. Keep host clocks synchronized.

Defaults are 4 outstanding generation jobs per workspace and 12 globally,
configured with `CODEGEN_MAX_OUTSTANDING_RUNS` and
`CODEGEN_MAX_GLOBAL_OUTSTANDING_RUNS`. Runner defaults are 4 and 20, configured
with the equivalent `RUNNER_` variables. Capacity responses use HTTP 429.

After a runner restart, leave the original run attached while its execution
receipt is reconciled. A completed receipt restores the outcome and output
artifacts. An expired or unavailable receipt eventually produces an unknown
outcome failure. Check destination systems before creating a new run: an unknown
outcome is not evidence that external side effects did not happen. Generation
restart recovery requires an explicit resume and a fresh provider credential.

The default Compose topology still gives the private codegen service access to
the Docker socket. Treat this service as privileged infrastructure. For a
dedicated execution host, deploy codegen on that host and point the gateway's
`INLUMEN_CODEGEN_SERVICE_URL` and runner's `INLUMEN_CODEGEN_SERVICE_URL` to its private
HTTPS endpoint, preserving service authentication and the workspace header.
Keep PostgreSQL access private and use the same database. Configure the worker's
validation directory so Docker resolves the same absolute bind paths. Restrict
worker network egress to the connectors and package/model endpoints needed by
your pipelines. The repository does not provision a worker VM, rootless Docker,
egress firewall or a Kubernetes cluster.

Per-workspace model-cache volumes append `-ws-<digest>` to
`INLUMEN_MODEL_STORE_VOLUME`. They contain replaceable cached models and can be
removed while the relevant workers are stopped. They are not shared across
workspaces. Cached model availability and download latency should be measured
in the staging workload.

## Backups and isolated restore

Run from the repository root with access to the target Docker daemon. This is
an offline backup: it stops only running containers bearing the exact Compose
project label, archives all named volumes attached to that project, and restarts
the containers that were running. Schedule an appropriate maintenance window.

```sh
python scripts/backup_volumes.py backup /secure/backups/inlumen-2026-09-06 --project inlumen
python scripts/backup_volumes.py verify /secure/backups/inlumen-2026-09-06
python scripts/backup_volumes.py restore /secure/backups/inlumen-2026-09-06 --restore-prefix inlumen-restore-check
```

The manifest records source image IDs, volume names and SHA-256 checksums.
Restore verifies checksums, refuses existing target volumes, rejects unsafe
archive paths and preserves ownership. It prints the mapping from old to new
volumes. Start a separate Compose project using those restored volumes, without
the production tunnel, to test application readiness and workspace access before
any cutover. A disposable-volume drill verified bytes and UID preservation;
this does not replace a restore drill of your actual PostgreSQL/Neo4j/MinIO data.

Archive files contain application data. Store them on encrypted storage with
restricted permissions. Keep `.env.production`, external service secrets and
`INLUMEN_SECRET_ENCRYPTION_KEY` separately; bind mounts and external databases
are not covered by the volume script. Losing the encryption key makes encrypted
workspace credentials unreadable. Replicate backups off the application host.

Old blobs and receipts are retained by default. To review aged, unreferenced
MinIO objects in a selected workspace, run:

```sh
python scripts/prune_workspace_blobs.py --workspace WORKSPACE_ID --older-than-days 30
```

This reports candidates without deleting anything. It scans active graph and
saved-version references, including nested JSON. Back up first and stop every
writer to that workspace before adding `--apply --writers-stopped`. Ordinary
uploads, runner outputs and execution receipts are excluded. Keep receipt IDs
indefinitely as replay protection; do not delete them as generic log retention.
No automatic garbage collector is enabled.

## Validation, evaluation and diagnostics

```sh
python scripts/sync_shared.py --check
python scripts/run_tests.py
cd frontend
npx playwright install chromium
npm run test:e2e
npm audit --audit-level=moderate
```

Use Python 3.11 with backend `requirements.lock`, frozen `uv.lock` environments
for codegen/runner, and Node 22.12+ for frontend checks. The Python runner uses
its invoking interpreter for backend tests. To update shared code, edit
`shared/leases.py`, `shared/request_diagnostics.py`, or the backend core node
manifest, then run `python scripts/sync_shared.py`. CI checks generated copies.

Real Neo4j tests require an explicitly disposable database:

```sh
cd backend
RUN_NEO4J_INTEGRATION=1 NEO4J_URI=bolt://127.0.0.1:7687 NEO4J_AUTH=neo4j/test-password \
  python -m unittest discover -s tests -p test_graph_transactions_integration.py
RUN_NEO4J_INTEGRATION=1 NEO4J_URI=bolt://127.0.0.1:7687 NEO4J_AUTH=neo4j/test-password \
  python -m unittest discover -s tests -p test_agent_workspace_queries.py
```

The graph transaction test commits fixtures in the local workspace. Never point
it at a user's database. The two-workspace agent query test rolls its data back.
A disposable Dagster materialization smoke test verified the runtime UID and
read-only filesystem. Run it explicitly with:

```sh
PYTHONPATH=codegen codegen/.venv/bin/python scripts/test_runtime_container.py
```

PostgreSQL worker claims, cross-worker quotas and deletion markers are checked
with `TEST_POSTGRES_URL=postgresql://... python scripts/test_postgres_workers.py runner`
and the equivalent `codegen` command, always against a disposable database.

Browser tests use mocked gateway responses; they do not certify a live Keycloak,
storage and generated-execution deployment. CI runs both test categories.

For AI evaluation, execute each intent in `evals/pipeline-intents.json` against
the candidate model in a disposable workspace. Record a JSON list with `id`,
the persisted `graph`, measured `elapsed_seconds`, provider-reported `cost_usd`,
and `execution_success` from the actual runner outcome. Then run:

```sh
python scripts/evaluate_pipelines.py candidates.json \
  --suite evals/pipeline-intents.json --output evaluation.json
```

Missing cases, required task/graph failures, unverified execution, and missing
or exceeded cost/time budgets fail the report. The scorer does not call a model
or execute code. Its regression tests use synthetic fixtures; no live model
benchmark or improvement claim is implied. Label-pattern checks are a starting
point; review output semantics and expand the corpus before model comparisons.

Generation defaults: 12 LLM requests, 16,384 output tokens per request,
1,200 total seconds, and a $5 reported-cost threshold. The cost threshold is
not a billing cap: providers may omit cost, and the final in-flight request can
exceed the threshold. Configure a provider-side spend limit for a hard cap.

Each HTTP response carries `X-Request-ID`; accepted incoming IDs are bounded
and sanitized. Structured request logs use route templates rather than resource
IDs or query strings. Aggregate duration, 5xx, 409 and 429 events in your logging
system; alert on sustained errors, lease recovery failures and growing disk use.
Logs and headers provide instrumentation, not a bundled monitoring service.

Before production rollout, run the deployment's two-user Keycloak check, an
actual generated-container execution, provider evaluation, load test and full
database restore drill. Rollout and those external-environment checks remain
operator actions; this implementation has not modified the running deployment.


## Verification of this change set (2026-09-06)

- Backend: 272 discovered tests, 269 passed and 3 integration tests skipped in
  the default unit run; executed with Python 3.11 and the hashed lock.
- Codegen: 117 passed; runner: 21 passed, both on Python 3.11/frozen locks.
- Frontend: 131 passed, plus strict TypeScript, lint and production build.
  Lint retains 7 pre-existing warnings; the largest editor chunk remains above
  Vite's 500 kB advisory threshold.
- Real Neo4j: stale-write rejection and artifact transaction rollback passed;
  the separate two-workspace agent-query integration passed.
- Real PostgreSQL: concurrent claims, cross-worker quotas and deletion markers
  passed for both worker services.
- Real Dagster container: materialization passed and verified non-root UID,
  read-only root filesystem and writable output mount.
- Playwright: conflict recovery, revision header, draft download, keyboard
  movement and serious/critical accessibility checks passed on 3 repeated runs.
- Backup drill: checksum verification, isolated restore, bytes and ownership passed.
- `npm audit`: 0 reported vulnerabilities. Compose configuration and generated
  module/manifest parity checks passed.

No production deployment, live-provider evaluation, load benchmark or full
application database restore was performed. The starter evaluation corpus and
operator commands make those checks reproducible without claiming their results.
