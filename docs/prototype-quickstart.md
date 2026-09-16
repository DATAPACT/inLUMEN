# Deterministic prototype walkthrough

This example needs no LLM provider or API key. It uses supplied Python code,
three orders, and the native Dagster runner. It exercises the supported
design → save/reopen → run → inspect → download workflow.

## Start a local evaluation installation

Use a fresh checkout on a controlled local evaluation machine with Docker
Compose and a running Docker daemon. Initial builds require network access.
Beta 5 is fresh-install-only; first read the
[preservation limits](operations/beta3-compatibility.md) if you have existing data.
Do not run these commands over an existing installation's storage. The default
Compose file has fixed container/network names and ports, so a second simultaneous
installation needs explicitly isolated names, ports and storage.

Use the published `v1.0.0-beta.5` tag. Its accepted source SHA and checksums are
listed in the [release](https://github.com/DATAPACT/inLUMEN/releases/tag/v1.0.0-beta.5).
For prepublication verification, substitute the exact candidate SHA. Run:

```sh
# Use the published tag; release verification uses the exact candidate SHA.
INLUMEN_REF=v1.0.0-beta.5
git clone https://github.com/DATAPACT/inLUMEN.git inlumen-beta5
cd inlumen-beta5
git checkout --detach "$INLUMEN_REF"
git rev-parse HEAD
python3 - <<'PYCONFIG'
from pathlib import Path
import secrets
p = Path('.env')
if p.exists():
    raise SystemExit('Refusing to replace an existing .env')
settings = {
    'AUTH_ENABLED': 'false',
    'INLUMEN_API_PUBLIC_URL': 'http://localhost:5000',
    'API_AUTH_TOKEN': secrets.token_urlsafe(32),
    'INLUMEN_CODEGEN_SERVICE_API_KEY': secrets.token_urlsafe(32),
    'INLUMEN_RUNNER_SERVICE_API_KEY': secrets.token_urlsafe(32),
}
lines = Path('.env.example').read_text().splitlines()
with p.open('x') as f:
    p.chmod(0o600)
    for line in lines:
        key = line.split('=', 1)[0]
        f.write(f'{key}={settings[key]}\n' if key in settings else line + '\n')
PYCONFIG
docker compose up -d --build
docker compose ps
curl --fail http://localhost:5000/ready
```

The configuration command uses Python 3 from the host and refuses to overwrite
an existing `.env`. It creates three distinct service tokens without printing
them. Open [the local editor](http://localhost:8080) after readiness succeeds.
If readiness is still starting, inspect `docker compose logs backend runner
codegen` and retry the readiness request. This development topology uses one
shared local workspace and is intended for a controlled host, not public access.
The [multi-user guide](production-multi-user.md) covers the separate Keycloak
production topology.

A source archive can be extracted instead of cloning. Run the configuration and
Compose commands from its root and retain its filename/checksum; `git` commands
require a checkout. No local Node installation is required for the container
workflow; native frontend development requires Node 22.12+ and `npm ci`.

## Import, attach, and save

1. On an empty evaluation canvas, use **Import** to select
   [`pipeline.json`](../examples/order-summary/pipeline.json). It contains three
   connected components: Orders CSV → Summarize orders → Summary JSON.
2. Select **Orders CSV**, open **Inspector**, and upload
   [`orders.csv`](../examples/order-summary/orders.csv) under **Input Files**.
3. Select **Summarize orders** and attach
   [`main.py`](../examples/order-summary/main.py) as its Python runtime package.
   It uses only the Python standard library; no `requirements.txt` is needed.
4. Open **Library → Run → Runtime code** and choose **Download code ZIP**. The
   downloaded `pipeline-code.zip` contains the Task's code in its own folder; it
   does not include `orders.csv` or the pipeline design. The existing uploader can
   use the archive with a matching Task that does not already have code attached.
5. Use **Save** to save a named version, such as `Order summary example`.
   Reload the browser. Check that all three components, their connections, the
   Source input, and the Task script are still present.

The JSON file contains the design only. Uploading the two files is intentional:
design JSON import does not package or restore code and data. An existing canvas
is replaced on import; use a fresh evaluation workspace or save its version first.

## Run and check the result

Open **Library → Run**, then choose **Run current pipeline**. The run captures
the saved graph and attached files. Wait for success and download `summary.json`
from **Results**. Its contents must match
[`expected-summary.json`](../examples/order-summary/expected-summary.json):

```json
{
  "order_count": 3,
  "total_amount": "49.75"
}
```

Expand **Run details and technical logs** and choose **Download tested snapshot**.
The archive contains the exact tested runtime and outputs. Its root
`docker-compose.yml` starts the exported Dagster project with
`docker compose up --build`; Dagster opens at `http://localhost:3000`.
That runtime bundle is distinct from the design JSON import format.

The Task reads `orders.csv` from `PIPELINE_INPUT_DIR` and writes `summary.json`
to `PIPELINE_OUTPUT_DIR`. Its runtime directories are provided by inLUMEN;
no port-named subdirectories or manifest files are required in user code.

## Save a reusable pipeline

After validating the example, open **Library → Lab → Reusable pipelines → Manage**.
Choose **Save current canvas**, name it `Order summary`, and save it.
It appears as one item in the Reusable pipelines catalog, without versions.
Choose **View pipeline** on its card, in Manage, or in a Subpipeline inspector
to explore its graph and component details in a read-only viewer. The viewer
supports pan and zoom and leaves the main canvas unchanged.
The saved definition and attached files cannot be edited. To change the design,
build it on the canvas and save a new reusable pipeline with a different name.

Build a parent pipeline on another saved canvas and drag that catalog item
onto it. The new component already points to its saved reusable pipeline.
There is no empty Subpipeline tile. An empty catalog explains how to
create the first reusable pipeline.

Only one subpipeline level is supported: a main pipeline may contain reusable
components, but a reusable pipeline cannot itself contain a Subpipeline.
Legacy nested definitions are marked unavailable and cannot be attached or
executed. Existing legacy version references remain readable for compatibility.
Saving a reusable design validates its graph, but does not prove that every
parent execution works.

## Troubleshooting and reset

- **Frontend fails after an image update:** rebuild the frontend with
  `docker compose build frontend`, refresh its dependency volume with
  `docker compose run --rm --no-deps frontend npm ci`, then run
  `docker compose up -d frontend`. This preserves application data. The
  development image requires the same supported Node runtime as the production
  build; Node 18 cannot start the current Vite version.
- **Run is unavailable:** check `docker compose ps`, backend readiness, and the
  runner/codegen logs. Native execution requires their private service tokens
  and working Docker access.
- **Missing input:** attach `orders.csv` to Orders CSV, preserving the filename.
- **Missing implementation:** attach `main.py` to Summarize orders, not to the
  Source or Destination.
- **Depth-limit error:** remove Subpipeline components from the canvas before
  saving it as a reusable pipeline; save the enclosing graph as an ordinary
  pipeline version instead.
- **Repeat the example:** import its design JSON into an empty evaluation
  canvas and attach the two files again. **Clear canvas** affects the drawing;
  **Clear all** removes the workspace's history and saved reusable pipelines.
- **Stop the installation:** `docker compose down` stops its services while
  retaining persisted data. Keep storage and encryption credentials when
  restarting. For backup and recovery, follow the
  [operations guide](hardening-and-operations.md).

## Verification scope

To reproduce bundle generation and real container execution without touching
an application workspace, install the documented backend/codegen development
dependencies and run:

```sh
backend/.venv/bin/python scripts/verify_quickstart.py --materialize
```

The script substitutes local files for object-store reads, builds the actual
deployment bundle, executes it through the codegen Dagster validator in Docker,
and checks both generated `summary.json` outputs. It prints the temporary bundle
directory, including `verification.json` with the execution report. Omitting
`--materialize` verifies bundle generation only.

On September 9, 2026, this example passed real Docker execution on an ARM64 host
with Docker Engine 29.7.2 and Dagster 1.13.12. The selected runtime profile was
`lightweight` (1 CPU, 1 GiB). The output was 3 orders totaling 49.75. This is
generated-bundle evidence, not a claim that the live UI walkthrough or full
evaluation topology has passed.

Record the candidate SHA, host/Docker versions, readiness result, saved version,
run ID/status, downloaded output, and tested-snapshot checksum when performing
the live walkthrough. Automated browser tests use a simulated gateway; the
database integration tests and a generated-bundle execution are separate checks.
They do not establish live Keycloak login, two-user deployment isolation,
restart recovery, or a full application restore. Those remain release gates in
the [release plan](release-plan.md).

## Beta 3 verification follow-ups

See the [real authentication and recovery record](operations/beta3-auth-recovery.md)
for two-user Keycloak login, API isolation, and representative same-revision data
restoration. The [compatibility policy](operations/beta3-compatibility.md)
describes fresh installation and preservation limits; no Beta 2 upgrade or
concurrent-user capacity is certified. Final release acceptance remains tracked
in [issue #129](https://github.com/DATAPACT/inLUMEN/issues/129).
