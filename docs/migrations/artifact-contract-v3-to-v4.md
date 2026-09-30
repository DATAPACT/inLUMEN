# Artifact contract v3 to v4

New packages use `inlumen.generic-node@2`; pipeline generation plans use
`inlumen.pipeline-plan@2`. Exports use `inlumen.artifact-contract@4`,
`inlumen.run-spec@4`, and `inlumen.deployment-bundle@3`.

## Publication

Each connection carries one declared file or directory bundle. A bundle is an
explicit output declaration, never an automatic wrapper around arbitrary files.
Publish the declared relative path under `PIPELINE_OUTPUT_DIR`; put temporary
files in `PIPELINE_WORK_DIR`. The runtime validates publication before success
and stages only this artifact for each consumer. Sources obey the same rule.
Multiple uploads require `output_artifact` in the source's implementation or
parameters, with `name`, `filename`, `kind: directory`, and
`representation: directory`.

Generated functions still accept `(inputs, output_dir, context)`. Each connected
input descriptor carries its producer, connection ID, and materialized path.
Open that path; do not scan directories or choose the first file containing
recognizable data. Directory contents remain grouped under one descriptor.
Different incoming connections must have distinct input-relative paths.

A producing function returns a one-element descriptor list. Terminal destinations
can return no artifact; delivery receipts are not pipeline connections. For
uploaded filesystem tasks, declare the same contract in `node-manifest.json`.
The runtime validates their declared output even when the script writes no receipt.

## Validation and migration

Missing outputs, extra published results, wrong representation, invalid JSON
schemas, missing CSV columns, undeclared bundle members, and paths escaping the
artifact root fail before downstream execution. Internal metadata and scratch
files never count as additional artifacts.

Regenerate existing generated packages before creating new-format exports. An
uploaded package can instead supply an explicit version-2 data contract. Package
configuration hashes include the contract version; older generated packages show
as stale. Historical snapshots and exported runtime source are not rewritten.

Sample pipeline validation executes the compiled packages through shared artifact
staging and publication, rather than calling their canonical functions in-process.
Model execution may still be deferred when dependencies are unavailable; this is
reported separately and does not constitute an end-to-end pass.

## Verification

Deterministic boundary tests live in `backend/tests/test_artifact_runtime_v4.py`.
The Docker integration case uses the exported Dagster component loader and proves
that connection IDs stay strings, anonymization output reaches its consumer,
and an inactive upstream asset leaves its consumers skipped. Run it against a
built Dagster runtime image with `INLUMEN_ARTIFACT_TEST_IMAGE=<image>` when running
that test module. Other fixtures exercise the exported Argo runner directly,
including retries, directory bundles, schema errors, and undeclared output.

## User-uploaded code ZIPs

Upload one Task folder containing `main.py`, optional `requirements.txt`, and
`inlumen.task.json`. New packages use the public version-1 declaration documented
in [Portable Task packages](../task-packages.md). Its output declares an exact
relative path, file/directory type, and optional format and content schema.
Legacy manifests containing `data_contract` with `contract_id: inlumen.generic-node@2`
remain supported through a compatibility adapter.

The upload API validates this declaration before storing it; package preparation
preserves it in the runtime-generated `node-manifest.json`. Output filenames must
match the actual script, including for filesystem Tasks that do not return a
manifest. The platform does not infer output filenames by inspecting arbitrary
Python, relabel a legacy package as current, or wrap undeclared files as a bundle.

The ZIP importer checks declarations before upload and allows reviewed replacement
of existing Task files. It reports missing declarations at import time. The
`examples/uploaded-audio-pipeline` packages demonstrate the format and explicit
input-descriptor resolution. Older snapshots retain their existing semantics.

Dependency aggregation preserves wheel URLs and named HTTP(S) references,
including model wheels, during runtime builds.
