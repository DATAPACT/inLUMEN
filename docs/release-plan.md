# Prototype release plan and issue review

Review date: 2026-09-08. Reviewed `main` at
[`7312a7e`](https://github.com/DATAPACT/inLUMEN/commit/7312a7e6d26ed549a15ddb612e89d458c823cd54).
This is a proposed development plan, not a release announcement. GitHub issue
states and milestone assignments have not been changed by this review.
Issues #59 and #9 are excluded from scope and release completion criteria.

## Release decision

Publish the next accepted candidate as **inLUMEN Prototype Beta 3**,
tag `v1.0.0-beta.3`, marked **Pre-release** on GitHub. There are already two
officially published prereleases:
[Beta 1](https://github.com/DATAPACT/inLUMEN/releases/tag/v1.0.0-beta.1) and
[Beta 2](https://github.com/DATAPACT/inLUMEN/releases/tag/v1.0.0-beta.2).
An official, documented prototype release does not require a stable `1.0.0`.

For a new project, `0.1.0` would be a natural initial-development version.
Here, retaining the existing beta sequence avoids moving backward in semantic
version precedence. Keep published tags unchanged. Reserve `1.0.0` for an
explicitly documented compatibility and support commitment; do not use `rc`
until the intended stable scope is complete and only final verification remains.
These choices follow the distinction between initial development, prereleases,
and a defined public API in [Semantic Versioning](https://semver.org/).
GitHub's [release guidance](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository)
supports drafts and a prerelease designation for software that may be unstable.

The release should promise a reproducible evaluation workflow: install, design,
save/reopen, attach or generate code, run, inspect results, and download the
tested bundle. Describe generated code as requiring review, and compliance
assistance as advisory. Do not claim production readiness, general workload
capacity, or compatibility beyond what the release actually verifies.

## Milestone review

| Current milestone | Assessment | Proposed treatment |
| --- | --- | --- |
| [v1.0.0-beta.1 — Artifact contracts](https://github.com/DATAPACT/inLUMEN/milestone/1) | Closed historical release; no open issues. | Preserve as published history. |
| [v1.0.0-beta.2 — Native execution](https://github.com/DATAPACT/inLUMEN/milestone/2) | Closed historical release; no open issues. Its single-runner description describes that release, not all subsequent development. | Preserve; describe changes since Beta 2 in the next release notes. |
| [Stabilization & workflow](https://github.com/DATAPACT/inLUMEN/milestone/4) | Eight open issues mix bugs, small UX changes, portability, a tutorial, and a broad redesign. Useful direction, but no shared completion condition. | Retain as an uncommitted backlog. Move only accepted release work to a separate beta milestone. |
| [Exploration — Automation & integrations](https://github.com/DATAPACT/inLUMEN/milestone/3) | The two open issues cover artifact reconstruction and agent efficiency. Both need discovery and measurable scope. | Keep outside the release critical path; narrow its description to those active topics. A backlog/project view would also suit work without a finish condition. |

Proposed new milestone: **v1.0.0-beta.3 — Reproducible prototype**.

Suggested description:

> Deliver an installable prototype for controlled evaluation. Resolve the Flow
> runtime startup defect, verify the advertised saved-pipeline workflow, and
> provide a tested quickstart, explicit compatibility notes, and release evidence
> for one immutable candidate commit. Complete the release gates in
> docs/release-plan.md. General UI redesign, interactive onboarding, new
> integrations, and agent-framework changes are outside this milestone.

Use no due date until the blocking reproductions and staging checks are sized.
Assign each accepted item an owner. Completion means the release gates are met,
not that every enhancement in the backlog is finished.

## Open issue triage

There are 12 in-scope open issues. Source inspection is distinguished from
end-to-end reproduction below; existing functionality alone is not grounds to
close an issue.

| Issue | Evidence and next action | Release priority |
| --- | --- | --- |
| [#116 — Condition runtime environment](https://github.com/DATAPACT/inLUMEN/issues/116) | Still reproducible in static discovery on current main; details below. Keep open, remove legacy workspace handling from the generated Flow runtime, and add coverage for discovery and startup. | Blocking defect. |
| [#102 — Subpipeline problems](https://github.com/DATAPACT/inLUMEN/issues/102) | Real database checks confirm save/reload and attachment, including saving a parent containing another reusable version. Nested references are not recursively resolved, and a nonexistent nested version is accepted on save. See the verification below. | Fix nested-reference resolution and validation, or explicitly restrict unsupported nesting before release. Optional empty-node UX is separate. |
| [#104 — Node name/ID in Properties](https://github.com/DATAPACT/inLUMEN/issues/104) | Header still shows only the structural kind. Add the selected node's label and stable ID, including a fallback for an unnamed node; update immediately on selection. | Small, useful next change; not a release blocker. |
| [#5 — Overview refresh](https://github.com/DATAPACT/inLUMEN/issues/5) | Fetch depends on tab, active version, and callback identity; there is no explicit graph-revision dependency. Reproduce while keeping Overview open, then refresh on committed changes without overwriting metadata edits. | Near-term correctness fix; block only if a reproduced failure affects saved data or the documented workflow. |
| [#99 — Propose parameter names](https://github.com/DATAPACT/inLUMEN/issues/99) | Static discovery and read-only requirement display already exist. The inspector explicitly says it does not create parameters or store values. Remaining work is an editable suggestion flow, preserving manual Add and secret handling. | Assign to Stabilization & workflow; nonblocking enhancement. Do not close as already implemented. |
| [#100 — Code-generation time](https://github.com/DATAPACT/inLUMEN/issues/100) | An elapsed timer already appears while generation runs. Refine to durable completion duration in run history, available after reload and alongside cost/token metrics where supplied. Define queue versus execution time and avoid inventing unavailable cost. | Nonblocking observability improvement. |
| [#103 — Package import/export](https://github.com/DATAPACT/inLUMEN/issues/103) | Project JSON export and tested Dagster-bundle download exist, but do not establish a design-plus-code package round trip. Specify included files, schema, secret exclusion, and import into a fresh workspace. | Defer unified package export. Document current JSON/bundle boundaries and verify the supported downloads for Beta 3. |
| [#92 — Help/tutorial](https://github.com/DATAPACT/inLUMEN/issues/92) | Existing installation and usage docs are substantial; the requested skippable, reopenable interactive tour is separate work. | Create a small release documentation task for one tested walkthrough. Keep #92 open for the interactive tour. |
| [#63 — Preview graph changes](https://github.com/DATAPACT/inLUMEN/issues/63) | A new interaction flow requiring proposed changes, accept/cancel, and stale-revision handling. Existing save-conflict recovery is not equivalent to a preview. | Defer the feature; make automatic edit behavior clear in the quickstart. Prioritize separately if a destructive-edit defect is reproduced. |
| [#77 — UI refactoring](https://github.com/DATAPACT/inLUMEN/issues/77) | No concrete acceptance criteria. Split into named screens and observable usability problems; avoid duplicating #104 and #92. | Defer until specified. |
| [#72 — Agent call efficiency](https://github.com/DATAPACT/inLUMEN/issues/72) | Generation budgets and an evaluation scaffold now exist, but they do not demonstrate improved pipeline-designer cost or latency. Measure calls, tokens, cost, latency, and successful task outcomes before selecting an optimization. | Exploration; no framework migration as a release prerequisite. |
| [#98 — Artifact to design](https://github.com/DATAPACT/inLUMEN/issues/98) | New reconstruction capability with unresolved fidelity and supported-input scope. Start with one artifact type and a measurable reconstruction example. | Exploration; outside Beta 3. |

### Why #116 is still open

[`_control_flow_main_source`](../backend/deployment_agents.py) still emits indexed
`os.environ["INLUMEN_OUTPUT_DIR"]` inside a legacy fallback.
[`discover_runtime_environment`](../backend/runtime_environment.py) is
branch-insensitive and correctly identifies that access as required.
The generated code also references the legacy input/output manifest variables.

Reproduction from the repository root, using a configured backend interpreter:

```sh
PYTHONPATH=backend python - <<'PY'
from deployment_agents import _control_flow_main_source
from runtime_environment import discover_runtime_environment

source = _control_flow_main_source({
    "kind": "flow",
    "template": "Condition",
    "parameters": {"expression": "value.score >= 0.8"},
})
print(discover_runtime_environment(source))
PY
```

Observed: `INLUMEN_INPUT_MANIFEST` (optional), `INLUMEN_OUTPUT_DIR` (required),
and `INLUMEN_OUTPUT_MANIFEST` (optional). Expected: `[]`.
The existing `test_control_flow_runtime_passes_through_filesystem_inputs`
passes because it directly calls the generated script with standard directories;
it does not exercise the preceding required-environment validation in
[`filesystem_runtime.py`](../backend/filesystem_runtime.py).

Acceptance: use only `PIPELINE_INPUT_DIR` and `PIPELINE_OUTPUT_DIR` for generated
Flow file handling; remove legacy manifest handling as well as the directory
fallback so discovery is empty. Preserve detection of legacy variables in
user-provided scripts. Verify both Condition and Parallel Map generated packages,
the exported run-spec requirements, and a real Condition pipeline startup.
Close only after the fix is merged and its checks pass. This review reproduced
the discovery defect and ran the existing pass-through test; it did not launch
a complete pipeline or modify runtime code.

Follow-up implementation: [PR #121](https://github.com/DATAPACT/inLUMEN/pull/121)
removes the legacy handling and adds the missing regressions. The new tests
failed on the old source and passed with the fix. An isolated local Dagster
1.13.12 materialization also passed startup and file pass-through using the
generated ShellCommand with no legacy environment variables. The PR remains
separate from this documentation proposal; issue closure depends on its merge.

### #102 verification and remaining scope

Follow-up on 2026-09-08 used a disposable Neo4j 5 Community container and the
Flask adapter's real save, load, sync, and attach routes. The tested subpipeline
code is unchanged from reviewed main; the only application change in that
checkout was the separate Flow-runtime fix in PR #121. No existing application
database, browser workspace, or LLM provider was used.

| Check | Observed result |
| --- | --- |
| Save a simple Source-to-Destination graph as reusable version A; load A by its pipeline/version IDs. | Passed; the graph and public interface were retained. |
| Save version B with Source → Subpipeline referencing A → Destination; load B. | Passed; the nested immutable reference was retained. |
| Put a Subpipeline referencing B on the main graph, attach B through the endpoint, then reload the graph. | Passed for attachment. B has `resolved_graph`; its nested reference to A has neither `resolved_graph` nor a resolution error. The overall graph is reported valid. |
| Change the nested reference to a nonexistent version ID and save another reusable pipeline. | Incorrectly accepted with HTTP 200. |
| Attempt to save a graph with a connection cycle. | Rejected with HTTP 422. |

Reproduce with explicitly typed matching ports (the probe used `Text`) through
`/neo4j_reusable_pipelines`, `/neo4j_reusable_pipeline_version`,
`/neo4j_sync_graph`, `/neo4j_attach_reusable_pipeline_version`, and
`/neo4j_get_graph`. The unresolved inner graph matters because the codegen
[subpipeline task profile](../codegen/app/task_profiles.py) expects the pinned
graph in `node.subpipeline.resolved_graph`. This probe establishes missing
runtime context; it does not claim a measured generated-pipeline failure.

The next focused implementation should resolve pinned nested references within
the requesting workspace and validate their existence. Cover multiple levels,
missing versions, recursive reference cycles, and excessive nesting without
unbounded recursion. Keep persisted versions immutable; resolve graphs for
consumption without writing transient resolved copies back into saved versions.
Verify the same behavior for save, attach, and reload. Keep #102 open until that
scope and a browser save/reopen workflow are verified.

Existing frontend subpipeline and graph-validation tests also passed (19 tests
across three files). Those unit tests do not establish browser behavior. The
disposable database and its volumes were removed after verification.

## Release gates

Track these as a small release-readiness issue linked to the proposed milestone.
Each checkbox needs evidence tied to the final candidate SHA. A prior successful
run is useful baseline evidence, not validation of later changes.

- [ ] **Core workflow:** #116 fixed and merged; #102 current behavior recorded;
  a saved pipeline survives reload and can run through the advertised path.
  A deterministic example using attached code should work without paid LLM
  access. Separately verify one generation example with a configured provider,
  recording provider/model and measured results.
- [ ] **Automated validation:** all six jobs in `.github/workflows/test-suite.yml`
  pass on the candidate: backend, codegen, runner, frontend (including build,
  browser checks and dependency audit), real database integration, and Compose.
  Run `python scripts/sync_shared.py --check` and the consolidated test entry
  point as described in [operations](hardening-and-operations.md).
- [ ] **Real deployment smoke:** from a fresh checkout of the candidate, build
  and start the documented evaluation topology. Verify readiness, login,
  two-user workspace separation, save/reload, execution success and failure,
  cancellation/restart behavior, output download, and tested-bundle download.
  Record machine resources and versions. Mocked browser tests do not replace
  this check. Measure a small representative load before claiming a capacity.
- [ ] **Install and walkthrough:** publish one copyable path with prerequisites,
  configuration, sample data/code, expected result, troubleshooting, and reset
  instructions. State whether local auth or Keycloak is required for each
  supported topology. Use a tag or exact SHA, not a development branch, in the
  released instructions. The interactive tour in #92 is not required.
- [ ] **Compatibility and recovery:** document changes since Beta 2, including
  workspace ownership/auth-mode behavior, PostgreSQL job state, encryption-key
  retention, and the shared Cloudflare ingress prerequisite. Test a supported
  upgrade and isolated full application restore, or explicitly designate the
  prototype as fresh-install-only with a preservation/export path. Do not imply
  that checking out an old tag reverses database or ownership changes.
- [ ] **Release contents:** curated highlights, installation links, supported
  environment, known issues, compatibility notes, and evidence links. Verify
  the source archive builds using committed locks and contains the documented
  assets. Distribute source first; prebuilt images are optional. If shipping
  binaries/images, identify their source SHA, digests/checksums, and licenses.
- [ ] **Version identity:** declare the repository tag as the product version.
  The frontend currently has `0.0.0` and the two Python services `0.1.0`; either
  document these as internal component versions or deliberately align them and
  regenerate affected locks. Keep contract schema versions independent of the
  product release number.
- [ ] **Prototype limits:** explicitly describe reviewed generated-code use,
  the privileged codegen service's Docker access, native Dagster versus exported
  Argo execution, CPU/GPU boundaries, current export limitations, and any
  unsupported nested reuse. Provide an issue-reporting path and a private
  vulnerability-reporting contact. Do not turn later integrations into gates.

Baseline: the [main CI run](https://github.com/DATAPACT/inLUMEN/actions/runs/34226721934)
passed all six jobs. The existing [operations verification record](hardening-and-operations.md#verification-of-this-change-set-2026-09-06)
explicitly excludes a live provider benchmark, load benchmark, and full
application database restore. No new staging validation was performed in this
review, so the gates above remain unchecked.

## Development and publication order

1. Fix #116 with the missing regression coverage, and reproduce/narrow #102.
2. Finish the deterministic quickstart, smoke-test the actual deployment, and
   resolve any failures in that supported path. Take #104 as a small companion
   improvement if it does not delay verification.
3. Complete compatibility/recovery notes, version identity, and the release
   evidence record. Freeze feature scope; defer remaining enhancements.
4. Select the exact green commit, create an annotated `v1.0.0-beta.3` tag on that
   commit, and prepare a GitHub draft prerelease with all intended assets.
   Review the candidate and notes before publication. Never retarget a
   published tag; corrections get a new version.
5. Publish the prerelease only when its gates are satisfied. Follow with beta
   iterations as needed. Define stable `1.0.0` separately around a supported
   public interface, tested migration policy, and sustainable maintenance scope.

No release date, tag, or published release is created by this plan.
