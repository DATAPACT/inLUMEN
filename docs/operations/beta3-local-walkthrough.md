# Beta 3 local installation and supplied-code walkthrough

Verified September 14, 2026. Baseline `27a8ae99dd429ba44ee79cbe8cd5abad330ab348`
failed frontend startup. The successful walkthrough used
`ffe3e9f87fd03351f4d23d9dff96768d59fe6924`, which changes only the development
frontend Dockerfile. Later CI/documentation changes do not alter the tested
application. This is acceptance evidence for that source revision, not a release
announcement or a frozen final candidate.

## Startup failure and fix

A fresh checkout and fresh application storage reproduced a restarting frontend:
Node 18.20.8 could not import `styleText` from `node:util` while loading Rolldown
for Vite 8.2.2. Host-based frontend CI had not exercised the development image.

The development Dockerfile now uses the same digest-pinned Node 22 Alpine base
as the production build and installs the committed lock with `npm ci`.
The repaired image starts Vite 8.2.2 on Node 22.23.2. CI now builds that image and
runs `npm exec vite -- --version`; this loads the CLI that failed on Node 18.
The dependency volume from the failed installation was recreated for this
isolated fixture so it could not shadow the repaired image's dependencies.
For an existing development install, the quickstart documents refreshing the
dependency volume with `npm ci` without deleting application data.

## Environment and isolation

- macOS 26.6.2 (25G83), ARM64, 18 GiB host RAM, 12 logical CPUs.
- Docker Engine 29.7.2; Compose v5.5.0; Docker VM: 12 CPUs, 8,319,238,144 bytes RAM.
- Separate checkout, Compose project `inlumen_beta3`, network
  `inlumen_beta3_isolated`, container names, image tags, bind storage, named
  volumes, and validation working directory. Existing application containers and
  data were left untouched. Docker's image/build cache was available; this is not
  evidence of a cache-empty or offline installation.
- `.env` started from `.env.example`, with three distinct random service tokens,
  `AUTH_ENABLED=false`, public API `http://localhost:15000`, frontend port 18080,
  and separate database/storage ports. Validation workdir:
  `/tmp/inlumen-beta3-runtime`; model volume prefix `inlumen_beta3_model_store`.
  The Compose override changed only isolation names and image tags.
- All seven application services ran with zero restarts after repair. Five
  configured healthchecks were healthy; the frontend returned HTTP 200 and
  MinIO served real uploads. `/ready` returned HTTP 200. Its `static_bearer`
  field describes public API readiness; the local editor uses the documented
  shared workspace with Keycloak disabled.
- [Service image IDs and status](evidence/beta3-local-2026-09-14/service-status.json).
  Development database image tags and generated runtime dependency resolution
  are not all immutable; record/recheck them for the final release candidate.

## Live browser workflow

No API responses, object-store reads, or execution services were mocked.

1. Imported `examples/order-summary/pipeline.json` through the browser file chooser.
2. Uploaded `orders.csv` under Source Input Files and `main.py` through the Task
   implementation upload. Both uploads used the actual file chooser interface.
3. Saved **Beta 3 order summary**, version
   `c6c75ce2-4ba1-4930-b626-5877b00ba2de`. Reloaded the editor: three nodes,
   two connections, both attachments, and no validation issues. Chrome's separate
   browser session also listed the named snapshot with three steps/two links/two files.
4. Launched **Run current pipeline** from Library → Run. The launch captured Main,
   whose graph matched the saved example, rather than selecting the named snapshot.
5. Run `c3df3c20f57c4f88bbad8603d05d4200` succeeded, with all three nodes completed.
   Start `2026-09-14T12:08:46.216914Z`; finish `2026-09-14T12:09:00.391181Z`
   (about 14.17 seconds, including runtime preparation). Dagster 1.13.12;
   lightweight worker profile: 1 CPU, 1 GiB. This single example is not a benchmark.
6. Downloaded `summary.json` and **Download tested snapshot** through Chrome.
   Verified the actual files saved in Downloads. The browser tool's download-event
   wait timed out even though Chrome saved them; no application download change
   was needed. The in-app browser was used for import/upload/save/run; its file-save
   behavior was not established.

The summary is exactly `{"order_count": 3, "total_amount": "49.75"}` after JSON
parsing. Both summary artifacts in the run receipt have SHA-256
`8448e5747c820c396122e490f98f4d887b676fecfa57f70129c0fc658aea0c60`.

Browser-downloaded ZIP: `inlumen-dagster-run-c3df3c20f57c4f88bbad8603d05d4200.zip`,
26,334 bytes, SHA-256
`82b46e19d316063da188eec158b93002c5ffa8fd0cc8a110d3ef05cfbe674d4a`.
The earlier direct-API ZIP has a different archive hash because ZIP metadata
changes; all 32 entry names and content hashes match the browser-downloaded ZIP.
The run's source snapshot digest is a separate identity from the result archive.

Evidence: [run receipt](evidence/beta3-local-2026-09-14/run.json),
[saved versions](evidence/beta3-local-2026-09-14/versions.json),
[downloaded summary](evidence/beta3-local-2026-09-14/summary.json), and
[archive entry hashes](evidence/beta3-local-2026-09-14/archive-content-sha256.json).

## Independent exported startup

Extracted the tested snapshot and ran its unmodified root Compose file:

```sh
docker compose -p inlumen_beta3_export up -d --build
```

PostgreSQL, the Dagster code server, and webserver became healthy; the daemon
was running. `http://localhost:3000` returned HTTP 200. The browser's Deployment →
Code locations screen showed `inlumen_dagster_project.definitions` **Loaded** with
one asset group. No additional materialization was launched in this exported
installation; the live inLUMEN run above establishes the example's execution.

## Remaining acceptance work

This verifies local no-auth installation after the Node fix, browser save/reload,
supplied-code execution, real downloaded output, and independent exported startup.
It does not establish real LLM generation, two-user Keycloak isolation, failure/
cancellation/restart behavior, upgrade/full application restore, capacity, or the
final source archive. Those gates remain open in issue #129. The Node fix needs
review/merge and CI evidence before selecting the final release candidate.
