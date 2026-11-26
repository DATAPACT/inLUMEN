# inLUMEN 1.0 Beta 6 — Typed data flow, run outputs and plugins

Beta 6 records what has been merged to `main` since Beta 5 (16 September 2026).
The deployed instance already runs this code; the release checks and tags it.

## Changes

### Pipeline model

- **Typed ports.** Every port has a data type taken from the step's declared
  output. A connection between different kinds of data is a validation error; a
  mismatch that cannot be checked is a warning. The editor does not refuse the
  connection (#170).
- **Parallel branches and Parallel Map.** A step can feed several branches. A
  Parallel Map runs the steps after it once per item and is closed by a Collect
  step (#170).
- **Multi-file Sources.** A Source can carry several uploaded files as one
  directory artifact (#169).
- **One artifact per output port.** A step publishes exactly the output it
  declares. This is artifact contract v4 (#153).

### Running pipelines

- Run outputs are stored in object storage with the lineage of each step, and
  can be opened in the application in compact, table or raw view (#152, #154,
  #159).
- Results of unchanged steps are reused, and a failed run resumes from the
  failed step (#155).
- The pipeline activity list shows finished steps as finished (#189, #191).

### Export

- **Argo Workflows bundle.** A self-contained bundle with image build, setup and
  submit scripts; models can be downloaded to a volume or included in the image;
  each step has CPU and memory requests (#162, #164, #167).
- **Portable Task packages.** A Task is a folder with `main.py` and
  `inlumen.task.json`, which declares its output (#144). See
  [Portable Task packages](task-packages.md).

### Plugins

- Third-party tools with a REST API can be installed as Sources, Tasks or
  Destinations from a manifest (#148, #156, #157, #160). See
  [Plugins](plugins.md).

### Agents

- The pipeline design agent and the code generator are published as the
  `inlumen-agents` library, version 1.0.0b6 with this release (#171).
- The design agent builds a pipeline in one tool call, and its changes can be
  previewed before they are applied (#141, #183, #184).
- A plain delete request ("delete language detection") is carried out (#193).
- Code generation repairs only the failing step when the errors name it, and
  regenerates the whole module otherwise (#185).

### Interface

- The inspector no longer has a plugin picker, palette groups are compact, and
  the Run tab has several fixes (#168).
- "Reload saved graph" completes when an autosave is queued during the reload
  (#202).

### Operations

- Application images are published to GHCR after the tests pass, and the
  reference VM is deployed from them on each merge to `main` (#176, #177).
- Backups run weekly with restic instead of before each deploy (#180).

## Compatibility

Beta 6 does not certify an in-place upgrade from Beta 5. What was checked, and
what a Beta 5 installation must do, is in the
[compatibility statement](operations/beta6-compatibility.md). In short:

- Generated packages must be regenerated before a new export
  ([contract v3 to v4](migrations/artifact-contract-v3-to-v4.md)).
- The PostgreSQL schema created by Beta 5 is accepted unchanged; new tables are
  added on first use.
- A pipeline and a pipeline version saved with Beta 5 opened and saved again
  unchanged in the check. The check used one small pipeline.

## Supported evaluation and limits

This is a prerelease for controlled evaluation. Review generated code and
dependencies before running them. The codegen service controls Docker and must
remain private. Dagster is the native execution engine; Argo is export only, and
inLUMEN does not run the exported workflow. Native resource profiles are
CPU-based. Nested reusable pipelines remain unsupported. The release makes no
new production TLS, migration, identity-provider recovery, capacity, or
model-quality claim.

See [verification](operations/beta6-verification.md) for the checks that were
run and their limits.

The repository tag is the product version. `inlumen-agents` follows it
(`v1.0.0-beta.6` is 1.0.0b6); other component package versions remain internal
metadata. Source is licensed under [Apache-2.0](../LICENSE); dependencies retain
their own licenses. Report ordinary bugs in
[GitHub issues](https://github.com/DATAPACT/inLUMEN/issues), with the source SHA
and redacted reproduction details. Report vulnerabilities through
[private reporting](https://github.com/DATAPACT/inLUMEN/security/advisories/new);
see the [security policy](../SECURITY.md).
