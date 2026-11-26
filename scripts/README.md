# Scripts

Operations and CI tooling only. Evaluation scripts go to `evals/<name>/scripts/`
(see `evals/README.md`). Names use `snake_case`; `test_*` is kept for pytest
files, so a script that verifies something by hand or in CI is named `check_*`
or `verify_*`.

| Script | Purpose |
|---|---|
| `run_tests.py` | Run the regression suite of one or all components (used by CI) |
| `sync_shared.py` | Generate the service copies of shared modules and the browser contracts; `--check` in CI |
| `deploy_vm.sh` | Deploy one commit's published images to the production Compose project |
| `backup_vm.sh` | Incremental restic backup of the production data, run weekly from cron |
| `backup_volumes.py` | Offline backup and isolated restore of a Compose project's volumes |
| `prune_workspace_blobs.py` | Report or delete aged, unreferenced blobs in one workspace |
| `check_postgres_workers.py` | Check worker claims and deletion markers on a disposable PostgreSQL (CI) |
| `check_runtime_container.py` | Build a disposable Dagster bundle and check non-root, read-only execution |
| `verify_quickstart.py` | Build the quickstart example and optionally run it in a Dagster container |
| `stress_test.sh` | Run the Playwright load test from a separate host |
