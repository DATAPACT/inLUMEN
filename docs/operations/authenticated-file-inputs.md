# Authenticated uploads and codegen inputs

Branch: `codex/fix-authenticated-file-inputs`. Deployed on 2026-09-09 after explicit approval; application source commit `3387a54808086905c9d68dc305512fc6052bcd1b`.

## Changes

Upload and text-edit responses now include `file_reference` with the actual workspace bucket and filename. The frontend uses that reference for upload, replacement, and edit, discarding obsolete snapshot pointers after a mutation. It no longer invents local bucket names for missing references. The backend still supplies defaults for legacy filename-only references.

`container_id` in frontend file reads and edits is the node ID, not a bucket name. Reads resolve the saved snapshot on the backend; edits write to the node's live bucket. The read resolver retains support for explicit, workspace-owned bucket references. Text edits reject mismatched container IDs. Existing snapshot copying and workspace checks remain in force.

Authenticated codegen uses **backend-authorized input staging**, an alternative to issuing download capabilities. Before submitting generation, the backend reads only attached descriptors from its workspace graph, checks both live and snapshot bucket ownership, and transfers bounded, complete file bytes with SHA-256 checksums through the existing authenticated backend-to-codegen request. Snapshot references select the immutable object, not the current live filename. Node parameters and arbitrary metadata are not traversed as file locations.

Codegen persists these bytes as job input and verifies their checksum when staging validation. Retries use the same bytes after process restart or browser-token expiration. Full bytes are stripped from model prompts and input manifests. No user token, new download secret, URL credential, authentication bypass, or codegen-to-backend service-key exception is introduced. `/api/files/content` continues to require normal Keycloak authentication and workspace authorization.

An authenticated job without staged bytes fails closed with an instruction to start a new generation run. Pre-upgrade jobs lacking full bytes therefore require a new run. Unauthenticated development retains its optional downloader and sample behavior.

## Configuration and limits

No new credentials or database migrations are required. Compose and both example environment files expose:

| Variable | Default | Meaning |
| --- | ---: | --- |
| `INLUMEN_CODEGEN_INPUT_MAX_BYTES` | 52428800 | Maximum raw bytes per attached file; enforced on the storage read. |
| `INLUMEN_CODEGEN_INPUT_TOTAL_MAX_BYTES` | 104857600 | Maximum total base64 bytes across transported descriptors, including repeated references. |
| `CODEGEN_INPUT_FILE_MAX_BYTES` | 52428800 | Codegen's decoded-file limit; keep at least as large as the backend per-file limit. |

Staging increases request size and durable job-input storage. A request exceeding the limits fails before job submission. Existing job-history cleanup governs retention of these input copies. `CODEGEN_INPUT_FILE_API_KEY` is unnecessary for this authenticated flow; do not configure it with a browser token or the service key.

## Verification

- Backend: 327 tests run, 10 opt-in tests skipped, remainder passed.
- Separate authenticated storage integration passed against local Neo4j and MinIO with unique UUID workspaces and cleanup: upload, replacement, edit, save twice, reload, read, and immutable snapshot retrieval.
- Authentication fixture uses real RS256 signing/verification with a test JWKS and a workspace-membership fixture, with `AUTH_ENABLED=true`, `APP_ENV=production`. Missing and expired JWTs, cross-workspace selection, and explicit foreign buckets are rejected.
- Frontend: 156 tests passed; TypeScript, lint (7 existing warnings), and production build passed.
- Codegen: 125 tests passed across the full suite and the corrected API fixture check. Tests cover complete bytes beyond preview length, checksums, limits, prompt omission, durable retry/reopen, foreign-workspace job access, and local development. The real attachment exported by the authenticated backend test is also staged through the production-authenticated codegen API with the next model boundary stubbed.

This is not an end-to-end Keycloak browser-login test or a paid LLM/generated-program execution. It verifies authorization and real-file staging up to the next generation boundary. Before deployment, production access was read-only: repository status and Compose metadata. The live checks below subsequently used a separate disposable workspace; no existing production pipeline or file was changed.

### Reproduce the authenticated storage check on a local stack

With the repository's development backend, Neo4j, and MinIO containers running:

```sh
docker exec -e RUN_AUTH_FILE_INTEGRATION=1 \
  -e AUTH_FILE_STAGED_OUTPUT=/tmp/inlumen-regression-input.json \
  inlumen_backend python -m unittest discover -s tests \
  -p test_authenticated_file_workflow.py -q
docker cp inlumen_backend:/tmp/inlumen-regression-input.json /tmp/inlumen-regression-input.json
```

