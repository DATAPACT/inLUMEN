# Uploaded audio pipeline

These filesystem Tasks demonstrate the declared-artifact contract and compatibility
with legacy `data_contract` manifests. New Task packages use the public version-1
declaration in [Portable Task packages](../../docs/task-packages.md).
Create a ZIP containing the four Task folders (not this README). Import it using
**Upload code ZIP**, review the Task matches and replacements, then run with one
audio file attached to the Audio Upload Source. No code generation is needed.

Each folder contains `main.py`, `requirements.txt`, and `inlumen.task.json`.
The metadata's `data_contract` declares `inlumen.generic-node@2` and exactly one
output path, representation, format, and JSON schema. The runtime copies this
contract into its generated node manifest and enforces it on publication.

The sequence is `transcript.json` → `anonymized.json` → `entities.json` →
`sentiment.json`. Inputs are resolved from `INLUMEN_INPUT_MANIFEST`; each Task
requires one descriptor and opens its supplied path. Graph connections supply
producer and connection identity, so these packages do not embed canvas node IDs.
The empty `inputs` declaration permits connection binding from the graph; the
scripts themselves require a single file and validate transcript structure.

The implementations retain Faster Whisper, regex-based anonymization, spaCy NER,
and VADER sentiment. The anonymizer removes segments so they cannot retain the
original transcript. Its patterns do not cover all possible personal information;
artifact validation does not establish anonymization completeness or model accuracy.

For tests, see `backend/tests/test_uploaded_artifact_contract.py`. Those tests use
real subprocesses for the anonymizer and validate the uploaded-package export path;
they do not claim a full model-backed audio run.

The runtime dependency aggregator preserves the spaCy wheel URL from
requirements.txt as a named dependency for both Dagster and Argo builds.
