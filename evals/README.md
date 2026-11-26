# Evaluations

Material for evaluating inLUMEN's agents: inputs, the scripts that score them,
and a short description. Product code and operations tooling do not go here.

## Layout

One folder per evaluation:

```
evals/<name>/
  README.md    what is evaluated and how to run it
  inputs/      cases, intents and reference data (tracked)
  scripts/     scoring and run scripts (tracked)
  results/     run outputs (ignored by git)
  models/      model files, for example fine-tuned checkpoints (ignored by git)
```

- `results/` and `models/` are ignored by git. To record which model files a
  result used, commit a manifest with file names and SHA-256 checksums in the
  evaluation folder.
- Scripts here must not be imported by product code. A backend test may import
  a scoring script to check it.

## Evaluations

| Folder | What it evaluates |
|---|---|
| [`pipeline-intents/`](pipeline-intents/README.md) | Pipeline designs produced for a fixed set of user intents |
| [`design-edits/`](design-edits/README.md) | Small edit requests on an existing canvas (calls a real model) |
