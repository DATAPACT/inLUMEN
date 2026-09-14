# inLUMEN 1.0 Beta 4 — Guided configuration & generation metrics

Draft release notes. Beta 4 is not published; no tag or accepted release commit
has been selected. Track acceptance in the [release plan](release-plan.md).

## Changes

- Click a detected environment variable in a node's Inspector to add it to Runtime
  parameters. Its name is filled in and its empty value input receives focus.
  Existing parameters show Added; manual entry remains available. Sensitive
  variables use the existing secure-value controls.
- Background generation jobs retain their duration after completion and reload.
  Recent history shows duration; job details place duration and queue time beside
  available cost/token metrics. Worker time includes validation and repairs.
- Each resumed attempt and cache hit has its own measurement. Missing historical
  measurements remain unavailable. An interrupted worker's actual completion time
  is unknown, so recovery does not invent a duration.

## Compatibility and evaluation

The change adds optional fields to existing generation-job JSON records. Artifact
contract versions are unchanged. Historical records remain readable without a
fabricated duration. Synchronous node-generation requests do not gain background
history through this change. A Beta 3 in-place upgrade is not certified by these
checks; retain the existing fresh-install-only evaluation policy until separately
verified. Follow the [quickstart](prototype-quickstart.md) and
[compatibility and preservation guidance](operations/beta3-compatibility.md).

See [implementation verification](operations/beta4-verification.md) for test
results and limits. Package round-trip export/import (#103), graph previews (#63),
interactive tutorials (#92), broad UI refactoring (#77), and integrations remain
follow-up work. Final publication needs a candidate SHA, green CI, installation
and source-package checks, and reviewed release contents.