The test signs isolated test identities and deletes only its UUID workspace data. Never run it against production storage. To feed its exact bytes through the codegen regression suite, from the repository root:

```sh
docker run --rm \
  -v "$PWD/codegen/app:/app/app:ro" \
  -v "$PWD/codegen/tests:/repo/codegen/tests:ro" \
  -v "$PWD/contracts:/repo/contracts:ro" \
  -v /tmp/inlumen-regression-input.json:/fixture.json:ro \
  -e AUTH_FILE_STAGED_INPUT=/fixture.json -e PYTHONPATH=/app \
  inlumen-codegen-service:local sh -c \
  'uv pip install --python /app/.venv/bin/python pytest && python -m pytest /repo/codegen/tests -q -p no:cacheprovider'
```

## Live deployment verification — 2026-09-09

The branch was pushed, merged with the already-deployed `main` history (no additional source changes), and deployed using the existing `.env.production`. Backend, codegen, and frontend were rebuilt and recreated. All seven services were healthy. No generation jobs were active before the restart.

The public frontend loaded successfully with the existing real Keycloak login. Because the UI has no workspace switcher, file mutations were tested through the deployed HTTP API with that real token and an explicit new workspace header, not through the existing personal pipeline's UI.

- Upload, replace, text edit, graph reload, repeated saves, and content retrieval returned HTTP 200 with canonical `files-ws-…` references.
- Restoring a named snapshot returned the earlier replacement bytes after a subsequent edit.
- Missing and actually expired tokens returned 401. A non-member workspace and a foreign bucket returned 404.
- The isolated workspace had no LLM credentials. A request-scoped `allow_deterministic_fallback=true` smoke run exercised the deployed service's input staging, compilation, Docker whole-pipeline sample execution, and output-contract checks. Authentication and deployment model settings were unchanged; no paid model was used.
- Both the initial run `7900a50a8fe646cd9fde2a9e6db9b060` and resumed run `6df7ecb9ffc144019c56fb1ac861fd11` completed as `valid` with no validation errors.
- A scoped read of the durable job records verified exact original input bytes and SHA-256 on both attempts, even after restoring an older graph snapshot. No bearer or LLM credential was persisted in those records.
- The disposable workspace, its file buckets, graph, and generation jobs were removed after testing. The existing personal workspace was not modified.

## Deployment commands and future rebuilds

The VM runs this branch with `docker-compose-prod.yml` and `.env.production`. Its worktree was clean before deployment. Reuse the following workflow for an approved future update.

First publish the reviewed local branch:

```sh
git push -u origin codex/fix-authenticated-file-inputs
```

Then, on the VM, with a clean worktree:

```sh
ssh roby-avo@roberto-vm.vpn.sintef
cd /home/roby-avo/inLUMEN
git status --short
git fetch origin refs/heads/codex/fix-authenticated-file-inputs:refs/remotes/origin/codex/fix-authenticated-file-inputs
git switch codex/fix-authenticated-file-inputs
git merge --ff-only origin/codex/fix-authenticated-file-inputs
docker compose --env-file .env.production -f docker-compose-prod.yml config --quiet
docker compose --env-file .env.production -f docker-compose-prod.yml build backend codegen frontend
docker compose --env-file .env.production -f docker-compose-prod.yml up -d --no-deps --force-recreate --wait --wait-timeout 180 backend codegen frontend
docker compose --env-file .env.production -f docker-compose-prod.yml ps
```

The VM fetch configuration only tracks `main`, so the initial checkout required `git switch --no-track -c codex/fix-authenticated-file-inputs origin/codex/fix-authenticated-file-inputs` after the explicit fetch above. Subsequent updates can use the shown switch and fast-forward merge.

If the fix is merged before a later deployment, update `main` to the reviewed merge commit instead of switching to this branch. Retain the existing `.env.production`; defaults work without adding keys. Rebuild/recreate all three services together. Reload the browser to clear a persistence block left by an earlier failed save. Use a new isolated workspace for the post-deployment smoke check and start a new generation run for any pre-upgrade job that lacks staged input bytes.

## Follow-up: Dagster execution permissions and recovery-copy label

The live checks above covered codegen sample execution, not the Run pipeline Dagster worker. A subsequent real audio run exposed a separate failure: exported bundles contain `outputs/<node>/.gitkeep`, so extraction creates root-owned node directories. Only the mount root was made writable. The worker (UID 65532) could not create `outputs/<node>/<run-id>`.

