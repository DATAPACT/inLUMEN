# Beta 6 compatibility statement

Beta 6 is a prerelease for controlled evaluation. **An in-place upgrade from
Beta 5 is not certified.** The supported path is the same as before: a fresh
installation, with pipelines preserved and rebuilt as described in
[compatibility and data preservation](beta3-compatibility.md).

This page reports what was checked on 2026-11-26 when Beta 5 data was opened
with the code on `main`, so that an evaluator who upgrades in place knows what
is known and what is not. The run output is in
[the evidence record](evidence/beta6-2026-11-26/compatibility.json).

## What a Beta 5 installation must do

1. **Regenerate generated packages** before a new export. Exports use artifact
   contract v4; packages generated with Beta 5 show as stale. See
   [contract v3 to v4](../migrations/artifact-contract-v3-to-v4.md). Historical
   snapshots and exported runtime source are not rewritten.
2. **Review the new settings.** `INLUMEN_RUN_ARTIFACT_STORAGE`,
   `INLUMEN_RUN_ARTIFACT_RETENTION_DAYS`, `MINIO_ENDPOINT` and `MINIO_SECURE` are
   new in both Compose files. The production file also adds the
   `CODEGEN_EXECUTION_*` limits, `CODEGEN_ML_CPU_THREADS` and
   `VITE_REVIEW_AI_CHANGES_DEFAULT`. All have defaults; compare with
   `.env.example` and `.env.production.example`.
3. **Keep a backup of the old installation** until the pipelines have been
   checked. Checking out the Beta 5 tag again is not a database rollback.

## What was checked

| Check | Result |
|---|---|
| PostgreSQL schema: Beta 5 bootstrap, then the `main` bootstrap on the same database, compared with a database created by `main` alone | Identical (12 tables). No table created by Beta 5 needs a change |
| Tables added since Beta 5 | Six, all created on first use: five for workspace chat (`chat_*`) and `installed_plugins` |
| Pipeline saved with Beta 5, opened with `main` | Same 3 steps and 2 connections; step data identical (same checksum) |
| The opened pipeline saved again with `main` | Accepted |
| Pipeline version saved with Beta 5 | Listed and restored with `main`; the restored graph is identical |

The pipeline was `examples/order-summary/pipeline.json`. The stores were
disposable PostgreSQL 17, Neo4j 5 and MinIO containers, with authentication
off. A Beta 5 backend wrote the data, and the backend at commit `f309e5f` was
then started on the same stores.

## What was not checked

- Pipelines with attached files, node secrets, LLM settings or reusable
  pipelines.
- Run history, queued or failed jobs, and generated packages in object storage.
- An authenticated installation with several workspaces.
- A full installation upgrade with the frontend, the runner and the codegen
  service.

For these, use the fresh-installation path.
