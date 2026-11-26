# Pipeline intents

Scores recorded pipeline designs against a fixed set of user intents. The
scoring script never calls a model and never executes code.

- `inputs/pipeline-intents.json`: four intents, each with the step kinds it
  expects, label patterns for the required tasks, and cost and time budgets.
- `scripts/evaluate_pipelines.py`: checks each candidate for a valid graph,
  the required steps and connections, verified execution, and the budgets.

## How to run

1. Run each intent against the candidate model in a disposable workspace.
2. Record a JSON list with, per intent: `id`, the persisted `graph`,
   `elapsed_seconds`, `cost_usd` and `execution_success`.
3. Score it from the repository root, with an environment that has the
   `inlumen` library installed (for example `backend/.venv`):

```sh
python evals/pipeline-intents/scripts/evaluate_pipelines.py candidates.json \
  --suite evals/pipeline-intents/inputs/pipeline-intents.json \
  --output evals/pipeline-intents/results/evaluation.json
```

The exit code is 0 only when every case passes. A missing case fails. See
`docs/hardening-and-operations.md` for the release use of this evaluation.
