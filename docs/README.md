# inLUMEN: AI-assisted Pipeline Design Editor Tool

Powered by

[![SINTEF](./images/download.png)](https://www.sintef.no/)

| Project Links |
| ------------- | 	
| Software GitHub Repository <https://github.com/DATAPACT/inLUMEN>| 
| Progress GitHub Project <https://github.com/DATAPACT/> |

## **General Description**

**inLUMEN** is a DATAPACT tool that evolves traditional AI/ML/data pipeline design tools into AI agent-driven co-design environments. The story begins with simple visions (or intents) and context provided by the user.

In DATAPACT, the **intent** translates to the pipeline goal: both in terms of structure, use and compliance goals. The **context** is the input data, code snippets, artifacts, constraints/rules/requirements, and resources.

The user remains in control of the design, however supported by dedidated agents whose role is to make these visions come to life. inLUMEN materializes their intents by generating the pipeline steps as a directed graph, and gives recommandations on compliance-strengthening design choices.

Additionally, it generates deployment artifacts such as containers and workflow blueprints needed to simulate/run the pipeline. Provenance is given via tracking reports on decisions taken by the user and agents during the design process. 

## **Architecture**

[![inLUMEN Architecture](./images/conceptual_diagram_datapact_lumen.png)]

## **Component Definition**
[![Component Diagram](./images/component-image.png)]

## **Screenshots**
[![Dashboard](./images/dashboard.png)]

## **Commercial Information**

Table with the organisation, license nature (Open Source, Commercial ... ) and the license. Replace with the values of your module.

| Organisation (s) | License Nature | License |
| ---------------  | -------------- | ------- |

## **Top Features**

- **Agentic AI**: LLM agents can serve as helpful assistants in pipeline design, translating high-level business-level intents to pure AI/data pipeline design choices. inLUMEN agents reason on user intents and context, draw pipeline steps, and give recommandations according to compliance insights provided by the user or via tool integrations. They can also support deployment artitfact generation, making pipelines deployable.  NOTE:  UNDER DEVELOPMENT
- **Human in the Loop**: Chat function to enable co-design approach to generating pipeline designs. Lab tab allows manual modifications of nodes and provides common pipeline steps.  NOTE:  UNDER DEVELOPMENT
- **Visual Aesthetics**: User-friendly graphic interface. Dark/light modes.
- **Integrated and Configurable**: Integrates with DATAPACT tools such as SIM-PIPE and LexAlign. LLM configuration enabled. NOTE:  UNDER DEVELOPMENT

## **How To Install**

Step 1: Clone this repository on your computer. 

Step 2: Navigate to the cloned project directory.

Step 3: Run the following command to build the docker containers:
```
docker compose up --build
```

Step 5: Open localhost:9099 and generate an access/secret key called minio-datapact.

Step 6: In a separate terminal/shell, navigate to the inLUMEN/connection directory. Run the commands found in inLUMEN/connection/command.txt, in the given order:

First:
```
docker build -t inlumenapi -f Dockerfile .
```

Then:
```
docker run -p 5000:5000 -p 5001:5001 -p 5002:5002 -v ${PWD}/downloads:/usr/inlumen/downloads --network=datapact_network -it inlumenapi
```

Step 7: Install different models into your Llama service. NOTE: TO BE UPDATED

### Requirements

Requirement: [Docker Desktop](https://www.docker.com/%20products/docker-desktop/) installed. 
Requirement: Node.js & npm installed - [install with nvm](https://github.com/nvm-sh/nvm#installing-and-updating)

### Software
n/a

### Summary of installation steps

Follow the steps under *How to install* to complete installation. Note: building the containers may take around 5 minutes, please wait until Neo4J is fully started.  

Once the installation is complete, frontend will be offered as a service at localhost:8080 and backend services (databases) will be offered at localhost:7474 (Neo4J), localhost:9099 (MinIO).

### Detailed steps

Follow the steps under *How to install* to complete installation.

To log into the database services, use the *database_name* as username and *password* as password. For security reasons, make sure to change these later in the docker-compose.yml file. 

## **How To Use**

To open the editor, go to localhost:8080. This will open the dashboard. 

Backend services (databases) currently offered at localhost:7474 (Neo4J), localhost:9099 (MinIO). To log into the database services, use the *database_name* as username and *password* as password. *For security reasons, make sure to change these later in the docker-compose.yml file.*

LLM-agents are (by default) powered by Llama models, but can also integrate with OpenAI models given an API key. Configure your setup in the dialog window. (TODO)

## **Other Information**

inLUMEN is still under development, any current users should expect unstable behaviour.

## **OpenAPI Specification**

n/a

## **Additional Links**

n/a

