# Release plan and roadmap

Reviewed 2026-11-28. This page is the **proposed** plan for the release after
Beta 6. Nothing in it is agreed yet: the open decisions are listed in
[Decisions to take](#decisions-to-take). Earlier plans are kept for history:
[Beta 6](beta6-release-plan.md), [Beta 5](beta5-release-plan.md),
[Beta 3](beta3-release-plan.md).

## Where we are

| | |
|---|---|
| Last release | [`v1.0.0-beta.6`](https://github.com/DATAPACT/inLUMEN/releases/tag/v1.0.0-beta.6), 2026-11-27, at `f61689b` |
| `main` today | `8f40894` (merge of #229), 34 commits after the tag. Test suite, container images and deployment succeeded for this commit |
| Deployed instance | follows `main`: every merge is deployed after its tests pass |
| Agents library | `inlumen-agents` 1.0.0b6 is built from the release commit but not on PyPI; the latest version there is 1.0.0b6.dev6 (checked 2026-11-28) |
| Next release | proposed: `v1.0.0-beta.7`. No milestone, tag or release exists for it yet |

## Proposed milestone

Name: **v1.0.0-beta.7 — Design from existing code and artifacts, faster edits**.
No fixed date, as for Beta 6: tag when the release gates below pass.
The agents library follows the tag: `inlumen-agents` 1.0.0b7.

## Scope: merged since Beta 6

Each row was checked against the merged pull request.

| Area | What changed | Pull requests |
|---|---|---|
| Design from a deployment artifact | **Import** accepts an inLUMEN bundle ZIP (or its manifest) and an Argo workflow of any origin. No model is used and nothing in the file is run | #220 |
| Design from code | A ZIP of code is read by the design agent after the user agrees to the list of files that are sent. Since #226 the design is one model call | #221, #226 |
| Argo conditions, loops and nested templates | `when` becomes a Condition step, `withItems`, `withParam` and `withSequence` become Parallel Map → task → Collect, a nested template is expanded one level deep. A workflow that inLUMEN exported is read back from its annotations; the export now also records the template of a step | #228 |
| Code and data attached on import | Bundle: Task code, Source data and step parameters. Code ZIP: the input file of each Source; Task code is not attached | #225, #229 |
| Undo after an import | The import still replaces the canvas at once. The message after it has an **Undo** button, and the consent dialog says how many steps are replaced | #226 |
| Design agent: edits | An edit that is not valid alone (a lone disconnect, a Flow behaviour switch, removing the only destination) is undone and explained without a repair round | #217 |
| Design agent: cost of plain edits | A plain delete, clear or connect is one small model call (about 710 prompt tokens). `INLUMEN_DESIGN_FAST_EDITS=0` switches it off | #218 |
| Plugins | A pipeline run joins the Docker network named by `INLUMEN_RUN_TOOL_NETWORK` (default `inlumen_tools`) when it exists, so a tool on a private address is reachable from a run. The application's services are not on that network | #215 |
| Maintenance | The filesystem hand-off that is shipped in bundles exists once, and the tests run the shipped code. The shipped text is byte-identical | #219 |
| Release plan | Documentation only | #212, #214, #216, #227 |

Closed issues covered: #98, #213, #222, #224. #223 (a preview before the import
replaces the canvas) was tried in #226, dropped, and closed as not planned.

No change to the database schema or to the artifact contract was found in the
changed files. This was read from the list of changed files, not tested.

## Proposed scope: open issues

Each of the four issues has a session working on a part of it on 2026-11-28.
No pull request of these sessions was open when this page was written, so the
table counts only what is on `main`.

| Issue | Done on `main` | Left | Recommendation |
|---|---|---|---|
| #194 Reject edits that cannot pass validation before the agent loop | #217: the target of two model calls is met for 30 of 33 measured requests | A Flow behaviour switch made through several edits still gets a repair round (4 and 15 calls in two of three runs); no measurement on the self-hosted model or with default reasoning; `disconnect_steps` on edges saved without port handles; whether a user may leave the canvas invalid on purpose | **Partly in.** The release ships #217 and what is merged before the candidate. Not a gate: the rest needs measurements on the self-hosted model |
| #195 Reduce the cost of small edits | #218: plain edits in one small call, correct in 45 of 45 measured answers | The full agent's prompt is unchanged (about 7,400 tokens per call); the share of plain edits in real usage is not measured; ServiceCraftBench was not run; no measurement on the self-hosted model; the 26 requests of #181 are not in the repository | **Partly in.** The saving for plain edits is on `main`. Not a gate: the usage measurement needs the stored chat messages, which only Roberto can release |
| #205 Reduce duplication and split the largest files | #219: first item, except `parameter_environment` | `shared/` as a package, Compose base file, shared Flask setup, splitting the largest files, test-only functions | **Later**, #219 is in. The issue itself says none of its items is scheduled, and three of them change how images are built, which is a risk just before a release |
| #92 Interactive tutorial | Nothing | The whole feature. The issue has no specification and no comments | **Later** as a whole. A first part goes in only if it is merged and tried in a browser before the candidate |

#59 (README follows the template) and #9 (compliance agents, LexAlign
integration) are reminders. They are not planned and do not block any release.

## Gate issues

Proposed: **none**. No open issue describes a defect in what Beta 7 would
ship. The work before the tag is the verification below, because several of
the new features were merged without a full check (see
[Risks](#risks-and-unverified-points)).

## Release gates

Same gates as Beta 6, with the checks for the new features added. "Who" says
what a check needs: **CI** runs by itself; **Local** needs a local stack and no
model; **Model** needs a real model and so an API key; **VM** needs the
deployed instance; **Roberto** needs his account, his data or his decision.

### Same as Beta 6

| | Gate | Who |
|---|---|---|
| [ ] | Release notes written (`docs/beta7-release-notes.md`) | Session, reviewed by Roberto |
| [ ] | Verification record written (`docs/operations/beta7-verification.md`) | Session, reviewed by Roberto |
| [ ] | Compatibility statement: what a Beta 6 installation must do. Expected to be short; see the note on schema and contract above | Local |
| [ ] | Version of `inlumen-agents` raised to 1.0.0b7 in `packages/agents/pyproject.toml` | Session, in a pull request |
| [ ] | Full regression suites, type check, lint, build, shared-file check, database integration and Compose checks pass on the candidate | CI |
| [ ] | Deterministic quickstart (`scripts/verify_quickstart.py --materialize`): 3 orders, total 49.75 | Local |
| [ ] | Argo order-summary bundle runs on a local k3d cluster: 3 of 3 steps, same result checksum | Local |
| [ ] | One pipeline designed with the chat agent, generated, run and exported | Model |
| [ ] | The same check on the deployed instance. Left open in Beta 6 | VM, Model, Roberto |
| [ ] | Source archive, installation instructions and release receipt built and inspected | Local |
| [ ] | `inlumen-agents` 1.0.0b7 built from the candidate and published to PyPI | Roberto |
| [ ] | User acceptance | Roberto |
| [ ] | Prerelease tag and assets published; milestone closed | Roberto |

### New in Beta 7

| | Gate | Who |
|---|---|---|
| [ ] | Bundle round trip in the browser: export a pipeline, import the bundle ZIP on an empty canvas, run it without attaching anything by hand. Once with the order-summary bundle and once with a Parallel Map bundle (the second was never run after import) | Local |
| [ ] | Argo workflow exported by inLUMEN, imported **on the canvas in the browser**: same steps and connections as the original. #228 was checked through the endpoint only | Local |
| [ ] | Argo workflow of other origin with `when`, a loop and a nested template, imported on the canvas: Condition, Parallel Map and Collect steps appear, and the message names what was kept as text. Use at least one workflow from a real project that was not written for the tests | Local |
| [ ] | ZIP of code imported in the browser: the consent dialog lists the files, the design passes validation, and each Source has its input file. The attachment of #229 was not tried in the browser | Model |
| [ ] | Undo after an import on a canvas that held steps: the earlier canvas and the saved graph come back | Local |
| [ ] | A plugin pipeline run through the application reaches a tool that is only on the private network (`mock-tool` with the `plugins-demo` profile). #215 checked the network argument and the reachability separately | Model, or Local with a hand-made design |
| [ ] | From the same network, the application's services (`postgres`, `neo4j`, `minio`, `backend`, `codegen`, `runner`) do not resolve. Repeat on Linux; #215 was checked on Docker Desktop (arm64) only | Local, VM |
| [ ] | `evals/design-edits/` repeated on the candidate: edits that are not valid alone (#217) and plain edits (#218), with the same model as before | Model |
| [ ] | A plain delete, clear and connect in the application with "Preview graph changes" on and off | Model |

## Decisions to take

| Question | Proposal |
|---|---|
| Version and title | `v1.0.0-beta.7` — "Design from existing code and artifacts, faster edits" |
| Date | No fixed date; tag when the release gates pass |
| Gate issues | None. #194 and #195 ship in part and stay open; #205 and #92 stay in "Later" |
| Milestone | Create the milestone with the name above. Put the closed issues #98, #213, #222 and #224 in it. Leave #194, #195, #92 and #205 in their current milestones |
| Upgrade from Beta 6 | Decide whether Beta 7 certifies an in-place upgrade or, like Beta 6, only reports what was checked |
| Private tool network on the VM | Decide whether the deployed instance gets an `inlumen_tools` network. Without it, runs there behave as in Beta 6 |
| Self-hosted model | Decide whether a measurement on the self-hosted model is needed before the tag. It is the open point of both #194 and #195 |
| Real usage of plain edits | Decide whether the stored chat messages may be read to measure the share of plain edits (#195, step 2). They may hold personal data |
| Tutorial (#92) | Decide whether a first part may ship without a specification for the whole feature |

## Steps that only Roberto can take

Nothing below was done or prepared on GitHub by writing this plan.

1. **Publish `inlumen-agents` 1.0.0b6 to PyPI**, still open from Beta 6:
   `uv publish dist/release/v1.0.0-beta.6-library/*`. Publish the files that
   were built from the release commit `f61689b`. Do not build again from
   `main`: `main` still carries the version 1.0.0b6, but seven source files of
   the library changed after the tag (under `packages/agents/src`).
2. Take the decisions in the table above.
3. Create the milestone and move the issues, if the proposal is accepted.
4. Review and merge the pull requests of the four issue sessions. Each merge
   to `main` deploys to the VM.
5. Run, or allow, the checks marked **Roberto** and **VM**: the generated
   pipeline on the deployed instance, and user acceptance.
6. Publish `inlumen-agents` 1.0.0b7, create the tag and the prerelease, and
   close the milestone.

## Risks and unverified points

Taken from the "Limits" and "Not checked" sections of the merged pull requests.

| Point | Source | Effect on the release |
|---|---|---|
| The library version on `main` was not raised although the library changed | this review | A build from `main` named 1.0.0b6 would differ from the tagged one. Covered by step 1 above and by the version gate |
| Argo conditions, loops and nested templates were **not tried on the canvas in the browser** | #228 | Gate added |
| Input files of a code ZIP: the attachment was **not tried in the browser**; the new canvas code has unit tests only | #229 | Gate added |
| No full plugin pipeline run through the application with the private network; not checked on Linux | #215 | Gates added |
| The deployed instance has no `inlumen_tools` network, so its runs did not change. The production Compose file passes the setting but does not create the network (read in the file; the VM itself was not inspected) | #215 | Decision above |
| **Check endpoint** still tests from the backend, not from a run. It can pass for a tool that a run cannot reach | #215 | Documented in `docs/plugins.md`; say so in the release notes |
| A wrong route applies a wrong edit: if the router takes a larger request for a plain delete, the delete is applied. Not seen in 80 measured answers; the suite is small and was written with the change | #218 | Keep "Preview graph changes" as the safeguard; say so in the release notes |
| The fixed replies of the quick path and of a declined edit are in English, whatever the language of the request | #217, #218 | Known limit for the release notes |
| The wording of the tool note was chosen on the same requests that are reported, so those numbers are not an independent test. Compound edits: 8 of 9 correct after, 9 of 9 before | #217 | Repeat the check on the candidate (gate added); a larger sample is part of #194 |
| All model measurements used one model (`openai/gpt-oss-120b`, low reasoning). Nothing was measured on the self-hosted model or with default reasoning | #217, #218, #221, #226, #229 | Decision above |
| A design from code is the model's reading of the code. The same script gave one Task in two runs and three in a third. Tried with two small ZIPs; no large repository; notebooks are not read | #221, #226 | Known limit; the result message already asks the user to check each step |
| Text hidden in a code ZIP could influence the design. The agent can only edit an in-memory graph, so the effect is a wrong design | #221 | Known limit for the release notes |
| The first line of a table is sent to the model. In a table without a header row this is a data row | #221 | The consent dialog says so; repeat in the release notes |
| Code ZIP: a wrong file name from the model that still matches a file attaches that file; one file per Source; nothing over 50 MiB; Task code is never attached | #229 | Known limits |
| Bundle import: no rollback if attaching fails part-way; input files in subfolders are not attached; the whole ZIP is read in the browser (up to 2,000 files, 200 MB) | #225 | Known limits |
| Argo import: Source and Destination are guessed from position; a workflow exported without generated code gives default port names; a looped task that also reads from another task gives a design that does not pass validation | #220, #228 | Known limits; the import message names them |
| Argo files of other origin were written for the tests. No workflow from another real project was tried | #220 | Gate added |
| #219 was not checked by starting the full stack from rebuilt images. The shipped text is byte-identical and the quickstart ran | #219 | Covered by the quickstart gate and by the deployments since |
| ServiceCraftBench was not run after #218. New designs start from an empty canvas and should not reach the router; this was not measured | #218 | Part of #195, not a gate |

Not verified while writing this plan: the state of the VM, the content of the
work in progress of the four issue sessions, and any behaviour in a browser.
Only GitHub, PyPI's public version list and the repository were read.

## Later: exploratory

Direction only; no commitment.

| Issue | Summary |
|---|---|
| #205 | Reduce duplication and split the largest files (first item done in #219) |
| #92 | Interactive tutorial (needs a specification first) |

## Towards 1.0

A release candidate or stable 1.0 needs its own agreed compatibility and support
scope; none is defined yet. The README lists four expected KPIs. No measurement
of them is recorded against a release so far:

| KPI | Target in the README |
|---|---|
| Accuracy | Cosine similarity of at least 0.8 between generated and reference pipelines |
| Usability | Mean SUS score of at least 80 |
| Deployment success rate | More than 90% of generated pipeline designs executable |
| Compliance by design | Expert-confirmed successful refinement for more than 90% of mutations (with LexAlign) |

Deciding which of these must be measured before 1.0, and on what data, is the
first step towards a 1.0 plan.

## How this page is kept

- Each release has one GitHub milestone named like its tag. Issues that are
  planned but not tied to a release sit in the milestones "Stabilization &
  workflow" and "Exploration — Automation & integrations".
- When a release is published, its section moves to a `betaN-release-plan.md`
  page and this page is reviewed.
- An issue without a milestone is not planned.
