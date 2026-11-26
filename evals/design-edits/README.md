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
