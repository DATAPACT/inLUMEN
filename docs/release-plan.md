# Release plan and roadmap

Reviewed 2026-11-27, after publishing Beta 6. This page says what the next
release contains, what is planned after it, and what is not planned. Earlier
plans are kept for history: [Beta 6](beta6-release-plan.md),
[Beta 5](beta5-release-plan.md), [Beta 3](beta3-release-plan.md).

## Where we are

| | |
|---|---|
| Last release | [`v1.0.0-beta.6`](https://github.com/DATAPACT/inLUMEN/releases/tag/v1.0.0-beta.6), 2026-11-27, at `f61689b` |
| `main` since then | design from a bundle, an Argo workflow or a ZIP of code (#98); plain edits in one small model call (#218) and invalid single edits answered without a repair round (#217); plugin tools on a private Docker network (#215); one copy of the filesystem hand-off (#219) |
| Deployed instance | follows `main`: every merge is deployed after its tests pass |
| Agents library | `inlumen-agents` 1.0.0b6 is built from the release commit but not yet on PyPI; the latest version there is 1.0.0b6.dev6 |

No next release is defined yet: there is no milestone, title or gate list for
Beta 7. The issues below are the candidates for it.

## Open after Beta 6

- Publish `inlumen-agents` 1.0.0b6 to PyPI.
- The generated-pipeline check of Beta 6 was done on a local stack. It was not
  repeated on the deployed instance.

## Next: after Beta 6

Planned, not yet assigned to a release number.

| Issue | Summary |
|---|---|
| #194 | Reject edits that cannot pass validation before the agent loop (first part done in #217; a Flow behaviour switch can still take many model calls) |
| #195 | Reduce the cost of small edits: fewer calls and a smaller prompt (plain edits done in #218; the full agent's prompt is unchanged) |
| #92 | Interactive tutorial (needs a specification first) |

## Later: exploratory

Direction only; no commitment.

| Issue | Summary |
|---|---|
| #205 | Reduce duplication and split the largest files (first item done in #219) |
| #222 | Attach code and data when a design is imported from a bundle or a code ZIP |
| #223 | Show an imported design as a proposal before it replaces the canvas |
| #224 | Map Argo conditions and loops to Condition and Parallel Map steps on import |

#98 (a design from existing code or a deployment artifact) was closed by #220
and #221; #222 to #224 are its follow-ups.

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
