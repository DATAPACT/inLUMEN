# Portable Task packages

A Task is a directory containing `main.py` and `inlumen.task.json`. The minimal declaration is:

```json
{"version": 1, "output": {"type": "file", "path": "result.json"}}
```

The versioned schema and examples live in `contracts/task-package-v1`. `shared/task_package_spec.json` generates the browser help, published schema, and service copies through `python3 scripts/sync_shared.py`. `shared/task_package_contract.py` is the shared validator and normalizer. Run `python3 scripts/sync_shared.py --check` in CI.

Output paths are exact paths relative to `PIPELINE_OUTPUT_DIR`. A directory bundle is one artifact. Optional input requirements identify a consumer port, not a producer ID or filesystem directory. The runtime supplies the graph connection identities and stages each bound artifact in `PIPELINE_INPUT_DIR`. `INLUMEN_INPUT_MANIFEST` identifies those artifacts; `PIPELINE_WORK_DIR` is for temporary files. A JSON output without a content schema is syntax-checked, with structural validation explicitly reported as unavailable.

The design uses explicit file/directory types and output bindings, principles also used by [CWL CommandLineTool](https://www.commonwl.org/v1.2/CommandLineTool.html). These packages are not CWL documents. Dagster and Argo continue to be the execution engines.

## Upload and replacement

`POST /api/pipeline/task-packages/validate` accepts multipart `file` and optional JSON `mappings` (folder → node ID). It returns the ZIP digest, graph revision, normalized declarations, proposed mappings, warnings and errors. No uploaded code is executed during validation.

`POST /api/pipeline/task-packages/import` accepts the same file and mappings, the reviewed `digest`, and `If-Match` with the reviewed graph revision. It revalidates, stages immutable objects, and replaces all reviewed Task references in one graph transaction. Dependencies and helpers omitted from a replacement stop being part of the active package. Failed validation, interrupted staging and graph conflicts leave the previous package references active. Unreferenced staged objects can be collected by storage retention tooling.

The ZIP limit is below 50 MiB, with a 200 MiB extraction limit and 4096 entries. Traversal, duplicate entries, links, special files and encrypted entries are rejected. Ambiguous Task names require an explicit mapping. Exported folders use `nodes/<Task name>--<node-id>/`. The label is readable context and the ID is stable identity, not sequence. Numeric-only folders from earlier exports remain accepted.

`GET /api/pipeline/task-packages/download` exports complete portable Task packages. Platform execution metadata and Dockerfiles are excluded. `GET /api/task-package-template` supplies the template and schema. The inspector and ZIP workflow use the same public help and validator.

## Dependencies and execution

Python requirements follow [standard dependency specifiers](https://packaging.python.org/en/latest/specifications/dependency-specifiers/), including extras, environment markers and named direct references. Raw wheel URLs are normalized to named references. Package-local requirement and constraint includes are resolved within the package root. Constraints are retained separately in exported runtimes, including constraints on transitive dependencies. Unsupported installer directives, unsafe includes and known contradictions produce errors; dependency installation failures remain explicit runtime-build failures.

Ordinary uploaded packages execute with `python main.py`. Generated functions retain their compiled launcher. Both pass through the existing artifact-contract v4 boundary: isolated attempt directories, exact publication, schema checks, atomic publication and bound delivery. No authoring-only runtime version bump is introduced.

Model acquisition is allowed during managed execution by default. A declared model does not force unrelated Tasks into offline mode. Reviewed adapters still resolve pinned snapshots from the read-only `INLUMEN_MODEL_ROOT`; ordinary libraries download missing models into a separate writable, disk-backed Hugging Face cache outside published outputs. Managed caches are isolated per job; exported Dagster Compose uses a separate runtime-cache volume. Explicit `HF_HUB_OFFLINE` and `TRANSFORMERS_OFFLINE` settings remain respected. Package validation does not prove model availability: a requested download can still fail because of network access, credentials, storage, or the selected model. Python distributions such as spaCy model wheels still belong in package dependencies.

The previous `data_contract` authoring format remains supported through a compatibility adapter. Conflicting public and internal output declarations are rejected. Historical snapshots are not rewritten. Packages missing a declaration need an explicit manifest, not AI regeneration.

## Regression coverage

Portable manifests may include pinned local `models` dependencies and optional
directory `members`. Download/import preserves model preparation requirements
and member schemas; neither adds another connection artifact. Generated and
imported packages use the same normalizer. Code edits invalidate prior package
validation status.

Deterministic tests cover malformed declarations, ZIP safety, package mappings,
Python syntax, dependency includes and URLs, stale imports, compatibility failures
and downstream text identity. Artifact-boundary tests cover bundles, joins,
fan-out, inactive branches, schema violations, filename collisions and isolated
retries. Readable source fixtures in `backend/tests/fixtures/task-packages` contain
Task code for reproducible regression tests. Tests assemble ZIPs in memory;
recordings and run outputs are not required, and fixtures are excluded from the
production backend image.

The sentiment adapter gives the canonical transcript precedence over an original
`text` alias. A fixture checks distinguishable original/anonymized values,
including an empty canonical transcript. Generation prompts require consistent
aliases and input selection by consumer port rather than hard-coded graph IDs.

Runtime-cache tests verify that declaring one model does not force unrelated
Tasks offline or make their downloads target the read-only model store. Managed
execution and exported Dagster Compose provide a separate writable download
cache, while retaining explicit offline settings. Tests establish these runtime
boundaries rather than universal model availability or model accuracy.

## External ZIP authoring and repair

The external-generation prompt now includes the authoritative Draft 2020-12 manifest schema, exact readable Task folders with stable ID suffixes for Tasks only, runtime constraints, and pinned model-loading instructions. Model revisions must be real immutable commit IDs. Resolve a repository reference before packaging and use the same commit in both the manifest and code; do not invent hashes. JSON output uses `kind: "json"` or omits the optional kind, not `kind: "data"`.

Validation collects all manifest schema issues in a single report. Task mapping is independent of manifest validity, so an invalid `nodes/2/inlumen.task.json` retains its Task 2 match. Name-based suggestions are labeled for review. Each issue is displayed once with field-specific repair guidance. The review supports copying a repair prompt (attach the original ZIP separately), choosing a replacement ZIP, and revalidating against the current graph. A stale import disables confirmation until the report is refreshed. Graph conflicts during the final commit also return to this review flow.

The failing two-Task package contents are retained as readable files in `backend/tests/fixtures/task-packages/audio-sentiment-unpinned`. Regression checks rebuild the ZIP and cover its four schema issues, retained mappings, no writes on invalid imports, and the browser repair/replacement flow. Package validation still does not execute uploaded code or establish model availability.

Folder names, upload examples and external-generation prompts use the same label-plus-ID convention. Labels are sanitized for ZIP paths; IDs are percent-encoded (including hyphens) to preserve unambiguous matching. Renaming a Task does not break an existing package mapping. Review cards show task names and incoming/outgoing graph neighbors, while repeated task names include their IDs in selectors. Parallel Tasks are matched by identity regardless of folder order.
