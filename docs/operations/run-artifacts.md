# Pipeline run outputs and lineage

When a pipeline runs, codegen executes every step in one Dagster container.
Afterwards it uploads each step's published outputs to MinIO and returns
references to the runner. The bytes never pass through the runner or the run
record. The run panel's viewer and downloads read the objects straight from
MinIO, so previews of large files fetch only the byte ranges they show.

## Layout

- Bucket: one per workspace. It is `run-artifacts-local` for the local
  workspace and `run-artifacts-ws-<sha256(workspace)[:20]>` otherwise.
- Outputs: `runs/<run_id>/<public output path>`, for example
  `runs/<run_id>/outputs/node-2-split/rows/rows.csv`.
- Step receipts: `runs/<run_id>/steps/<flow_id>.json`. Each one records what
  the step read and published, with sha256 digests. The run result keeps the
  same facts under `steps`.

The backend serves an object only when its bucket and key are exactly the ones
derived from the caller's workspace, the run id and an output listed in that
run's result.

## Settings (codegen)

| Variable | Default (Compose) | Meaning |
| --- | --- | --- |
| `INLUMEN_RUN_ARTIFACT_STORAGE` | `minio` | `minio` stores outputs by reference. Any other value keeps them inline on the runner, as before. |
| `INLUMEN_RUN_ARTIFACT_RETENTION_DAYS` | `30` | A bucket lifecycle rule expires `runs/` objects after this many days. Leave it empty to keep them. |
| `MINIO_ENDPOINT`, `MINIO_SECURE` | `datapact-minio-db:9000`, `false` | MinIO address. |
| `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD` | same as the backend | Credentials. `MINIO_ACCESS_KEY` and `MINIO_SECRET_KEY` also work. |

If MinIO is not configured, or an upload fails, the run's outputs are kept
inline on the runner instead and the run gets a `run.artifact_storage` event.
Runs recorded before this change keep working from the runner's disk.

## Cleanup

Clearing runs (`DELETE /api/pipeline-runs`) also deletes the workspace's
`runs/` objects and reports `removed_artifact_objects`. The lifecycle rule
removes anything older than the retention period.

## Lineage

`GET /api/provenance/prov-o?run_id=<run_id>` returns the run as PROV-O JSON-LD:
- a `PipelineRun` activity;
- one `StepExecution` per step;
- artifacts identified by content digest.

Because of the digests, the file a producer generated and the file a consumer
used are the same entity. The run panel offers this file as **Download run
lineage**.
