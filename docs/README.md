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

Step 4: Open localhost:9099, log in, and generate an access/secret key called minio-datapact.

Step 5: In a separate terminal/shell, navigate to the inLUMEN/connection directory. Run the commands found in inLUMEN/connection/command.txt, in the given order:

First:
```
docker build -t inlumenapi -f Dockerfile .
```

Then:
```
docker run -p 5000:5000 -p 5001:5001 -p 5002:5002 -v ${PWD}/downloads:/usr/inlumen/downloads --network=datapact_network -it inlumenapi
```

Step 6: Install the base Ollama model into your Llama service by accessing *datapact-llm* in Docker Desktop and executing (in Exec) the following line:

```
ollama run llama3.1:8b
```

Note: building the containers may take around 5 minutes, please wait until Neo4J is fully started.  

Note: Once the installation is complete, frontend will be offered as a service at localhost:8080 and backend services (databases) will be offered at localhost:7474 (Neo4J), localhost:9099 (MinIO).

Note: To log into the database services, use the *database_name* as username and *password* as password. For security reasons, make sure to change these later in the docker-compose.yml file. 

## **How To Use**

To open the editor, go to localhost:8080. This will open the dashboard. 

Backend services (databases) currently offered at localhost:7474 (Neo4J), localhost:9099 (MinIO). To log into the database services, use the *database_name* as username and *password* as password. *For security reasons, make sure to change these later in the docker-compose.yml file.*

LLM-agents are (by default) powered by Llama models, but can also integrate with OpenAI models given an API key. Configure your setup in the dialog window. 

## **Other Information**

inLUMEN is still under development, any current users should expect unstable behaviour.

## **OpenAPI Specification**

n/a

## **Additional Links**

n/a

