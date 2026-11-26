# Design edits

Checks that the design agent performs small edit requests on an existing
canvas. It calls a real model, so it is run by hand, not in CI.

- `inputs/delete-requests.json`: one six-step canvas and three delete requests
  in the wording that the agent skipped in issue #193.
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
