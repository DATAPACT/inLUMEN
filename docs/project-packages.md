# Project packages

Use **Package** in the canvas toolbar. **Export package** downloads the saved
design as `inlumen-project.zip`. Wait for autosave to finish before exporting.
The archive includes attached code and input data, so large source files count
toward the package limit.

In the destination workspace, choose **Package → Choose package**. The preview
shows the project name and counts of canvas nodes, attached files, reusable
pipelines and secret parameters. **Replace canvas and import** replaces the current
canvas. Saved versions remain available. Import does not execute code.

Canvas nodes receive fresh identities, and reusable pipelines are recreated under
fresh identities. Existing definitions are not modified; duplicate names receive
an import suffix. Connections and reusable references are remapped to the imported
objects. Python environment-variable suggestions are rebuilt from the attached
code. Supply secret parameter values in the Inspector after import.

## Contents and limits

The ZIP contains `manifest.json` with format `inlumen.project-package@1` and
attachments under `files/<sha256>`. The manifest includes the design and referenced
single-level reusable definitions. File entries carry their original filename,
role, archive path and SHA-256 checksum. Duplicate content shares one ZIP entry.

The design preserves labels, descriptions, positions, ports, connections, template
and implementation settings, ordinary runtime parameter values and secret names.
Secret runtime values, storage locations, run history, validation reports and
generation metrics are excluded. Attached file bytes are unchanged; secrets
written inside a file are therefore part of that file. This is a project transfer,
not a workspace backup: saved versions, unrelated reusable pipelines and service
configuration are not included.

Archives are limited to 50 MiB compressed and expanded, 1,000 ZIP entries, a 5 MiB
manifest, 200 reusable definitions, and the existing per-graph canvas limits.
Export reserves 5 MiB for its manifest and limits aggregate attachments to 45 MiB.
Paths, entry types, checksums, graph shape and reusable boundaries are validated
before importing. Missing attachments or unresolved/nested reusable references
fail explicitly. Arbitrary ZIPs from the code uploader or deployment exporter are
not project packages.

## Persistence and API

`GET /api/pipeline/package` exports a workspace-scoped graph and its attachments
under the graph revision lock. `POST /api/pipeline/package?preview=true` validates
raw ZIP bytes without changing storage or the graph. `POST /api/pipeline/package`
imports the same bytes and requires `If-Match` with the current graph revision.
The UI also rejects a preview if the workspace changed since selecting the file.

Imports stage immutable attachment objects in the destination workspace's snapshot
bucket, then create reusable definitions and replace the canvas in one Neo4j
transaction. Conflicts are rejected before staging. Storage or graph failures
leave the active graph unchanged. A failed or ambiguous commit may leave orphaned
immutable objects; the failure path does not delete potentially committed files.

Tests: `backend/tests/test_project_package.py`,
`backend/tests/test_project_package_integration.py`, and
`frontend/e2e/project-package.spec.ts`. Integration tests require disposable Neo4j
and MinIO with `RUN_NEO4J_INTEGRATION=1` and `RUN_MINIO_INTEGRATION=1`.
