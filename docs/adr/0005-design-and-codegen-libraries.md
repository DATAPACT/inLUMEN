# ADR 0005: Pipeline design and code generation as libraries

- Status: Accepted
- Date: 2026-11-05

## Context

inLUMEN has two agents:
- **Pipeline design** (`backend/pipeline_agent/`) turns a request into a pipeline graph.
- **Code generation** (`codegen/app/`) turns a pipeline graph into runnable Task packages.

`inlumen-bench` benchmarks both steps on the ServiceCraft corpus. Today its
`inlumen` method is a standalone reimplementation, so its results describe the
copy, not inLUMEN. To benchmark the real agents, the bench has to run inLUMEN's
own code, outside the inLUMEN stack.

What stops that today:
- **Design is tied to Neo4j.** Each graph tool in `pipeline_agent/tools.py`
  builds a Cypher query and sends it through `run_neo4j_query` (about 20 call
  sites). Part of the behaviour lives in the Cypher itself: step ordering, flow
  ids, port matching. The agent also reads its catalog (node definitions,
  plugins, connectors, reusable pipelines) from backend registries and Neo4j, and
  builds its own model client from `LLMConfig`.
- **Design is part of the backend.** It imports backend top-level modules
  (`graph_client`, `connector_catalog`, `node_definitions`, `plugins`,
  `pipeline_graph_validation`, …), so it can't be installed on its own.
- **Codegen is close already.** `generate_pipeline_script_bundles(request)`
  imports only modules inside `codegen/app/`. MinIO, the Postgres job store,
  leases and service auth sit in `main.py`. The core still calls Docker validation
  (`sandbox.py`) and its own HTTP model client (`llm.generate_json`) directly.
- **Shared code is copied.** `scripts/sync_shared.py` copies `shared/` (port
  types, map scope, artifact runtime, Task package contract) into `backend/` and
  `codegen/app/`.

## Decision

1. **One Python package, `inlumen-agents`, in this repo under `packages/agents/`.**
   - It has three modules:
     - `inlumen.contracts`: the parts of `shared/` the agents use (port types,
       map scope, artifact runtime, Task package contract and spec). It
       replaces the Python copies made by `sync_shared.py`. Copies for the
       frontend stay generated. Service plumbing (`leases.py`,
       `request_diagnostics.py`, `run_artifact_names.py`) stays in `shared/`.
     - `inlumen.design`: the design agent.
     - `inlumen.codegen`: the code generator.
   - Extras keep installs light: `inlumen-agents[design]` adds autogen, and
     `inlumen-agents[codegen]` adds the Docker client.
   - The backend, the codegen service and `inlumen-bench` all install it. There
     is one implementation of each agent, and one version to pin.
   - One package fits as long as both agents are released together. If one is
     ever needed on its own release schedule, it can be split out then.

2. **Design entry point.**
   ```python
   async def design(
       request: str,
       *,
       model: ChatCompletionClient,      # autogen client, from the caller
       catalog: Catalog,                 # snapshot; see 4
       store: GraphStore | None = None,  # default: InMemoryGraphStore
       graph: ProjectIR | None = None,   # starting design; default: empty
       events: EventSink | None = None,  # see 5
       max_tool_iterations: int = 30,
   ) -> DesignResult                     # project: ProjectIR, message, usage
   ```
   - The library holds the prompt, the tools' logic (parameter parsing, port
     resolution, template resolution), guardrails, graph validation and the
     repair loop.
   - Session concerns stay in the backend's `pipeline_agent/service.py`: chat
     state on disk, reconciling the visible canvas, preview workspaces,
     cancellation with rollback, and saving the active version.

3. **`GraphStore`: graph operations, not queries.**
   - A protocol with the operations the tools need:
     - read the graph
     - create or update the pipeline
     - add, insert, configure, delete and delete-all steps
     - connect and disconnect steps
     - list, find and create reusable pipelines
   - `InMemoryGraphStore` ships with the library and works on Project IR.
   - `Neo4jGraphStore` stays in the backend and holds the existing Cypher, moved
     out of `tools.py`.
   - Behaviour now encoded in Cypher (ordering, flow ids, port matching) moves
     into the shared tool logic, so both stores behave the same.
   - One contract test suite runs against both stores. The existing
     `test_pipeline_agent_*` tests must pass unchanged.

