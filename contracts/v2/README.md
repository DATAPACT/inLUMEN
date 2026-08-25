# inLUMEN V2 contracts

This directory is the machine-readable source of truth for V2 portable outputs.
The schemas use JSON Schema Draft 2020-12 and match the contracts emitted by the
Project JSON export, deployment builder, runtime output manifest, and Dagster run
result.

| Contract | Runtime identifier | Schema |
| --- | --- | --- |
| Pipeline definition | `inlumen.project@2` | `project.schema.json` |
| Deployment bundle manifest | `inlumen.deployment-bundle@1` | `deployment-bundle.schema.json` |
| Artifact directory ABI | `inlumen.artifact-contract@2` | `artifact-contract.schema.json` |
| Engine-neutral run specification | `inlumen.run-spec@2` | `run-spec.schema.json` |
| Pipeline run result | `inlumen.run-result@1` | `run-result.schema.json` |
| Node/pipeline output manifest | `inlumen.output-manifest@1` | `node-output-manifest.schema.json` |

## Compatibility

- `schema_version` is required and is the contract identifier. The number after
  `@` is the major version.
- Consumers must reject an unknown major version with a clear error. They may
  ignore unknown optional fields within a supported version.
- Additive optional fields do not require a new major version. Removing a field,
  changing its meaning, or narrowing accepted values does.
- Project import continues to accept unversioned V1/raw React Flow JSON and
  migrates it to the V2 graph. New exports always use `inlumen.project@2`.

## Artifact rules

Artifact metadata is JSON, but artifact bytes stay in their native format. Small
structured values may use `inline_value`; files use a bundle-relative `path`; and
large, streaming, or externally persisted artifacts use a durable `uri`.
Persisted artifacts should include `size_bytes` and a lowercase
`sha256:<64-hex-digits>` digest. Credentials, signed URLs, provider keys, and
unmasked secret parameters must never be exported.

At execution time, every connected artifact is routed from
`PIPELINE_OUTPUT_DIR/<source_port>/...` to
`PIPELINE_INPUT_DIR/<target_port>/...`. Orchestrators may use different physical
transports, but must expose that same Task-facing directory layout and isolate
mutable output paths by run id.

The canonical logical kinds are `table`, `json`, `text`, `image`, `audio`,
`video`, `document`, `model`, `directory`, and `binary`. `format` identifies the
physical encoding and `content_type` uses an IANA media type when one exists.

See [ADR 0001](../../docs/adr/0001-export-output-contracts.md) for the decisions,
surface inventory, and bundle layout.
