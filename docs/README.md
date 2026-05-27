<div class="tool-header">
  <h1>inLUMEN: AI-assisted Pipeline Design Editor Tool</h1>
  <a href="https://www.sintef.no/">
    <img src="./images/download.png" alt="SintefLOGO">
  </a>
</div>


## **General Description**
**inLUMEN** is a DATAPACT tool that evolves traditional AI/ML/data pipeline design tools into AI agent-driven co-design environments. The story begins with simple visions (or intents) and context provided by the user.

In DATAPACT, the **intent** translates to the pipeline goal: both in terms of structure, use and compliance goals. The **context** is the input data, code snippets, artifacts, constraints/rules/requirements, and resources.

The user remains in control of the design, however supported by dedidated agents whose role is to make these visions come to life. inLUMEN materializes their intents by generating the pipeline steps as a directed graph, and gives recommandations on compliance-strengthening design choices.

Additionally, it generates deployment artifacts such as containers and workflow blueprints needed to simulate/run the pipeline. Provenance is given via tracking reports on decisions taken by the user and agents during the design process. 

## **Related Compliance aspects**
- Compliance by design
- Traceable decisions (provenance)​​

## **Main Goal/Functionalities**
- Co-design Intelligent Pipeline Design Editor (GUI with chat dialog window)
- Deployment Artifact Generation (Dockerfiles, YAML)
- Agentic AI Backend (agents assist with compliance-strenghtening design refinements)

## **Architecture**
The picture below shows the component in the DATAPACT architecture.
[![Component Diagram](./images/component-image.png)]

## **Component Definition**
inLUMEN's core functionality is provided by LLM-powered agents that serve as helpful assistants in pipeline design, translating high-level business-level intents to pure AI/data pipeline design choices. inLUMEN agents reason on user intents and context, draw pipeline steps, and give recommandations according to compliance insights provided by the user or via tool integrations. They can also support deployment artitfact generation, making pipelines deployable. The chat dialog window enables human-machine interactions to co-design pipelines. inLUMEN integrates with external DATAPACT tools through public workflow and artifact APIs.

[![inLUMEN Architecture](./images/conceptual_diagram_datapact_lumen.png)]

## **Screenshots**
[![Dashboard](./images/dashboard.png)]

## **Commercial Information**

| Organisation (s) | License Nature | License |
| SINTEF | Open Source | TBD |

## **Expected KPIs**

|What (types)|How(Process)|Values|
|------------|------------|------|
|Workflow steps generated accurately when full pipeline descriptions are given|	User Evaluation| >80%|
|Avg. time from design handoff to first executable/valid workflow| System logging | <5 minutes|
|Workflow execution success rate of generated YAML > 80% in simulated environment|	Integration test success| >80%|

## **Related Project Links**
| Project Links |
| ------------- | 	
| Software GitHub Repository --> MADT4BC/LUMEN software <https://github.com/SINTEF-9012/madt-neodash> |
| Software GitHub Repository --> inSwitch software <https://github.com/INTEND-Project/inSwitch> |

## **How To Install**
Tool is provided as a service.

### Detailed steps

