# Artifact contract v2 to v3

New deployment exports use `inlumen.deployment-bundle@2`,
`inlumen.run-spec@3`, and `inlumen.artifact-contract@3`.

## What changed

- v2 exposed orchestration ports as `PIPELINE_INPUT_DIR/<input-port>/...` and
  `PIPELINE_OUTPUT_DIR/<output-port>/...`.
- v3 stages upstream artifacts directly beneath `PIPELINE_INPUT_DIR` and expects
  Task results directly beneath `PIPELINE_OUTPUT_DIR`.
- Executable Tasks expose one logical output artifact set. A single output may
  fan out to multiple consumers; distinct output sets require an explicit split
  Task.
- Input publication is atomic. A failed copy or collision leaves the previous
  complete workspace untouched.
- Duplicate-path comparisons are size-checked and streamed with bounded memory.

## Compatibility

Existing v1/v2 run specifications and deployment-bundle@1 manifests remain
accepted by deployment validation and keep their original semantics. They are
not silently rewritten as v3. Regenerate a bundle to adopt the flat workspace.

Task implementations already reading and writing files directly at the two
environment-provided roots require no code change. Implementations that include
port directory names must remove those orchestration-specific path components
when regenerated for v3.
