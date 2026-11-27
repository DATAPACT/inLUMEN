> Historical Beta 6 planning record. Published on 2026-11-27 at `f61689b`.
> See the [current plan](release-plan.md).

# Beta 6 release plan

Reviewed 2026-11-27 after publishing Beta 6 at
[`v1.0.0-beta.6`](https://github.com/DATAPACT/inLUMEN/releases/tag/v1.0.0-beta.6).
The earlier plans are kept for history:
[Beta 5](beta5-release-plan.md), [Beta 3](beta3-release-plan.md).

## Milestone

[v1.0.0-beta.6 — Typed data flow, run outputs and plugins](https://github.com/DATAPACT/inLUMEN/milestone/8).
No date was fixed: the release was tagged when the release gates below passed.

## Scope: merged since Beta 5

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
| Found by the release checks | Parallel Map: stale manifest size and checksum in the bundle; Argo export used the old workflow | #211 |

Closed issues covered: #63, #72, #147, #150, #151, #158, #161, #182, #188, #190, #193, #202.

## Gate issues

Both gate issues are closed. No other open issue gates Beta 6.

| Issue | What was wrong | Fixed by |
|---|---|---|
| #193 Design agent skips some delete requests | Deleting a step needed an id that the canvas snapshot does not show, so some requests were answered without the edit | #207 |
| #202 Flaky save-recovery browser test | The test was right: "Reload saved graph" could fail when an autosave was queued during the reload | #208 |

## Release gates

Same gates as Beta 5, applied to the candidate commit `f61689b`. The results are
in the [verification record](operations/beta6-verification.md) and in
`release-verification.json` of the [release](https://github.com/DATAPACT/inLUMEN/releases/tag/v1.0.0-beta.6).

- [x] Gate issues closed (#193, #202).
- [x] Release notes written ([`docs/beta6-release-notes.md`](beta6-release-notes.md)).
- [x] Verification record written ([`docs/operations/beta6-verification.md`](operations/beta6-verification.md)).
- [x] Compatibility statement ([`docs/operations/beta6-compatibility.md`](operations/beta6-compatibility.md)). Beta 6 does not certify an in-place upgrade; the statement reports what was checked.
- [ ] `inlumen-agents` 1.0.0b6 published to PyPI from the candidate commit. Built and checked, **not published on the release date**; the latest version on PyPI was still a development build.
- [x] Full regression suites, type check, lint, build, shared-file check, database integration and Compose checks pass in CI on the candidate.
- [x] The [deterministic walkthrough](prototype-quickstart.md) repeated on the candidate (scripted check; not repeated by hand in the browser).
- [x] One generated pipeline per new area designed, run and exported: typed ports with a Parallel Map, a plugin step, an Argo bundle. Done **on a local stack, not on the deployed instance**, and before the two fixes of #211 were merged. The check found the two defects that #211 fixes.
- [x] Source archive, installation instructions and release receipt built and inspected.
- [x] User acceptance (2026-11-27).
- [x] Prerelease tag and assets published (2026-11-27).

## Decisions

Agreed on 2026-11-26.

| Question | Decision |
|---|---|
| Title | Keep "Typed data flow, run outputs and plugins" |
| Date | No fixed date; tag when the release gates pass |
| Gate issues | #193 and #202 only, both closed; #194 and #195 stay in "Next" |
| Upgrade from Beta 5 | Not certified. Beta 6 stays a prerelease for controlled evaluation, like Beta 5; the compatibility statement reports what was checked |
| Agents library | `inlumen-agents` 1.0.0b6 is released together with the application tag |

## Left open at publication

- `inlumen-agents` 1.0.0b6 was not yet on PyPI.
- The generated-pipeline check was not repeated on the deployed instance.
- Run containers are not on the Compose network, so the offline plugin example
  in [Plugins](plugins.md) (`http://mock-tool:8099`) is unreachable from a run.
- Issues #194, #195, #92, #98 and #205 stayed open; see the [current plan](release-plan.md).
