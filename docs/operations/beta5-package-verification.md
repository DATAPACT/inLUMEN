# Beta 5 package implementation checks

Implementation started from published Beta 4 (`511ac57`) on 2026-09-14.
These results cover the project-package implementation before its PR review, not
release acceptance or publication.

- Backend regression suite: 341 tests passed, with 15 environment-dependent skips.
  A final legacy-file-role case was then added; the package unit file passed all
  seven tests. These validate byte preservation, ordinary parameters, blank secret
  values, missing/altered/unexpected files, limits, storage-pointer exclusion and
  reusable-definition requirements.
- Disposable Neo4j and MinIO integration: all 13 tests passed, including five package
  tests. Package coverage verifies preview without revision changes, import and
  reload, re-export/re-import, actual attachment reads, rebuilt parameter
  suggestions, stale writes rejected before uploads, rollback after storage or
  graph failure, reusable name collisions, and gateway transfer into a separate
  workspace with new storage references. Real Keycloak was not involved.
- Frontend: 165 unit tests, typecheck, lint and production build passed. Lint retains
  seven existing Fast Refresh warnings; the build retains its chunk-size warning.
- Full browser suite: 21 of 22 passed on the first run. The existing Overview
  refresh test timed out waiting for a read after an arrow-key edit; it passed
  when rerun alone. The three package tests then passed together: export/preview/
  confirmed import/reload, invalid preview preserving save state, and refusing to
  import after another editor changes the design. Browser tests use API fixtures;
  database/storage behavior is covered separately by integration tests.
- Shared generated-file consistency and whitespace checks passed.

The release still needs user acceptance, green CI on the final candidate and the
execution/download walkthrough using an imported package from that source archive.
See the [release plan](../release-plan.md) and [package contract](../project-packages.md).
