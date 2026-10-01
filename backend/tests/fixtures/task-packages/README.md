# Task-package regression fixtures

These are test inputs, not supported application pipelines or participant bundles.
They contain source code and manifests only. Tests build ZIP archives in memory so
fixture changes remain readable in code review.

- `audio-nlp-original`: four Tasks for import normalization, mapping, and text
  handoff through the export runner. Tests execute only the standard-library
  stages; they do not download transcription models.
- `audio-sentiment-unpinned`: two deliberately invalid Tasks, with an unsupported
  output kind and mutable model revisions. Backend and browser tests verify
  validation errors, mapping retention, and the repair flow without running them.

The backend Docker build excludes `tests/`, including these fixtures.
