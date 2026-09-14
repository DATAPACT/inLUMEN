# Beta 3 source-package verification

On September 14, 2026, the tracked source at `efec43b` was exported with
`git archive --format=tar --prefix=inlumen/`, compressed with a zero gzip
modification time, and extracted outside the checkout. The full revision,
archive checksum, byte count and local image IDs are in
[the verification receipt](evidence/beta3-package-2026-09-14/summary.json).
This is a local verification artifact, not a published release asset.

All five Docker builds succeeded from that extracted source: backend, codegen,
runner, development frontend, and production frontend. Existing Docker cache
was enabled. Dependencies were installed through the committed backend hashed
lock, codegen/runner frozen uv locks, and frontend `npm ci`. No untracked files,
local `.env`, `node_modules`, or Git directory were available in the build context.
The archive contains the license, security policy, Compose files, configuration
templates, and all four order-summary example files.

Shared-file consistency and both Compose validation checks passed. The quickstart
configuration snippet created three distinct tokens and local settings with mode
0600, and refused to replace an existing `.env`. The development image started
Vite 8.2.2 on Node 22.23.2. Production `nginx -t` passed with the `backend` hostname
provided using `--add-host backend:127.0.0.1`; without Compose DNS the first
standalone check failed to resolve that upstream. No backend connection or
production login was tested by the syntax check. Docker's auth-variable naming
warnings and Vite's bundle-size advisory remain nonblocking build warnings.

The source revision differs from the previous application verification revision
`1f39ecb` only in documentation/evidence, the installation instructions, release
notes and security policy. Application files, dependencies, Dockerfiles and
runtime configuration templates are unchanged. Earlier live workflow evidence
therefore remains relevant within its stated limits; it does not become a live
test of a later commit merely through this assessment.

Before publication, name the final merged commit, verify all six CI checks,
reconcile issue #129, and create the annotated tag only on that accepted commit.
Generate source assets from that tag and record their own checksums; do not reuse
this archive's checksum for a different revision or GitHub-generated archive.
When copying `.github/releases/v1.0.0-beta.3.md` into GitHub release notes,
resolve its relative repository links to absolute URLs at the accepted tag.
Review the draft before publication. No published tag may be retargeted.
