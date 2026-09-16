# Beta 5 release plan

Reviewed 2026-09-16 after publishing Beta 5. Beta 4 is published at
[`v1.0.0-beta.4`](https://github.com/DATAPACT/inLUMEN/releases/tag/v1.0.0-beta.4);
preserve its tag and evidence. The [Beta 3 plan](beta3-release-plan.md) is retained
for history.

## Accepted milestone

[v1.0.0-beta.5 — Runtime code exchange](https://github.com/DATAPACT/inLUMEN/milestone/7).
PR [#136](https://github.com/DATAPACT/inLUMEN/pull/136) merged on 2026-09-16 at
`4b64b48de350f3e126eb261d5d040c092dbebe7b`; issue #103 is closed. The user
confirmed acceptance after the code ZIP browser round-trip passed.

### #103 — Download code ZIP

Add **Download code ZIP** beside **Upload code ZIP** in Library → Run → Runtime
code. Export code attached to Task nodes into distinct, upload-compatible folders;
retain the file bytes and names. Include the Task ID in folder names so equal Task
labels stay distinct, and let the uploader match the exact exported folders before
using its existing name matching. Do not export source data, pipeline design,
reusable pipeline definitions, or credentials. No **Package** action appears in
the top toolbar.

## Release gates

Release checks apply to the final candidate source archive. The
[release verification receipt](https://github.com/DATAPACT/inLUMEN/releases/download/v1.0.0-beta.5/release-verification.json)
records the exact source SHA, test outcomes, compatibility limits, and checksums.

- [x] #103 implementation merged; unit and browser checks pass, including file
  contents, source-data exclusion, ZIP placement, and successful uploader matching.
- [x] User acceptance of #103 on 2026-09-16.
- [x] User tested published Beta 5 and confirmed it is good enough for controlled
  evaluation on 2026-09-16. This acceptance does not establish a 1.0 production
  support commitment.
- [x] Full regression suites, typecheck, lint, build, shared-file consistency,
  database integration, Compose checks, and CI passed on final candidate
  `dd0433e01264ef20e6fcfb99cde324a62a9931d3` in [CI run 35092341044](https://github.com/DATAPACT/inLUMEN/actions/runs/35092341044).
- [x] Repeated the supported deterministic pipeline walkthrough on the
  candidate; configuration checks passed and native Dagster execution returned
  3 orders totaling 49.75.
- [x] Built and inspected the final source archive and installation instructions;
  recorded the candidate SHA, runtime versions, evidence, compatibility limits,
  and checksums in the release receipt.
- [x] Reviewed and published the prerelease tag and assets:
  [`v1.0.0-beta.5`](https://github.com/DATAPACT/inLUMEN/releases/tag/v1.0.0-beta.5)
  at `dd0433e01264ef20e6fcfb99cde324a62a9931d3`.

Implementation and current checks: [verification record](operations/beta5-verification.md).
Release copy: [release notes](beta5-release-notes.md).

## Remaining backlog

Graph previews in #63 are the next scoped follow-up, with explicit accept/cancel
and stale-revision behavior. #92 interactive tutorial and #77 UI refactoring remain
uncommitted; split #77 into observable usability problems. #72 agent efficiency
and #98 artifact-to-design remain exploratory. #59 README format and #9 compliance
agents remain outside release completion criteria. A release candidate or stable
1.0 requires its own agreed compatibility and support scope.
