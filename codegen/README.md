# inLUMEN code generation

This private FastAPI service is embedded in the inLUMEN Compose stack. It uses
the dedicated code-generation model selected in inLUMEN Settings to turn the
canonical graph into Python runtime packages, then validates them before the
backend persists anything.

Each node artifact contains:

- `main.py`
- `requirements.txt`
- `node-manifest.json`
- `validation-report.json`

Node code generation calls the configured coding model but does not create
Dockerfiles. Dagster and Argo Dockerfiles are rendered deterministically later
by the deployment exporter from validated runtime packages. A combined export
uses one Dagster build image and one shared Argo/Kubernetes image; it does not
place Dockerfiles under `nodes/`.

## Background jobs

`POST /v1/generate/pipeline-scripts/runs` starts a durable job. Progress and the
original safe request are stored in SQLite at `CODEGEN_JOB_DB_PATH`, so the UI
can close and reconnect later. The list, detail, resume, and cancel endpoints
live under the same `/v1/generate/pipeline-scripts/runs` path.

Provider keys arrive in `X-LLM-API-Key`, are kept only in the active in-memory
request, and are excluded from SQLite. A resumed run receives a fresh key from
the currently selected inLUMEN model configuration.

## Authentication

Generation endpoints require the internal service bearer token configured with
`CODEGEN_SERVICE_API_KEY` (or `CODEGEN_SERVICE_API_KEY_FILE`). `/health` is
public; `/ready` also checks that production authentication is configured.

## Validation

Static validation checks syntax, imports, dependencies, implementation-profile
semantics, manifests, and graph contracts. Sample modes use an isolated Docker
sandbox with network disabled. The sandbox derives its validation base image
from `node-manifest.json`; it does not require a generated Dockerfile.

`CODEGEN_ALLOW_DETERMINISTIC_FALLBACK=true` exists for isolated tests and local
recovery only. It is disabled by default; normal production generation requires
the configured coding model.

## Local tests

```bash
uv sync --dev
uv run pytest -q
```

From the repository root, the codegen service starts with:

```bash
docker compose up --build codegen backend frontend
```

For horizontal scaling, replace the embedded SQLite store with a shared queue
and database, and run validation in dedicated workers or cluster jobs.
