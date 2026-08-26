# ADR 0001: V2 export and output contracts

- Status: Accepted
- Date: 2026-08-12
- Issue: [#85](https://github.com/DATAPACT/inLUMEN/issues/85)

## Context

inLUMEN is a visual design and export tool. It owns pipeline definitions,
generated runtime packages, deployment bundles, validation, and design
provenance. Dagster is the local validation runner and Argo is an optional
Kubernetes export; inLUMEN is not a general-purpose artifact store or execution
event bus.

V2 already emits versioned payloads in several places. Replacing them with one
large proprietary envelope would add migration risk without improving
interoperability. The remaining need is to state which contract applies at each
surface, publish its schema, and consistently classify non-tabular artifacts.

## Current surface inventory

| Surface | Producer | Consumers | V2 contract/format |
| --- | --- | --- | --- |
| Project JSON export/import | Canvas | UI, source control, external tools | `inlumen.project@2` JSON Pipeline IR |
| Generated node package | Codegen service | Backend, deployment builder | Native source files plus `node-manifest.json` and validation report |
| Deployment bundle | Backend exporter | UI ZIP download, Dagster, Argo, CI | Deterministic files plus `inlumen.deployment-bundle@2`, `inlumen.run-spec@3`, and `inlumen.artifact-contract@3` (validators retain legacy compatibility) |
| Node output | Generated runtime | Downstream nodes, Dagster validator | Native artifact plus `inlumen.output-manifest@1` descriptor |
| Pipeline run result | Deployment validation | UI and downloaded bundle | `inlumen.run-result@1` JSON referencing output files |
| Public workflow/artifact API | Backend | DATAPACT integrations and SDKs | OpenAPI-described JSON and native YAML/file responses |
| Design provenance | Backend | UI download, audit/interchange tools | W3C PROV-O JSON-LD; PDF is the human report |
| Chat transcript | UI | Human user | Markdown; not a pipeline/data contract |

## Decision

Use a small versioned inLUMEN control-plane envelope and keep data-plane bytes in
established native formats. The JSON Schemas in `contracts/v2` and `contracts/v3`
are the normative definitions for their respective contract majors. Runtime
identifiers use `inlumen.<contract>@<major>` and fields
use `snake_case`, matching the backend and generated runtime contracts.

### Standards decision matrix

| Concern | Decision | Rationale |
| --- | --- | --- |
| Control-plane contracts | JSON Schema Draft 2020-12; OpenAPI for HTTP | Machine validation and existing backend/API tooling |
| Tables | Parquet/Arrow for typed bulk interchange; CSV for simple exchange | Preserve types and scale where needed without banning familiar CSV |
| JSON, text, media, documents | Native format plus IANA media type | Avoid base64 or custom wrappers unless transport requires it |
| Large/binary/model artifacts | Durable URI plus size and SHA-256; native model format | Keep large data out of graph/run JSON and make persisted content verifiable |
| Deployment | Current deterministic ZIP layout; OCI images for runnable containers | ZIP is directly usable today; OCI is used where the product actually builds images |
| Provenance | W3C PROV-O JSON-LD | Already implemented and suited to design decision history |
| Runtime lineage | OpenLineage at external execution boundaries, when enabled | Maps jobs, runs, and datasets; not required for design-only operations |
| Lifecycle events | CloudEvents 1.0 only when publishing asynchronous events | Events remain notifications and never replace run or artifact outputs |
| Supply chain | CycloneDX or SPDX SBOM alongside distributed images, when produced | Use established BOM formats; do not invent an inLUMEN dependency document |

We explicitly reject a universal JSON payload, embedding large artifacts in
project exports, a proprietary model binary, and treating provenance or events as
ordinary node outputs.

## Canonical artifact descriptor

An artifact declares a logical `kind`, physical `format`, and preferably
`content_type`, `semantic_role`, `size_bytes`, and `sha256`. It contains exactly
one practical location strategy:

- `inline_value` for a small JSON-compatible value;
- bundle-relative `path` for a file inside an export or run directory; or
- durable `uri` for large, streamed, content-addressed, or externally persisted
  data.

The kinds are `table`, `json`, `text`, `image`, `audio`, `video`, `document`,
`model`, `directory`, and `binary`. The example manifest in
`contracts/v2/examples` covers every kind.

## Deterministic deployment layout

```text
README.md
bundle-manifest.json
run-spec.json
inputs/
  input_manifest.json
nodes/<stable-node-slug>/
outputs/<stable-node-slug>/
argo/                       # when selected
dagster/                    # when selected
validation/
runs/<run-id>/run-result.json
```

Entries are sorted by path before download. Paths are relative, use `/`, and may
not contain `..`. Node directories derive from stable flow IDs, not labels alone.
File entries retain native bytes and media types; transport-only base64 is
declared with `content_encoding`.

## Task workspace contract

Every Task receives exactly two public filesystem locations:
`PIPELINE_INPUT_DIR` and `PIPELINE_OUTPUT_DIR`. Upstream artifacts are staged at
their artifact-relative paths directly beneath the input root, and Task results
are written directly beneath the output root. Graph port names remain
orchestration metadata and never create implicit directories visible to Task
code. The orchestrator may use private staging directories for routing, but it
must flatten that boundary before starting the Task. Conflicting upstream paths
fail deterministically instead of being overwritten.

An executable Task publishes exactly one logical output artifact set. That set
may fan out to any number of downstream consumers. Pipelines needing two
independently routed result sets use an explicit split Task, keeping user code
independent from orchestration port directories.

## Status and errors

Run results use `succeeded`, `partial`, `failed`, or `cancelled`. A failed or
cancelled result may include a stable error `code`, human-readable `message`, and
structured `details`. HTTP APIs continue to use appropriate status codes and
their documented error bodies; an event notification is not the run result.

## Versioning and migration

Consumers must reject unsupported major versions and may ignore unknown optional
fields in a supported major. Additive optional fields are backward compatible.
Meaning changes, removals, or newly required fields require a new major schema.

The flat Task workspace therefore uses `inlumen.artifact-contract@3`,
`inlumen.run-spec@3`, and `inlumen.deployment-bundle@2`. The earlier
port-namespaced schemas remain published under `contracts/v2`, and validators
continue to accept their bundle/run-spec identifiers. New exports never emit
the legacy layout.

The UI imports `inlumen.project@2` and migrates legacy unversioned/raw React Flow
documents. It does not silently interpret an explicitly unknown schema version as
legacy. Other V1 runtime identifiers remain stable for V2 and can evolve
independently.

## Security and reproducibility

Exports exclude API keys, credentials, bearer tokens, signed URLs, and unmasked
secret parameters. Sample data is included only where the user explicitly
attached it for validation. Persisted binary artifacts carry byte size and
SHA-256 when practical. Runtime packages pin their declared environment and keep
validation reports next to the generated implementation.

## Consequences

This keeps the release change small: existing V2 envelopes remain compatible,
the contract becomes inspectable and testable, and audio/video/documents no
longer collapse into `binary`. CloudEvents, OpenLineage, OCI artifact manifests,
and BOM generation remain integration points to add when a real transport,
lineage backend, registry, or distribution workflow needs them.
