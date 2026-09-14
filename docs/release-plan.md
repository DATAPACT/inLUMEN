# Beta 4 release plan

Reviewed 2026-09-14 against GitHub and `main` at `a65dc709f4095b76466f3c2ce0c7cf5974eb44bc`.
[Beta 3](https://github.com/DATAPACT/inLUMEN/releases/tag/v1.0.0-beta.3) is published;
its milestone and acceptance issue #129 are closed. Preserve its tag and evidence.
The [historical plan](beta3-release-plan.md) is retained for context only.

## Accepted milestone

[v1.0.0-beta.4 — Guided configuration & generation metrics](https://github.com/DATAPACT/inLUMEN/milestone/6).
No release date is committed. Issues remain open until their implementation is
merged and acceptance checks pass.

### #99 — Click-to-add runtime parameters

Click a detected environment variable in the selected node's Inspector to add its
name to Runtime parameters with an empty, focused value input. Keep required/optional
and sensitivity information. Mark existing parameters Added and prevent duplicates
or overwrites. Removing a parameter makes its suggestion available again. Preserve
manual Add and secure secret storage. Never infer or generate parameter values.
Existing code discovery remains the source of suggestions after generation, upload
and edits. Verify normal and sensitive values, keyboard access, and save/reopen.

### #100 — Durable generation duration

Persist server timestamps and duration with each background generation job. Queue
time is request creation to worker start; generation duration is worker start to
terminal result, including model calls, validation and repairs. Each resumed job
is a separate attempt and has its own measurement. Cache hits measure the current
job, not the original generation. Completed values remain fixed across progress
updates, credential cleanup, history reads and service restarts.

Show duration and queue time next to available cost/token metrics, and duration in
recent history. Historical missing measurements are unavailable, never fabricated
from mutable updated_at. A queued cancellation has queue time but no worker duration.
After an interrupted worker is recovered, its finished_at records reconciliation;
its actual completion time and duration are unknown. Preserve that distinction in
UI. New fields are optional on reads and stored in existing JSON job payloads.

## Release gates

- [ ] #99 browser acceptance, including save/reopen and secret handling.
- [ ] #100 lifecycle and durable-store checks, gateway pass-through, browser history
  after reload, and completed record retrieval after service restart.
- [ ] Regression suites, typecheck, lint, build, shared-file consistency, database
  integration and Compose checks pass; CI is green on the candidate commit.
- [ ] Repeat the supported deterministic pipeline walkthrough on the candidate;
  verify configuration, execution and downloads remain usable.
- [ ] Record final candidate SHA, evidence, compatibility limits and release notes.
- [ ] Prepare and review Beta 4 prerelease contents before publication.

Implementation and checks: [verification record](operations/beta4-verification.md).
Release copy: [draft notes](beta4-release-notes.md).

## Remaining backlog

GitHub had 10 open issues and no open PRs at review. #99 and #100 are assigned to
Beta 4. #103 package portability is recommended as the next substantial milestone:
specify and verify design-plus-code export/import into a fresh workspace, attachments,
reusable references and credential exclusion. #63 graph previews follows with explicit
accept/cancel and stale-revision behavior. #92 interactive tutorial and #77 UI
refactoring remain uncommitted; split #77 into observable usability problems.
#72 agent efficiency and #98 artifact-to-design remain exploratory. #59 README
format and #9 compliance agents remain outside release completion criteria.

Closed items #124, #122, #116, #114, #104, #102 and #101 are not carried forward.
A release candidate or stable 1.0 requires its own agreed compatibility/support scope.
