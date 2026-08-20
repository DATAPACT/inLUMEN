# inLUMEN integration

The browser starts or reconnects to a background generation job through the
backend. The backend authenticates to the private codegen API using
`INLUMEN_CODEGEN_SERVICE_API_KEY`. It forwards the selected code model and
provider metadata in the request, plus the provider key in an ephemeral header.
The chat/design model is not used for code generation.

On a valid result, the backend persists `main.py`, `requirements.txt`,
`node-manifest.json`, and `validation-report.json` for each executable node.
Regeneration deletes stale generated files, including legacy per-node
Dockerfiles, without deleting user input/test-fixture files.

Deployment export is a separate stage. It derives deterministic build metadata
from the persisted runtime packages and can create Dagster and Argo targets in
one bundle.
