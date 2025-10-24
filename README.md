# inLUMEN (inSwitch + LUMEN)

Powered by

[![SINTEF](./images/download.png)](https://www.sintef.no/)

| Project Links |
| ------------- | 	
| Software GitHub Repository --> inSwitch software <https://github.com/songhui/inSwitch> |
| Software GitHub Repository --> LUMEN software (Note: part of digital twin) <https://github.com/SINTEF-9012/madt-neodash>| 
| Progress GitHub Project [ADD URL] |

## **General Description**

**inSwitch** = LLM agents for context switching from business-level intents to resource-level intents in the cognitive computing continuum. inSwitch makes informed decisions on which machine, which services, what workloads are needed for the service and which versions fit the machine based on context (knowledge).

**LUMEN** = Given a natural language query/request, provide digital twin operators with on-the-fly answers to questions about assets, real-time data flows or historical data by generating and executing analyzing code.

**inLUMEN** = DATAPACT tool combines the functionalities of inSwitch and LUMEN. When designing an AI/ML pipeline, one starts from simple intents and context. In DATAPACT, the **intent** translates to the pipeline goal. The context is the input data, code snippets, artifacts, constraints/rules, and list over available platforms/APIs. We let the user decide what’s relevant to achieve their goal, by allowing user-machine interaction to refine the pipeline design. Then, inLUMEN materializes their intents by generating code needed to containerize existing artifacts, and making the pipeline deployable by providing filled-in workflow blueprints (e.g. Argo Workflows). The process may be visualized by creating graph renditions of the envisioned pipeline.

## **Architecture**

[![inSwitch Architecture](.docs/images/arch-inswitch.png)]
[![LUMEN Architecture](./docs/images/arch-lumen.png)]

## **Component Definition**
[![Component Diagram](./docs/images/component-image.png)]


## **Screenshots**
n/a TODO

## **Commercial Information**

Table with the organisation, license nature (Open Source, Commercial ... ) and the license. Replace with the values of your module.

| Organisation (s) | License Nature | License |
| ---------------  | -------------- | ------- |

## **Top Features**

n/a TODO

## **How To Install**
Step 1: Navigate to the project directory.
Step 2: Install the necessary dependencies using the following command:
```
npm i
```
Step 3: Start the development server using the following command:
```
npm run dev
```

### Requirements

Requirement: Node.js & npm installed - [install with nvm](https://github.com/nvm-sh/nvm#installing-and-updating)

### Software
n/a TODO

### Summary of installation steps

Currently offered as a service at localhost:8080.

### Detailed steps

n/a TODO

## **How To Use**

n/a TODO

## **Other Information**

TODO: The LUMEN + inSwitch version dedidated for DATAPACT use is still under development.

## **OpenAPI Specification**

n/a TODO

## **Additional Links**

n/a TODO

