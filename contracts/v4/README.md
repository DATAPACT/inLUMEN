# Single-artifact handoff

New exports use `inlumen.artifact-contract@4`, `inlumen.run-spec@4`, and `inlumen.deployment-bundle@3`. Generation uses `inlumen.pipeline-plan@2` and `inlumen.generic-node@2`.

Each connection binds one declared file or directory artifact. Directory contents are members, not additional artifacts. Nodes with multiple incoming connections receive distinct descriptors carrying connection and producer identity. A producer can fan out the same artifact. Terminal destinations need not publish an artifact.

Task files remain under `PIPELINE_INPUT_DIR` and `PIPELINE_OUTPUT_DIR`. Scratch space is `PIPELINE_WORK_DIR`; platform receipts are kept outside published output. Only the declared path is staged. Missing, extra, malformed, unsafe, or incorrectly typed outputs fail before downstream execution. JSON content uses JSON Schema 2020-12 validation; CSV required columns and declared bundle members are checked.

Prior contract schemas and saved bundle source remain available with their original semantics. New exports require regeneration or explicit new-contract metadata for old packages.
