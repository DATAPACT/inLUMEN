# AI-backed code-generation plan

1. Read the canonical inLUMEN graph and node contracts.
2. Classify each node against a reviewed task profile.
3. Resolve trusted implementation plans and pinned dependencies.
4. Ask the configured coding model for one canonical pipeline program.
5. Validate syntax, dependency policy, semantic profile alignment, edge
   contracts, and (when requested) sample execution in an isolated sandbox.
6. Feed validation errors back to the coding model for bounded repair attempts.
7. Compile the validated canonical program into independent node scripts.
8. Persist only valid runtime packages and remove stale generated files.
9. Let the deployment exporter build Dagster and Argo artifacts from those
   packages.

The generator emits source, requirements, manifests, and validation reports.
It uses the dedicated code model and does not emit per-node Dockerfiles.
Deployment Dockerfiles remain deterministic and model-free.
