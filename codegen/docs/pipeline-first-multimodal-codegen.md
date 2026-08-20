# Pipeline-first AI code generation

inLUMEN builds a bounded pipeline plan from node descriptors, edge contracts,
sample metadata, and reviewed task profiles. It then creates one deterministic
generation prompt. The dedicated coding model creates one canonical Python
program, validation failures are returned through a bounded repair loop, and
the validated program is compiled into independently runnable node packages.

This preserves cross-node contracts while keeping runtime packages portable.
Multimodal inputs are represented through typed descriptors and bounded sample
metadata; trusted adapters constrain supported audio, image, document, and
model workflows. Unsupported or unsafe model output fails validation.

Dockerfiles belong to the deployment export. Dagster and Argo use consolidated
target-level build files rather than `Dockerfile.<flow_id>` files.
