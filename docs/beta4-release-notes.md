# inLUMEN 1.0 Beta 4 — Guided configuration & generation metrics

Beta 4 adds two focused improvements to the prototype evaluation workflow.
The product tag is `v1.0.0-beta.4`; the published release records the accepted
source commit and provides a checksummed source archive and installation guide.

## Changes

- Click a detected environment variable in a node's Inspector to add it to Runtime
  parameters. Its name is filled in and its empty value input receives focus.
  Existing parameters show Added; manual entry remains available. Sensitive
  variables use the existing secure-value controls.
- Background generation jobs retain their duration after completion and reload.
  Recent history shows duration; job details place duration and queue time beside
  available cost/token metrics. Worker time includes validation and repairs.
- Each resumed attempt and cache hit has its own measurement. Missing historical
  measurements remain unavailable. An interrupted worker's actual completion time
  is unknown, so recovery does not invent a duration.

## Compatibility and evaluation

The change adds optional fields to existing generation-job JSON records. Artifact
contract versions are unchanged. Historical records remain readable without a
fabricated duration. Synchronous node-generation requests do not gain background
history through this change. A Beta 3 in-place upgrade is not certified by these
checks; retain the existing fresh-install-only evaluation policy until separately
verified. Follow the [quickstart](prototype-quickstart.md) and
[compatibility and preservation guidance](operations/beta3-compatibility.md).

See [implementation verification](operations/beta4-verification.md) for test
results and limits. Package round-trip export/import (#103), graph previews (#63),
interactive tutorials (#92), broad UI refactoring (#77), and integrations remain
follow-up work. The [release verification receipt](https://github.com/DATAPACT/inLUMEN/releases/download/v1.0.0-beta.4/release-verification.json)
records the accepted SHA, final CI, source-package builds and installation checks.


## Supported evaluation and limits

This is a source-distributed prerelease for controlled evaluation. The inherited
Beta 3 evaluation environment is macOS ARM64 with Docker Desktop, Node 22 and
native Dagster execution; final package verification records the observed versions.
No prebuilt images or binaries are supplied. Builds require Internet access.
Local authentication-disabled mode shares one workspace; see the
[multi-user guide](production-multi-user.md) for Keycloak deployment.

Review generated code and dependencies before running them. The codegen service
controls Docker and must remain private. Dagster is native execution; Argo is
export-only. Native resource profiles are CPU-based. Nested reusable pipelines
remain unsupported. Design JSON and runtime ZIPs are not full editor backups and
do not carry credentials. The release makes no new production TLS, migration,
identity-provider recovery, capacity or model-quality claim.

The repository tag is the product version; component package versions remain
internal metadata. Artifact schema versions are unchanged. Source is licensed
under [Apache-2.0](../LICENSE); dependencies retain their own licenses. Report
ordinary bugs in [GitHub issues](https://github.com/DATAPACT/inLUMEN/issues), with
the source SHA and redacted reproduction details. Report vulnerabilities through
[private reporting](https://github.com/DATAPACT/inLUMEN/security/advisories/new);
see the [security policy](../SECURITY.md).
