# Beta 3 compatibility and data preservation

## Release policy

Beta 3 is a **fresh-install-only evaluation release**. An in-place upgrade from Beta 2 is not certified.
Use a separate checkout, Compose project, network, ports, storage, and secrets.
Do not attach Beta 2 databases to Beta 3 or switch authentication modes to
make old data appear. Keep the old installation available until the reconstructed
pipelines have been verified. Checking out an older tag is not a database rollback.

No concurrent-user capacity claim is made. Two identities completing small
fixtures establish a functional check, not a load benchmark.

## Changes since `v1.0.0-beta.2`

- Authenticated data belongs to a server-resolved workspace associated with an
  immutable Keycloak issuer and subject. Local no-auth mode is a shared identity.
  Existing unscoped browser drafts are not automatically assigned to a user.
  Recreating a username under a different subject does not preserve ownership.
- PostgreSQL now holds workspace membership and durable job/application state.
  Retaining only graph and object storage is insufficient to retain an installation.
- Preserve the existing node-secret encryption key together with its database.
  In development the key may be `backend/state/node-secrets.key`; deployments
  may instead provide `INLUMEN_SECRET_ENCRYPTION_KEY` or a configured key path.
  A fresh installation has its own key; re-enter credentials there rather than
  moving ciphertext without the key. Shared LLM credentials also depend on it.
- Production requires a separately managed Cloudflare connector and its ingress
  network. Follow the current [multi-user deployment guide](../production-multi-user.md),
  including removal of obsolete standalone-tunnel environment settings.
- Authenticated file references and input staging changed. Submit a **new** job
  after reattaching inputs; old queued/failed jobs may contain obsolete staging
  URLs or references and are not a supported migration mechanism.
- The development frontend now uses Node 22, matching the supported production
  build runtime. Install dependencies from the committed lockfile with `npm ci`.
- Reusable pipelines support one level only. Nested legacy definitions are not
  executable; flatten or rebuild them before evaluation.

## Preservation and reconstruction

1. While the old installation still works, save each important pipeline and
   export its design with **JSON**. Record which account/workspace owns it.
2. Download or retain every attached input and runtime file separately, including
   Python code, dependency declarations, and supporting assets. Record filenames,
   destination nodes, and checksums. Design JSON contains references, not the
   object bytes; those bucket references are not portable to a new workspace.
3. For successful runs, download results and **Download tested snapshot**.
   Keep the snapshot checksum and run ID. Its Dagster runtime can be evaluated
   independently using its root `docker-compose.yml`; the snapshot is not a
   complete editor backup or an importable replacement for the design JSON.
4. Retain a consistent offline copy of the old installation. Inventory actual
   mounts, not just Compose volume declarations. Preserve PostgreSQL (if used),
   Neo4j, MinIO, runner state/artifacts, codegen state, generated worker/model
   volumes, and any bind-mounted data/state. Preserve configuration, image/source
   versions, encryption keys, and the external Keycloak identity store separately.
   The [volume backup script](../../scripts/backup_volumes.py) covers only named
   volumes attached to project containers. Development Neo4j/MinIO bind mounts,
   detached dynamic worker/model volumes, external Keycloak/databases, and browser
   storage are outside its automatic inventory. Do not treat its success as a
   complete installation backup.
5. Start Beta 3 with fresh storage following the [quickstart](../prototype-quickstart.md).
   Sign in as the intended owner when using Keycloak. Import the design, replace
   old file references by uploading each retained file to its corresponding node,
   and re-enter credentials through the application. Recreate reusable definitions
   and parent references deliberately; package round trips are not supported.
6. Save, reload, run, and compare outputs with the retained reference results.
   A new installation creates new version/run IDs and new history. Chats, prior
   job history, account membership, LLM keys, and browser drafts are not restored
   by JSON or runtime ZIP import. Keep the old installation and its backup until
   these limitations are acceptable for your evaluation.

The September 14 [authentication and recovery record](beta3-auth-recovery.md)
checks same-revision restoration of representative data. It does not establish
Beta 2 migration, whole-host recovery, or a production deployment certification.
