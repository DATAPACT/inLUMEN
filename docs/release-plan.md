# Release plan and roadmap

Reviewed 2026-11-26. This page says what the next release contains, what is
planned after it, and what is not planned. Earlier plans are kept for history:
[Beta 5](beta5-release-plan.md), [Beta 3](beta3-release-plan.md).

> **Status: draft.** The Beta 6 scope, gates and title below are a proposal.
> Nothing here is agreed until the open decisions at the end of the Beta 6
> section are answered.

## Where we are

| | |
|---|---|
| Last release | [`v1.0.0-beta.5`](https://github.com/DATAPACT/inLUMEN/releases/tag/v1.0.0-beta.5), 2026-09-16 |
| `main` since then | about 50 merged pull requests, no tag |
| Deployed instance | follows `main`: every merge is deployed after its tests pass |
| Agents library | `inlumen-agents` 1.0.0b6.dev6 on PyPI (development builds of Beta 6) |

The deployed instance is ahead of the last release. Beta 6 records, checks and
tags what is already on `main`; it does not wait for new features.

## Beta 6 (proposed)

Working title: **v1.0.0-beta.6 — Typed data flow, run outputs and plugins**
([milestone](https://github.com/DATAPACT/inLUMEN/milestone/8)). Date: not set.

### Scope: merged since Beta 5

| Area | What changed | Pull requests |
|---|---|---|
| Pipeline model | Typed ports, parallel branches, Parallel Map closed by Collect; multi-file Sources; one artifact per output port (artifact contract v4) | #153, #169, #170 |
| Running pipelines | Run outputs stored with lineage and viewable in the app (compact, table and raw views); unchanged step results are reused and failed runs resume; run activity fixes | #152, #154, #155, #159, #189, #191 |
| Export | Self-contained Argo Workflows bundle; portable Task packages | #144, #162, #164, #167 |
| Plugins | Third-party tools with a REST API as Sources, Tasks or Destinations | #148, #156, #157, #160 |
| Agents | Design and code generation published as the `inlumen-agents` library; a pipeline is designed in one tool call; graph change previews; step-level repairs in code generation | #141, #171, #174, #183, #184, #185 |
| Interface | Inspector without plugin picker, compact palette groups, Run tab fixes | #168 |
| Operations | Published container images, deploy to the VM on merge, weekly backup | #176, #177, #180 |
| Maintenance | Code clean-up and folder structure (#182), faster CI, dependency audit | #165, #186, #187, #196 to #201, #203, #204 |

Closed issues covered: #63, #72, #147, #150, #151, #158, #161, #182, #188, #190, #193, #202.

### Gate issues

Both issues proposed as gates are closed.

| Issue | What was wrong | Fixed by |
|---|---|---|
| #193 Design agent skips some delete requests | Deleting a step needed an id that the canvas snapshot does not show, so some requests were answered without the edit | #207 |
| #202 Flaky save-recovery browser test | The test was right: "Reload saved graph" could fail when an autosave was queued during the reload | #208 |

### Release gates

Same gates as Beta 5, applied to the final candidate commit.

- [x] Gate issues closed (#193, #202).
- [ ] Release notes written (`docs/beta6-release-notes.md`).
- [ ] Verification record written (`docs/operations/beta6-verification.md`).
- [ ] Compatibility statement: what a Beta 5 installation must do. Contract v4 needs regenerated exports (see [v3 to v4](migrations/artifact-contract-v3-to-v4.md)); whether saved Beta 5 pipelines open unchanged must be checked, not assumed.
- [ ] `inlumen-agents` 1.0.0b6 released from the candidate commit (the application images install the library from the same commit).
- [ ] Full regression suites, type check, lint, build, shared-file check, database integration and Compose checks pass in CI on the candidate.
- [ ] The [deterministic walkthrough](prototype-quickstart.md) repeated on the candidate.
- [ ] One generated pipeline per new area designed, run and exported on the deployed instance: typed ports with a Parallel Map, a plugin step, an Argo bundle.
- [ ] Source archive, installation instructions and release receipt built and inspected.
- [ ] User acceptance.
- [ ] Prerelease tag and assets published.

### Open decisions

1. **Title and date** of Beta 6.
2. **Gate list:** #193 and #202 are closed. Should any other issue gate Beta 6?
3. **Upgrade promise:** does Beta 6 certify an in-place upgrade from Beta 5, or stay "controlled evaluation, no upgrade guarantee" like Beta 5?
4. **Library version:** should `inlumen-agents` 1.0.0b6 be released together with the application tag?

## Next: after Beta 6

Planned, not yet assigned to a release number.

| Issue | Summary |
|---|---|
| #194 | Reject edits that cannot pass validation before the agent loop |
| #195 | Reduce the cost of small edits: fewer calls and a smaller prompt |
| #92 | Interactive tutorial (needs a specification first) |

## Later: exploratory

Direction only; no commitment.

| Issue | Summary |
|---|---|
| #98 | Build a pipeline design from existing code or a deployment artifact |
| #205 | Reduce duplication and split the largest files |

## Outside release criteria

#59 (README follows the template) and #9 (compliance agents, LexAlign
integration) are reminders. They do not block any release.

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

- Each release has one GitHub milestone named like its tag. Issues in "Next"
  and "Later" sit in the milestones "Stabilization & workflow" and
  "Exploration — Automation & integrations".
- When a release is published, its section moves to a `betaN-release-plan.md`
  page and this page is reviewed.
- An issue without a milestone is not planned.
