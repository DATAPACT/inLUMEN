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

2. **Design entry point** (`inlumen.design.run`).
   ```python
   async def design(
       request: str,
       *,
       model: ChatCompletionClient,      # autogen client, from the caller
       catalog: Catalog | None = None,   # snapshot; see 4. Default: core components
       graph: dict | None = None,        # starting canvas graph; default: empty
       name: str = "",
       events: EventSink | None = None,  # see 5
       max_tool_iterations: int = 30,
   ) -> DesignResult  # message, graph, project (Project JSON), sync, accepted,
                      # repaired, requests, package_version, catalog_fingerprint
   ```
   - It runs on an `InMemoryGraphStore`. A result that still fails validation
     after the repair round is rejected and rolled back (`accepted` is False),
     as in inLUMEN.
   - The library holds the prompt, the tools' logic (parameter parsing, port
     and template resolution), guardrails, graph validation and the turn loop
     (`inlumen.design.turn.run_design_turn`: run, validate, repair once). The
     turn loop needs only a `GraphAccess` (read the graph, restore a snapshot).
   - Session concerns stay in the backend's `pipeline_agent/service.py`: chat
     state on disk, reconciling the visible canvas, preview workspaces,
     cancellation with rollback, and saving the active version.
   - The graph model the agent uses moves to `inlumen.contracts` too: step types,
     node ports and parameters, subpipeline references, graph validation, the
     connector catalog, plugin manifests, node definitions, implementation plans
     (`model_plans`), conversion between stored records and the canvas graph
     (`graph_records`) and Project JSON export (`project_ir`).

3. **`GraphStore`: graph operations, not queries** (`inlumen.design.store`).
   - One method per graph change the tools make (create or update the
     pipeline; create, insert, configure, delete and delete-all steps; connect
     and disconnect; find, create and attach reusable pipelines), plus reads
     and `restore_graph`.
   - Each operation is atomic and returns the same result rows the tools show
     the model, so the model sees the same thing with either store.
   - `Neo4jGraphStore` (backend) keeps each operation as the single Cypher
     query it was, so every graph change stays one transaction. Only the
     argument checks, port resolution and template logic move into the shared
     tools. (Moving the Cypher's own logic, such as layout and flow ids, into
     Python would have split each change across several queries.)
   - `InMemoryGraphStore` (library) reproduces each query in Python, including
     the backend's side effects (provenance normalizes the design pipeline's
     version after each change).
   - `inlumen.design.store_contract` holds scenarios run through the tools.
     `backend/tests/test_design_store_parity_integration.py` runs every scenario
     on both stores against a disposable Neo4j and requires identical results
     (ids, timestamps and attachment storage locations masked).

4. **The catalog is passed in, as a snapshot** (`inlumen.design.catalog`).
   - `Catalog` holds node definitions, installed plugin manifests and reusable
     pipelines. `Catalog.default()` has the core components and nothing else.
   - Plugin lookups go through a plugin source
     (`inlumen.contracts.plugins`): the backend registers its plugin store
     process-wide, and `design()` scopes its catalog's plugins to the run.
   - `catalog.fingerprint()` hashes the catalog and the package version, so a
     benchmark run records exactly what the agent could use.

5. **Callers supply the model and get usage reports.**
   - Design takes an autogen `ChatCompletionClient`;
     `openai_model_client()` builds one for any OpenAI-compatible endpoint.
     `design()` wraps it in a `RecordingModelClient` that reports every model
     request to the optional `EventSink` (tokens, latency, finish reason,
     error) and to `DesignResult.requests`.
   - Codegen takes a `ModelClient` protocol built around today's
     `generate_json`. The default is the current OpenAI-compatible httpx client.
     It reports to an `EventSink` in the same way, and codegen's
     `GenerationBudget` limits keep working.

6. **Codegen entry point.**
   - The generation core moves to `inlumen.codegen` (extra `codegen`):
     generator, model calls, pipeline compiler, sandbox, schemas, task
     profiles, trusted adapters, validation, model plans, resource profiles.
     `generate_pipeline_script_bundles(request, …)` keeps its signature and
     schemas, so the service calls it unchanged.
   - The model, sandbox and event sink are chosen per run through context
     variables (`use_model`, `use_sandbox`, `use_events`), not new arguments
     threaded through the generator's internal calls:
     - `ModelClient` answers one system + user prompt. Every request still
       goes through `llm.generate_json`, which keeps the budget, usage and
       JSON parsing; `HttpModelClient` (today's OpenAI-compatible call) is the
       default.
     - `DockerSandbox` (the default when `CODEGEN_DOCKER_VALIDATION_ENABLED` is
       set) and `NoSandbox` select whether execution checks run, and where
       their workspaces live. They are a switch rather than a protocol around
       the Docker calls: there is no second execution backend yet, and the
       Docker code stays one implementation.
   - `inlumen.codegen.context` builds the generator's input from a canvas
     graph. File storage is injected (`bucket_for`, `sample`): the backend
     reads MinIO, a library caller a local folder (`directory_sampler`).
   - `inlumen.codegen.run.generate(graph, model=…, sandbox=…, files=…)` is the
     entry point for other programs. It turns the deterministic fallback off
     unless asked, so a result measures the model.
   - The FastAPI app, job store, MinIO artifact store, leases, step cache,
     deployment validation and security stay in the codegen service, which
     becomes a thin wrapper around the library.
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
- A change to a Neo4j query needs the matching change in `InMemoryGraphStore`;
  the parity test fails until both agree. Add a store contract scenario for any
  new tool behaviour.
- `pipeline_agent/tools.py` becomes a small builder: the Cypher lives in
  `Neo4jGraphStore`, and the tool logic in the library.
- The Docker images for the backend and the codegen service install the package
  from `packages/agents/` instead of copying `shared/`.
- `DATAPACT/inLUMEN` must be readable wherever the bench installs from it. While
  the repo is private, the bench's Docker build needs a GitHub token.
- Not in scope: the deployment exporters (`deployment_artifacts.py`,
  `argo_bundle.py`) and inLUMEN's own Dagster bundles stay in the backend.
