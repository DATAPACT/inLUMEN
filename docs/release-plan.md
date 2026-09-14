# Beta 5 — Project portability

Reviewed against GitHub on 2026-09-14. Beta 4 is published at
`511ac57f700ab8fc4cb91925bb8458134892ec1b`; #99 and #100 are closed after user
acceptance. Its [release plan](beta4-release-plan.md) and evidence remain historical.
#103 is open and is the implementation scope of
[v1.0.0-beta.5 — Project portability](https://github.com/DATAPACT/inLUMEN/milestone/7).

## #103 — Import/export complete project packages

From the canvas, export the saved design with its code, data attachments and
referenced reusable pipelines. Import the archive into another workspace after
previewing its contents and confirming canvas replacement. Preserve node labels,
positions, ports, connections, runtime parameter names and non-secret values,
implementation settings, and file bytes/roles. Import reusable definitions under
fresh identities and resolve name collisions without changing existing definitions.

Secret runtime parameter values are omitted. Imported code is treated as supplied
code, and environment-variable suggestions are rebuilt statically. Run history and
generated validation/cost reports are not transferred. Existing JSON import/export,
code ZIP upload and deployment export remain available for their original uses.

Contract and user workflow: [project packages](project-packages.md).

## Acceptance and release gates

- [ ] Review the implementation PR and confirm the package workflow in the tool.
- [ ] Verify export/import into a fresh workspace, including code, binary/data
  attachments, reusable definitions, parameter suggestions and blank secret values.
- [ ] Verify malformed archives, missing/tampered files, stale revisions and storage
  failures preserve the current canvas and reusable catalog.
- [ ] Regression suites, browser checks, database/storage integration, typecheck,
  lint, build and CI pass on the final candidate.
- [ ] Repeat the deterministic execution/download walkthrough using an imported
  package from the final source archive.
- [ ] Record candidate SHA, release notes and evidence before publishing Beta 5.

## Later milestones

#63 graph-change preview is next after portability, with explicit accept/cancel and
stale-revision behavior. #92 tutorial and concrete usability items from #77 can
follow. #72 agent efficiency and #98 artifact-to-design remain exploratory.
#59 README format and #9 compliance agents remain outside these release gates.
The eight remaining open issues are not all committed to Beta 5.
