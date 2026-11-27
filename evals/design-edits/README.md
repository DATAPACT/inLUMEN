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
