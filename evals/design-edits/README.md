# Design edits

Checks that the design agent performs small edit requests on an existing
canvas. It calls a real model, so it is run by hand, not in CI.

- `inputs/delete-requests.json`: one six-step canvas and three delete requests
  in the wording that the agent skipped in issue #193.
- `inputs/delete-requests-paraphrased.json`: the same three requests on a
  seven-step canvas whose labels differ from the request wording ("Identify
  Language" for "language detection"). Pass it with `--suite`.
- `scripts/run_delete_requests.py`: sends each request to the agent on an
  in-memory canvas and checks that exactly the named step is gone and the
  result is accepted.

- `inputs/edits-not-valid-alone.json` and
  `scripts/run_edits_not_valid_alone.py`: edits that cannot pass validation
  alone (issue #194), on a six-step canvas with a Parallel Map and a Collect:
  seven disconnects, three Flow behavior switches and "remove the last step".
  Three compound requests contain such an edit as one step and must still
  change the canvas. The report counts model calls and repair rounds per kind;
  `--kind` runs one kind only.

- `inputs/simple-edits.json` and `scripts/run_simple_edits.py`: eight delete,
  three clear and four connect requests on an eight-step canvas, plus five
  other requests (add, insert, replace, two deletes at once, a question) that
  the quick path must hand to the full agent (issue #195). The report counts
  model calls and prompt tokens per kind. Run it again with
  `INLUMEN_DESIGN_FAST_EDITS=0` to compare with the full agent alone.

- `scripts/measure_prompt.py`: reports the size of one request of the full
  agent by part (rules, component palette, each tool, request and canvas) on
  the six-step canvas. It calls no model and needs no key.

## How to run

With an environment that has `inlumen-agents[design]` installed (for example
`packages/agents/.venv`) and the key of the provider:

```sh
OPENAI_API_KEY=... packages/agents/.venv/bin/python \
  evals/design-edits/scripts/run_delete_requests.py \
  --base-url https://openrouter.ai/api/v1 --model openai/gpt-oss-120b --repeat 3
```

One run is three requests of about four model calls each. The report is
written to `results/delete-requests.json` (ignored by git). The exit code is 0
only when every request passes.

## Results on 26 Nov 2026

`openai/gpt-oss-120b` on Cerebras through OpenRouter, both canvases, deletes
performed out of requests sent:

| Reasoning | Before the fix in #207 | With the fix |
|---|---|---|
| Provider default | 18 of 18 (4 to 5 model calls each) | 9 of 9 (3 calls each) |
| Low | 10 of 12 | 24 of 24 (2 to 4 calls each) |

Both skipped requests took exactly two model calls: an `overview`, then a
description of the pipeline. The skip only showed up with low reasoning, which
is the script's default.

## Edits that are not valid alone, 27 Nov 2026

`openai/gpt-oss-120b` through OpenRouter, low reasoning. "Before" is `main`
at `8558bb5`; "after" adds the tool note and the undo without a repair round.
The number of requests differs per cell because not every kind was repeated
equally often.

| Kind | Before: model calls | Before: repair rounds | After: model calls | After: repair rounds |
|---|---|---|---|---|
| Disconnect | 5 to 7 (7 requests) | 7 of 7 | 2 in 20 of 21; 3 in one | 0 of 21 |
| Remove the only destination | 5 (1 request) | 1 of 1 | 2 in 3 of 3 | 0 of 3 |
| Flow behavior switch | 6 to 11 (9 requests) | 9 of 9 | 2 in 7 of 9; 4 and 15 | 1 of 9 |
| Compound (must change the canvas) | 2 to 7, 9 of 9 correct | 1 of 9 | 2 to 7, 8 of 9 correct | 1 of 9 |

In every single-edit request the canvas ended valid, before and after: either
unchanged with an explanation, or (once, after) changed by a different valid
edit. The compound request that failed after the change deleted the step and
did not add its replacement; that delete is valid alone, so it carried no
note. With an earlier wording of the note, which told the agent to stop, 3 of
9 compound requests stopped after their first step; the current wording tells
it to carry on when the request names further steps.

The target of issue #194, at most two model calls for all of these requests,
is met for 30 of 33. "Change Collect invoices to a Parallel Map" still took 4
and 15 calls in two of three runs: the agent made several edits, so the turn
was not a single edit and kept its repair round.

## Simple edits, 27 Nov 2026

`openai/gpt-oss-120b` through OpenRouter, low reasoning. "Full agent" is the
same code with `INLUMEN_DESIGN_FAST_EDITS=0`.

Where the prompt of one full-agent call goes (six-step canvas, about 7,400
tokens): rules 2,800, tool descriptions 3,300 (`create_steps` alone 1,040),
request and canvas 1,070, component palette 220.

| Kind | Full agent: calls, prompt tokens, time | Quick path: calls, prompt tokens, time | Correct with the quick path |
|---|---|---|---|
| Delete (8 requests) | 3 calls, about 24,400, 1.6 to 2.4 s | 1 call, about 710, 0.3 to 0.6 s | 24 of 24 |
| Clear (3) | 2 calls, about 14,200, 1.1 s | 1 call, about 710, 0.3 to 0.4 s | 9 of 9 |
| Connect (4) | 3 calls, 21,500 to 25,500, 1.6 to 1.8 s | 1 call, about 710, 0.4 to 0.5 s | 12 of 12 |
| Other (5) | 1 to 6 calls | one more call of about 710 tokens | 15 of 15 |

The full agent was run once per request for the first three rows (15 of 15
correct) and four times for the other requests (20 of 20 correct); the quick
path three times. Asked 20 times about the five other requests, the router
answered "other" 20 times.

Not measured: the self-hosted model, default reasoning, requests in other
languages, and how often real requests are plain edits.

## Size of the full agent's prompt, 28 Nov 2026

`scripts/measure_prompt.py`, request "delete language detection" on the
six-step canvas of `inputs/delete-requests.json`. Tokens of tiktoken's
`o200k_base` encoding; no model was called. "Before" is `main` at `8f40894`.

| Part | Before | After |
|---|---|---|
| Rules in the system prompt | 2,805 | 2,696 |
| Component palette | 235 | 235 |
| Tool descriptions (14 tools) | 3,308 | 3,233 |
| of which `create_steps` | 1,041 | 976 |
| of which `connect_steps` | 488 | 478 |
| Request and canvas | 1,070 | 616 |
| of which copies of the canvas (453 each) | 2 | 1 |
| Total of one call | 7,418 | 6,780 |

One call is 638 tokens (9%) smaller. What changed:

- The canvas is sent once when the stored graph holds the same steps and
  connections as the visible canvas (save time and positions are not
  compared). When they differ, or no canvas came with the request, both
  snapshots are sent as before.
- `create_steps`: a more compact JSON example with the same four steps, and
  shorter wording. Every field is still named.
- `connect_steps`: a shorter JSON example. Its port-id paragraph now also
  names the label prefix and the named inputs of a joining Task, so its size
  hardly changes.
- Two rules were removed from the system prompt because a tool description
  states them: `create_steps` is the only tool that creates steps and takes
  them in one call; after deleting a branch point or merge, reconnect with
  `connect_steps`. The port-id rule stays in the system prompt: a backend
  test requires it there.

Limits. The count is an estimate: a provider adds a few tokens per message
and per tool and may use another tokenizer. The snapshot holds a save time,
so the request part varies by about two tokens between runs. Whether the
visible canvas and the stored graph give the same snapshot in the deployed
application (Neo4j store) was not checked; if they differ there, both are
still sent and that part of the saving does not apply.

### Check with a real model, 28 Nov 2026

`openai/gpt-oss-120b` through OpenRouter, low reasoning, one run of each
script on `main` at `8f40894` and on the shorter prompt. The run used the
first version of the shorter prompt, whose `create_steps` example had three
steps; the fourth step was put back afterwards (46 tokens), so the final
text is closer to `main` than the measured one.

| Check | `main` | Shorter prompt |
|---|---|---|
| `run_simple_edits.py`, quick path on | 20 of 20 correct | 20 of 20 correct |
| `run_simple_edits.py`, `INLUMEN_DESIGN_FAST_EDITS=0` | 20 of 20 correct, 449,056 prompt tokens | 20 of 20 correct, 361,184 prompt tokens |
| `run_edits_not_valid_alone.py` | 14 of 14 correct | 14 of 14 correct |

With the full agent alone, the 20 requests used 20% fewer prompt tokens in
total. This total also depends on the number of model calls, which varies
between runs, so one run does not separate the two effects. Prompt tokens
per call, read from the requests that took the same number of calls in both
runs (a lone disconnect, three calls): about 4,950 on `main` and 4,490 on
the shorter prompt, as reported by the provider. `run_delete_requests.py`
was not run.
