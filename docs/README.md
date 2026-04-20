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
inLUMEN's core functionality is provided by LLM-powered agents that serve as helpful assistants in pipeline design, translating high-level business-level intents to pure AI/data pipeline design choices. inLUMEN agents reason on user intents and context, draw pipeline steps, and give recommandations according to compliance insights provided by the user or via tool integrations. They can also support deployment artitfact generation, making pipelines deployable. The chat dialog window enables human-machine interactions to co-design pipelines. inLUMEN integrates with DATAPACT tools such as SIM-PIPE and LexAlign.

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

Step 3: Run the following command to build the docker containers:
```
docker compose up --build
```

Step 4: Wait for the stack to finish starting. The root compose file now:
- starts Neo4J, MinIO, frontend, Ollama, and the Python connection APIs together
- builds the `connection` image automatically
- mounts the frontend and connection source folders for development
- auto-restarts the Python API bundle when files under `connection/` change
- pulls the default Ollama model automatically on first run

Step 5: Optional: if you want GPT-based models, create a root `.env` file and set your API key there:

```
OPENAI_API_KEY=sk-xxxx-(...)-xxxx
```

Note: building the containers may take around 5 minutes, please wait until Neo4J is fully started.  

Note: Once the installation is complete, frontend will be offered as a service at localhost:8080 and backend services will be offered at localhost:5000 (MinIO API), localhost:5001 (Neo4J API), localhost:5002 (LLM/agent API), localhost:7474 (Neo4J), localhost:9000 (MinIO S3 API), localhost:9099 (MinIO console), and localhost:11434 (Ollama).

Note: To log into MinIO, use the configured root credentials from `docker-compose.yml` or `.env`. For security reasons, change these values before using the stack outside local development.

## **How To Use**

To open the editor, go to localhost:8080. This will open the dashboard. 

Backend services are currently offered at localhost:7474 (Neo4J), localhost:9000 (MinIO S3 API), localhost:9099 (MinIO console), and localhost:11434 (Ollama). Neo4J uses the credentials defined by `NEO4J_AUTH` and MinIO uses `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` from `docker-compose.yml` or `.env`.
The compose setup now also exposes the internal APIs at localhost:5000, localhost:5001, and localhost:5002 for the frontend and local debugging.

LLM-agents are (by default) powered by Llama models, but can also integrate with OpenAI models given an API key. Configure your setup in the dialog window. 

## **Other Information**

inLUMEN is still under development, any current users should expect unstable behaviour.

## **OpenAPI Specification**

n/a

## **Additional Links**

n/a
