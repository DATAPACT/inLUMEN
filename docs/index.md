# Documentation index

All pages in `docs/`, by topic. The [README](./README.md) is the entry point for
installation and use.

## Using and deploying

- [Deterministic prototype walkthrough](./prototype-quickstart.md)
- [Production multi-user deployment](./production-multi-user.md)
- [Hardening and operations](./hardening-and-operations.md)
- [Deploy a candidate and test concurrent pipeline design and execution](./vm-load-test.md)

## Features and formats

- [Plugins](./plugins.md)
- [Portable Task packages](./task-packages.md)
- [Design from a deployment artifact](./design-from-artifact.md): import an inLUMEN bundle or an Argo workflow as a design
- [Pipeline run outputs and lineage](./operations/run-artifacts.md)
- [Authenticated uploads and codegen inputs](./operations/authenticated-file-inputs.md)
- [Argo bundle release check](./operations/argo-bundle-verification.md)

## Repository

- [Repository structure](./repository-structure.md)

## Architecture decisions

- [ADR 0001: V2 export and output contracts](./adr/0001-export-output-contracts.md)
- [ADR 0002: Native background pipeline execution](./adr/0002-native-background-execution.md)
- [ADR 0003: Port types derived from artifact contracts](./adr/0003-port-types-from-artifact-contracts.md)
- [ADR 0004: Parallel Map runs per item, closed by Collect](./adr/0004-parallel-map-and-collect.md)
- [ADR 0005: Pipeline design and code generation as libraries](./adr/0005-design-and-codegen-libraries.md)

## Contract migrations

- [Artifact contract v2 to v3](./migrations/artifact-contract-v2-to-v3.md)
- [Artifact contract v3 to v4](./migrations/artifact-contract-v3-to-v4.md)

## Releases

- [Release plan and roadmap](./release-plan.md): what the next release contains and what comes after
- [Beta 6 release plan](./beta6-release-plan.md)
- [Beta 6 release notes](./beta6-release-notes.md), [verification](./operations/beta6-verification.md) and [compatibility statement](./operations/beta6-compatibility.md)
- [Beta 5 release plan](./beta5-release-plan.md)
- [Beta 5 release notes](./beta5-release-notes.md) and [verification](./operations/beta5-verification.md)
- [Beta 4 release notes](./beta4-release-notes.md) and [verification](./operations/beta4-verification.md)
- [Beta 3 release plan and issue review](./beta3-release-plan.md)

Beta 3 verification records:

- [Compatibility and data preservation](./operations/beta3-compatibility.md)
- [Real identity isolation and representative recovery](./operations/beta3-auth-recovery.md)
- [Generation and execution lifecycle](./operations/beta3-generation-lifecycle.md)
- [Local installation and supplied-code walkthrough](./operations/beta3-local-walkthrough.md)
- [Source-package verification](./operations/beta3-source-package.md)

The run output behind these records is in `operations/evidence/`.