Worker preparation now makes every directory within the job's dedicated outputs/workspaces mounts writable, rejecting symbolic links before changing permissions. Input mounts remain read-only and execution retains UID 65532, a read-only root filesystem, dropped capabilities, and no-new-privileges. No configuration changes are needed.

The toolbar's “Previous draft” was an optional download of a retained browser recovery copy, not an indication that the current graph was stale. It is now labelled “Download recovery copy”, with explanatory hover text; the recovery data is preserved.

Regression coverage includes a Linux subprocess that fails before preparation and writes successfully afterward as UID 65532, rejection of links to outside directories, and an opt-in real Docker Dagster materialization that reads an attached file and writes under pre-existing node directories. The integration test requires a directory shared at the same absolute path by the test container and Docker host:

```sh
mkdir -p /tmp/inlumen-permission-tests
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /tmp/inlumen-permission-tests:/tmp/inlumen-permission-tests \
  -v "$PWD/codegen/app:/app/app:ro" \
  -v "$PWD/codegen/tests:/repo/codegen/tests:ro" \
  -v "$PWD/contracts:/repo/contracts:ro" \
  -e PYTHONPATH=/app -e RUN_DAGSTER_PERMISSION_INTEGRATION=1 \
  -e DAGSTER_TEST_SHARED_DIR=/tmp/inlumen-permission-tests \
  inlumen-codegen-service:local sh -c \
  'uv pip install --python /app/.venv/bin/python pytest && python -m pytest /repo/codegen/tests/test_deployment_validation.py -q -p no:cacheprovider'
```

For this follow-up, rebuild and recreate `codegen frontend` with the production compose commands above. Backend/runner images do not change. Failed historical runs retain their recorded failure; start a new run to use the fix. The isolated fixture does not establish that the user's speech/transcription models or entire audio pipeline succeed.

Follow-up deployment verification (2026-09-09, source commit `a1fc0dc`):

- Codegen: 125 unit tests passed, 3 opt-in tests skipped; the targeted suite with the real Docker Dagster test enabled passed all 17 tests. Frontend: 157 tests, typecheck, and production build passed.
- Pushed the branch and rebuilt/recreated only codegen and frontend on the VM. All seven services were healthy; no pipeline or generation jobs were active before restart.
- Submitted a synthetic bundle through the deployed authenticated `/v1/validate/deployment-bundle` endpoint from the backend, using its existing service credential and a unique disposable workspace scope. No browser credentials entered the bundle or persisted result.
- Dagster executed as UID 65532, read the exact fixture input, wrote beneath pre-existing node output/workspace directories, and returned the expected output. Re-submission returned the identical durable receipt. Missing authentication returned 401, a missing workspace header returned 400, and cross-workspace receipt retrieval returned 404.
- Test execution `permission-smoke-3617f71efd7b4c0492d2ff42826244eb` completed. Removed only its completed receipt; temporary bundle directories were automatically cleaned. No existing user pipeline, uploaded file, or failed run was modified.
- A separate signed-in Chrome tab showed the new recovery-copy label after reload. An already-cached page may continue showing the old label until refreshed.
- This verifies the deployed Dagster execution service with a synthetic fixture; it does not claim a new successful run of the user's full audio/model pipeline or a complete browser-to-runner workflow.

## Follow-up: compressed revision headers (2026-09-10)

A fresh production pipeline exposed another save failure: HTTP 409 on upload. The public proxy rewrites numeric ETags such as `"109"` to `W/"109"` when compressing responses. The browser previously ignored weak ETags, so it could keep an earlier revision after a successful save and reject its own later upload. An isolated authenticated production request confirmed weak ETags on graph reads and graph writes. This was not a file-bucket or authentication failure.

Graph responses now include `X-InLumen-Graph-Revision`, exposed through CORS and preserved by the gateway when rebuilding responses. The browser prefers this numeric application revision and sends the existing strong numeric `If-Match` precondition. For older servers it also recognizes the numeric ETag after compression; arbitrary validators are rejected. Genuine stale writes still fail rather than overwriting someone else's changes. No configuration change or authentication relaxation is required.

Regression tests cover authenticated create/position/snapshot/upload/edit/save sequences with alternating weak and strong ETags, the dedicated header with an unrelated representation ETag, invalid validators, and gateway revision preservation. The real authenticated file workflow now sends revision preconditions on every write and checks the dedicated response header. Earlier file workflow coverage omitted those preconditions and missed the production proxy transformation.

Rebuild and recreate `backend frontend` for this change using the production compose commands above. Codegen and runner do not change. Refresh an old browser tab only after preserving any unsaved draft.
