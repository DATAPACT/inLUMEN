# inLUMEN flat runtime contracts

These schemas define the current engine-neutral deployment ABI emitted by
inLUMEN. Contract majors evolve independently from the product release number.

| Contract | Runtime identifier | Schema |
| --- | --- | --- |
| Deployment bundle manifest | `inlumen.deployment-bundle@2` | `deployment-bundle.schema.json` |
| Artifact directory ABI | `inlumen.artifact-contract@3` | `artifact-contract.schema.json` |
| Engine-neutral run specification | `inlumen.run-spec@3` | `run-spec.schema.json` |
| Pipeline run lifecycle | `inlumen.pipeline-run@1` | `pipeline-run.schema.json` |

The lifecycle record references the independently versioned terminal
`inlumen.run-result@1` schema retained in `contracts/v2`.

## Compatibility

The v3 artifact ABI removes implicit port-named directories from the public Task
workspace. Upstream artifacts are staged at their artifact-relative paths
directly beneath `PIPELINE_INPUT_DIR`, and Task results are written directly
beneath `PIPELINE_OUTPUT_DIR`. Every executable Task exposes one logical output
artifact set; that output may fan out to any number of downstream consumers.

Validators retain support for legacy deployment-bundle@1 and run-spec@1/@2
bundles. New exports always emit the versions listed above.