Software Requirements:
1. [Docker Desktop](https://www.docker.com/%20products/docker-desktop/) installed. 
2. Node.js & npm installed - [install with nvm](https://github.com/nvm-sh/nvm#installing-and-updating)

The custom version for DATAPACT is still under development. To try the current stable version, follow the installation steps below:

Step 1: Clone this repository on your computer. 

Step 2: Navigate to the cloned project directory.

Step 3: Optional but recommended: copy `.env.example` to `.env` and adjust only the values you need.

The Docker setup derives CORS, frontend API URLs, Neo4J URI, and MinIO endpoint from the Compose service names, ports, and credential values, so you do not need separate `CORS_ALLOWED_ORIGIN`, `NEO4J_URI`, `MINIO_ENDPOINT`, `NEO4J_API_BASE_URL`, or `VITE_*_API_URL` entries for normal use.

Common values you may change include:
- `LLM_PROVIDER`, `LLM_BASE_URL`, `LLM_API_KEY`, and `LLM_MODEL` for OpenAI-compatible LLM services
- `FRONTEND_PORT`, `MINIO_API_PORT`, `NEO4J_API_PORT`, `LLM_API_PORT`
- `NEO4J_HTTP_PORT`, `NEO4J_BOLT_PORT`, `MINIO_S3_PORT`, `MINIO_CONSOLE_PORT`
- `NEO4J_AUTH`, `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD`
- `API_AUTH_TOKEN` for the public API and Swagger/OpenAPI documentation
- `AUTH_ENABLED` plus the Keycloak values when enabling authentication

For Keycloak SSO, set `AUTH_ENABLED=true` and configure `KEYCLOAK_JWKS_URL`, `KEYCLOAK_ISSUER`, and `KEYCLOAK_AUDIENCE` in the root `.env`. For a local Keycloak on port `8081`, the default frontend client values are `VITE_KEYCLOAK_URL=http://localhost:8081`, `VITE_KEYCLOAK_REALM=inlumen`, and `VITE_KEYCLOAK_CLIENT_ID=inlumen-frontend`. The same frontend still supports the embedded toolbox contract: when loaded in an iframe it waits for an `SSO_TOKEN` postMessage and infers the toolbox parent origin, so `VITE_TOOLBOX_ORIGIN` is not normally needed; it remains supported in `frontend/.env` only as a fallback for deployments that hide iframe referrers. Standalone frontend setups can also keep using `VITE_AUTH_ENABLED` and `VITE_*_API_URL` in `frontend/.env`; Docker Compose derives those values from the root `.env` unless explicitly overridden.

Step 4: Run the following command to build the docker containers:
```
docker compose up --build
```

Step 5: Wait for the stack to finish starting. The root compose file now:
- starts Neo4J, MinIO, frontend, and the Python connection APIs together
- builds the `connection` image automatically
- mounts the frontend and connection source folders for development
- auto-restarts the Python API bundle when files under `connection/` change
- connects the LLM agents to an OpenAI-compatible endpoint configured through `.env` or the UI
- is set up to behave consistently on macOS and Windows through Docker Desktop

Step 6: Configure an LLM provider. The default provider is OpenRouter:

```
LLM_PROVIDER=openrouter
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_API_KEY=sk-or-xxxx
LLM_MODEL=gpt-oss-120b
```

For OpenRouter BYOK, use your OpenRouter API key after adding the provider key in OpenRouter settings. Short model aliases such as `gpt-oss-120b` are accepted by inLUMEN and normalized before the request is sent.

You can also use Ollama Cloud with `LLM_PROVIDER=ollama_cloud`, `LLM_BASE_URL=https://ollama.com/v1`, `LLM_API_KEY=...`, and an Ollama Cloud model such as `gpt-oss:120b`. For a custom on-prem service, set `LLM_PROVIDER=custom`, `LLM_BASE_URL=https://your-host.example/v1`, `LLM_API_KEY=...`, and the model name exposed by that service. The UI configuration dialog supports the same OpenAI-compatible provider, base URL, API key, and model fields.

For the best macOS/Windows experience:
- use Docker Desktop with `docker compose`
- keep the repository on a local filesystem, not a network drive
- keep Git line endings as checked in; the repo now enforces LF for container-executed files

Note: building the containers may take around 5 minutes, please wait until Neo4J is fully started.  

Note: Once the installation is complete, the default local endpoints are localhost:8080 (frontend), localhost:5003 (MinIO API), localhost:5001 (Neo4J API), localhost:5002 (LLM/agent API), localhost:7474 (Neo4J HTTP), localhost:7687 (Neo4J Bolt), localhost:9000 (MinIO S3 API), and localhost:9099 (MinIO console). These defaults can all be changed through `.env`.

Note: To log into MinIO, use the configured root credentials from `.env`. For security reasons, change these values before using the stack outside local development.

## **How To Use**

To open the editor, go to `http://localhost:8080` by default, or the custom value you configured in `FRONTEND_PORT`. This will open the dashboard.

Backend services are currently offered at their configured localhost ports for Neo4J and MinIO. Neo4J uses `NEO4J_AUTH`, and MinIO uses `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` from `.env`.
The compose setup also exposes the internal APIs at the configured `MINIO_API_PORT`, `NEO4J_API_PORT`, and `LLM_API_PORT` values for the frontend and local debugging.

LLM agents use OpenAI-compatible Chat Completions endpoints. Configure OpenRouter, Ollama Cloud, or a custom on-prem endpoint in the dialog window or through the root `.env` file.

## **Public API and Swagger**

The public API is served by the connection analytics service on `LLM_API_PORT`, which is `5002` by default.

Required public API environment variable:

```
API_AUTH_TOKEN=change-me-local-token
```

Local URLs:

- Swagger UI: `http://localhost:5002/docs`
- OpenAPI JSON schema: `http://localhost:5002/openapi.json`
- Health check: `http://localhost:5002/health`
- Readiness check: `http://localhost:5002/ready`

Swagger UI is enabled by default. Open `http://localhost:5002/docs`, enter the token from `API_AUTH_TOKEN`, then use the Swagger `Authorize` button or the pre-filled bearer auth to run live requests.

Authentication uses a static bearer token:

```
Authorization: Bearer <API_AUTH_TOKEN>
```

`/health` and `/ready` are public. The OpenAPI JSON and all `/api/v1/*` endpoints require the bearer token. Invalid or missing tokens return `401` or `403`; validation errors return `400` or `422`; missing resources return `404`.

Example requests:

```
curl http://localhost:5002/health

curl -H "Authorization: Bearer $API_AUTH_TOKEN" \
  http://localhost:5002/openapi.json

curl -H "Authorization: Bearer $API_AUTH_TOKEN" \
  http://localhost:5002/api/v1/pipelines

curl -X POST http://localhost:5002/api/v1/pipelines \
  -H "Authorization: Bearer $API_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"Remote patient monitoring","description":"Integration-ready pipeline"}'

curl -H "Authorization: Bearer $API_AUTH_TOKEN" \
  "http://localhost:5002/api/v1/workflows?include_download_urls=true"

curl -H "Authorization: Bearer $API_AUTH_TOKEN" \
  http://localhost:5002/api/v1/pipelines/pipeline-123/artifacts/dockerfiles

curl -H "Authorization: Bearer $API_AUTH_TOKEN" \
  http://localhost:5002/api/v1/pipelines/pipeline-123/artifacts/argo-workflow.yaml
```

Available public endpoint groups:

- `Pipelines`: create, list, fetch, and list versions for the current design pipeline
- `Artifacts`: generate Dockerfiles with the configured LLM, then assemble Argo Workflow YAML deterministically from the pipeline graph and Dockerfile metadata
- `Workflows`: list available workflow metadata, associated pipeline IDs, version metadata, and temporary MinIO signed access URLs when files are available
- `Health`: public liveness and readiness checks

The public API does not expose MinIO credentials. When file access is available through MinIO, responses contain temporary signed URLs only.

## **Other Information**

inLUMEN is still under development, any current users should expect unstable behaviour.

## **OpenAPI Specification**

The live OpenAPI 3 schema is available at `http://localhost:5002/openapi.json` with bearer authentication. The schema is the source used by Swagger UI at `http://localhost:5002/docs`.

## **Additional Links**

n/a
