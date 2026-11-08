# inlumen-agents

inLUMEN's pipeline design and code generation agents as one Python package, used
by the inLUMEN backend, the codegen service and `inlumen-bench`. The design
rationale is in inLUMEN's ADR 0005 (`docs/adr/0005-design-and-codegen-libraries.md`).

| Module | Contents | Status |
| --- | --- | --- |
| `inlumen.contracts` | The pipeline graph model: port types, Parallel Map scopes, the artifact runtime, the Task package contract, step types, node ports and definitions, graph validation, subpipelines, plugins, Project JSON | Done |
| `inlumen.design` | The pipeline design agent (extra `design`) | Done |
| `inlumen.codegen` | The code generator (extra `codegen`) | Done |

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

## Generate Task packages

```python
from inlumen.codegen.run import generate, http_model
from inlumen.codegen.sandbox import DockerSandbox

model = http_model(
    "openai/gpt-5.2-codex", base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY, provider="openrouter",
)
result = await generate(design_result.graph, model=model, files="data/")
result.accepted            # the generated pipeline passed integration validation
result.packages            # {flow_id: {filename: content}}: one Task package per step
result.write_packages("out/")
result.requests            # one record per model request: tokens, cost, latency
```

It runs the same planning, generation, repair and validation as the codegen
service. `sandbox` defaults to `NoSandbox()` (static checks only); pass
`DockerSandbox()` to run the pipeline on sample inputs, which needs a Docker
daemon. `files` is a folder of attached files, looked up as
`<folder>/<flow_id>/<name>` and then `<folder>/<name>`. `runtime=` overrides the runtime
constraints for a fixed target environment, e.g. `{"allowed_packages": [...],
"network_allowed": False}`. Any object with a
`name` and an async `complete(system_prompt=…, user_prompt=…, json_output=…,
max_tokens=…)` returning `inlumen.codegen.model.ModelReply` can be the model.

## Install

From PyPI (pre-releases need an exact version or `--pre`):
```sh
pip install "inlumen-agents[design,codegen]==1.0.0b6.dev2"
```
Extras: `design` for `inlumen.design`, `codegen` for `inlumen.codegen`;
`inlumen.contracts` needs neither.

In the inLUMEN repository (editable, for development):
```sh
uv pip install -e "packages/agents[design,codegen]"
```

## Tests
```sh
cd packages/agents && uv run --extra design --extra codegen --extra dev pytest
```
