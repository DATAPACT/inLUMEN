# inlumen-agents

inLUMEN's pipeline design and code generation agents as one Python package, used
by the inLUMEN backend, the codegen service and `inlumen-bench`. See
[ADR 0005](../../docs/adr/0005-design-and-codegen-libraries.md).

| Module | Contents | Status |
| --- | --- | --- |
| `inlumen.contracts` | The pipeline graph model: port types, Parallel Map scopes, the artifact runtime, the Task package contract, step types, node ports and definitions, graph validation, subpipelines, plugins, Project JSON | Done |
| `inlumen.design` | The pipeline design agent (extra `design`) | Done |
| `inlumen.codegen` | The code generator | Planned |

## Design a pipeline

```python
from inlumen.design.run import design, openai_model_client

model = openai_model_client(
    "openai/gpt-4.1", base_url="https://openrouter.ai/api/v1", api_key=OPENROUTER_API_KEY,
)
result = await design("Read invoice PDFs, extract totals, save one CSV", model=model, name="Invoices")
result.accepted            # passed validation (after at most one repair round)
result.project             # inLUMEN Project JSON: opens with the canvas Import button
result.requests            # one record per model request: tokens, latency, outcome
result.catalog_fingerprint # what the agent could use, with the package version
```

It runs on an in-memory graph (`inlumen.design.memory_store.InMemoryGraphStore`),
which behaves like the inLUMEN backend's Neo4j store: both run the scenarios in
`inlumen.design.store_contract`, and a backend integration test requires identical
results. Pass `graph=` to start from an existing canvas graph and `catalog=` to
offer plugins or reusable pipelines.

## Install

In this repository (editable, for development):
```sh
uv pip install -e "packages/agents[design]"
```
From another project, pinned to a release tag:
```sh
pip install "inlumen-agents[design] @ git+https://github.com/DATAPACT/inLUMEN@<tag>#subdirectory=packages/agents"
```

## Tests
```sh
cd packages/agents && uv run --extra design --extra dev pytest
```