4. **The catalog is passed in, as a snapshot.**
   - `Catalog` holds node definitions, installed plugins, connectors and reusable
     pipelines.
   - `Catalog.default()` loads the core manifest
     (`node_definitions/manifests/core.json`) with no plugins.
   - The backend builds one from its registries and Neo4j for each request.
   - `catalog.fingerprint()` returns a content hash, so a benchmark run can
     record exactly what the agent could use.

5. **Callers supply the model and get usage reports.**
   - Design takes an autogen `ChatCompletionClient`. Codegen takes a
     `ModelClient` protocol built around today's `generate_json`. The default is
     the current OpenAI-compatible httpx client.
   - Both report every model request to an optional `EventSink`: purpose, tokens,
     reported cost, latency and outcome. inLUMEN keeps its progress events this
     way, and the bench gets accurate telemetry and can enforce its own budget.
   - Codegen's `GenerationBudget` limits keep working.

6. **Codegen entry point.**
   - `generate_pipeline_script_bundles(request, …)` keeps its signature and
     schemas, and gains `model`, `sandbox` and `events` arguments.
   - `Sandbox` is a protocol around the Docker validation calls. `DockerSandbox`
     (today's `sandbox.py`) is the default. `NoSandbox` skips execution checks,
     as `docker_validation_enabled()` does today.
   - The FastAPI app, job store, MinIO artifact store, leases and security stay
     in the codegen service, which becomes a thin wrapper around the library.
   - Output stays Task packages. Turning them into a ServiceCraft submission
     (Dagster `definitions.py` with ServiceCraft's asset names) is the bench's job,
     because those names come from ServiceCraft's contract.

7. **Versioning and installation.**
   - The package is versioned with inLUMEN releases, e.g. `1.0.0b6` for
     `v1.0.0-beta.6`.
   - Consumers install from a release tag, not a branch:
     `pip install "inlumen-agents[design,codegen] @ git+https://github.com/DATAPACT/inLUMEN@v1.0.0-beta.6#subdirectory=packages/agents"`.
     No git submodules.
   - The bench records the package version and the catalog fingerprint in every
     experiment.

8. **Order of work.** Each stage ships on its own, with inLUMEN's behaviour
   unchanged and its tests green:
   1. Create `inlumen-agents` with `inlumen.contracts`: move the agents' part
      of `shared/` and update imports; remove the Python copies.
   2. `inlumen.design`: `GraphStore` and both stores, `Catalog`, model
      injection, the `design()` entry point; the backend switches to it.
   3. `inlumen.codegen`: move the `codegen/app` core, add `Sandbox`,
      `ModelClient` and `EventSink`; the service switches to it.
   4. `inlumen-bench`: the `inlumen` method becomes a thin adapter around the
      package; `direct` stays as the baseline. Bench step 1 (design) can switch
      after stage 2.

## Consequences

- The bench measures inLUMEN's actual agents, and a change to an agent shows up
  in the next benchmark run.
- The design agent gets tests that need no Neo4j, through `InMemoryGraphStore`.
- Stage 2 is the risky one: Cypher behaviour has to be reproduced exactly in
  the shared tool logic. The store contract tests and the existing agent tests
  are the safety net.
- `pipeline_agent/tools.py` gets smaller. Its Cypher moves into
  `Neo4jGraphStore`, and its tool logic moves into the library.
- The Docker images for the backend and the codegen service install the package
  from `packages/agents/` instead of copying `shared/`.
- `DATAPACT/inLUMEN` must be readable wherever the bench installs from it. While
  the repo is private, the bench's Docker build needs a GitHub token.
- Not in scope: the deployment exporters (`deployment_artifacts.py`,
  `argo_bundle.py`) and inLUMEN's own Dagster bundles stay in the backend.
