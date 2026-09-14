# Beta 3 generation and execution lifecycle verification

September 14, 2026. Baseline `87e03b14283d39ff8bbb00ac5abea0e6ff1e9b67`;
verified application change `a16431ffde792818ee1337ace815d16b2daeab77`.
The existing isolated local installation from the
[previous walkthrough](beta3-local-walkthrough.md) was restarted with its test
storage retained. These are gateway API checks against real PostgreSQL, Neo4j,
MinIO, codegen, Docker, and runner services; no responses or provider calls were
mocked. They do not establish browser button behavior or Keycloak isolation.

## Generation blocker and repair

`POST /api/nodes/summary/generate-script` initially returned HTTP 502 because
codegen rejected `options.repair_attempts=7` with HTTP 422: the receiving schema
allows at most six. No model call occurred for that rejected request.

Node generation, pipeline generation, and resume now normalize repair attempts
to 0–6, defaulting to six. Explicit zero is retained; excessive values are bounded
and malformed values use the default. Resume metadata records the normalized
value actually sent. Tests exercise the node route and validate pipeline options
against the real codegen Pydantic schema, including zero and out-of-range values.

The backend suite ran **329 tests: 319 passed, 10 optional skips** through the repository's
consolidated test command. An earlier invocation inside the running backend
container was unsuitable: it lacked sibling contracts/codegen/scripts and
inherited a live database URL. The complete-checkout invocation passed.

## Real model generation and execution

With explicit user approval, the saved shared LLM configuration was copied into
the isolated installation and enabled there only. The original shared setting
remained disabled at revision 3. The isolated credential copy was removed after
the model check; no provider key is included in this evidence.

- OpenRouter, `openai/gpt-oss-120b`, provider restriction `cerebras`.
- Target: the supplied order-summary example's Task. Requested standard-library
  Python, Decimal summation, three rows, and total string `49.75`.
- Gateway generation succeeded in **6.062 seconds**, one request/attempt.
- Reported usage: 2,065 prompt tokens, 854 completion tokens, 2,919 total.
  The service reported `cost_usd=0.0`; this is not independently verified billing.
- Reviewed the [generated Python](evidence/beta3-lifecycle-2026-09-14/generated-main.py)
  before execution: CSV input and JSON output through the standard workspace
  directories; no external service calls or third-party dependencies.
- Native Dagster run `14413f87d5f74dd3a6b3da5a9bfaddb9` succeeded. Both retrieved
  summary artifacts parsed as `{"order_count":3,"total_amount":"49.75"}`.
- A development auto-reload reset one status-poll connection while tests were
  being edited. The run was retrieved using the same ID, not resubmitted.

Evidence: [generation response](evidence/beta3-lifecycle-2026-09-14/generation-response.json),
[measurement](evidence/beta3-lifecycle-2026-09-14/generation-measurement.json),
[run](evidence/beta3-lifecycle-2026-09-14/generated-run.json),
[output](evidence/beta3-lifecycle-2026-09-14/generated-summary-0.json).

## Failure, cancellation, and gateway restart

Only the isolated example Task's uploaded code was replaced for these fixtures.
Named snapshots and the user's original installation were not modified.

| Scenario | Intervention and observed outcome |
| --- | --- |
| Intentional failure | Code raises `RuntimeError("Intentional isolated release-check failure")`. Run `3e630aac83fe469c9b9d7d4581da4222` becomes `failed` with `dagster_execution_failed` and the original message. |
| Active cancellation | Code prints a start marker, sleeps, then would calculate the summary. After task startup, DELETE run `f507a6d45a10460d8a4557ef297fbc17` transitions through `cancelling` to `cancelled`. A repeated DELETE remains cancelled; no labelled worker container remains running. |
| Gateway restart | Restarted only the isolated backend after the delayed task started. Run `fe3555cb09ca42c6a3b6c861908726f9` retained its ID and completed successfully after the gateway returned. |

Receipts: [failure](evidence/beta3-lifecycle-2026-09-14/failure-run.json),
[cancellation](evidence/beta3-lifecycle-2026-09-14/cancel-run.json),
[gateway restart](evidence/beta3-lifecycle-2026-09-14/gateway-restart-run.json).

## Abrupt runner restart and duplicate-execution check

A delayed Task emitted its start marker before the isolated runner was killed
with SIGKILL and started again. The codegen execution service and its worker
remained running. After the old runner lease expired, the new runner recovered
the completed receipt for the same run, without resubmitting it.

Run `1138433a8b774cd88f7cf241ada07220` completed successfully. A live Docker event
capture, started before submission, recorded exactly **one** worker start for
that run ID across the interruption. An earlier recovery run also succeeded,
but its after-the-fact Docker event query returned no retained events; it was
repeated with live capture to establish the no-duplicate-execution result.

Evidence: [pre-interruption state](evidence/beta3-lifecycle-2026-09-14/runner-restart-before.json),
[terminal receipt](evidence/beta3-lifecycle-2026-09-14/runner-restart-run.json), and
[live worker-start event](evidence/beta3-lifecycle-2026-09-14/runner-restart-docker-starts.jsonl).
The execution ADR now distinguishes receipt recovery from the legacy executor's
cancel-and-fail behavior.

## Scope and follow-up

The real-generation, explicit failure, active cancellation, gateway restart, and
runner SIGKILL scenarios passed after the repair-limit correction. These are
bounded local fixtures, not general reliability or capacity claims. Codegen
service crashes, whole-host failure, unknown-receipt outcomes, browser controls,
and two-user Keycloak behavior were not tested in this change set. Full
application restore and upgrade/preservation policy remain separate release gates.

The shared LLM stayed disabled in the user's original installation, and only
its temporary isolated credential copy was removed. The test installation is
stopped after verification, retaining its test storage and artifacts. The repair
needs review, CI, and merge before the final release candidate can be selected.
