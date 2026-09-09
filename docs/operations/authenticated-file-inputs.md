# Authenticated uploads and codegen inputs

Branch: `codex/fix-authenticated-file-inputs`. No deployment has been performed.

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

This is not an end-to-end Keycloak browser-login test or a paid LLM/generated-program execution. It verifies authorization and real-file staging up to the next generation boundary. Production access was read-only: repository status and Compose metadata. No existing production pipeline or file was changed.

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

## Rebuild and deploy later, only after approval

The VM currently runs `main` with `docker-compose-prod.yml` and `.env.production`. Its worktree was clean at diagnosis. The following commands have **not** been executed.

First publish the reviewed local branch:

```sh
git push -u origin codex/fix-authenticated-file-inputs
```

Then, on the VM, with a clean worktree:

```sh
ssh roby-avo@roberto-vm.vpn.sintef
cd /home/roby-avo/inLUMEN
git status --short
git fetch origin
git switch codex/fix-authenticated-file-inputs
git merge --ff-only origin/codex/fix-authenticated-file-inputs
docker compose --env-file .env.production -f docker-compose-prod.yml config --quiet
docker compose --env-file .env.production -f docker-compose-prod.yml build backend codegen frontend
docker compose --env-file .env.production -f docker-compose-prod.yml up -d --no-deps --force-recreate backend codegen frontend
docker compose --env-file .env.production -f docker-compose-prod.yml ps
```

If the fix is merged before deployment, update `main` to the reviewed merge commit instead of switching to this branch. Retain the existing `.env.production`; defaults work without adding keys. Rebuild/recreate all three services together. Reload the browser to clear a persistence block left by an earlier failed save. Use a new isolated workspace for the post-deployment smoke check and start a new generation run for any pre-upgrade job that lacks staged input bytes.
