# Beta 6 verification

Checked commit: `f309e5f509d23f16c15a8b6722be152581ee08cf` (merge of PR
[#206](https://github.com/DATAPACT/inLUMEN/pull/206), 2026-11-26). The release
candidate also includes the release-preparation commit that adds this page;
the candidate checks at the end are repeated on that commit before the tag.

## Results so far

- **CI on `main`** for the checked commit: all 12 jobs of the Test suite passed
  (changed areas, backend, agents library, code generation service, pipeline
  runner, graph transactions and workspace isolation, frontend, both browser
  test groups, dependency audit, Compose configuration, development image). The container
  images were built and the reference VM was deployed from them.
- **Gate issues:** #193 and #202 are closed (#207, #208).
- **Deterministic quickstart:** `scripts/verify_quickstart.py --materialize`
  built the order-summary bundle and ran it in a Dagster container on Docker
  29.8.0, ARM64. The output was 3 orders totaling 49.75 and matches the expected
  summary; see [the evidence record](evidence/beta6-2026-11-26/quickstart.json).
- **Argo bundle:** the order-summary bundle built from the checked commit ran on
  k3d v5.9.0 with Argo Workflows v4.1.5: 3 of 3 steps succeeded, each with its
  CPU and memory requests, and the result in the bucket matches the expected
  summary; see [the evidence record](evidence/beta6-2026-11-26/argo-order-summary.json).
- **Beta 5 data:** the database schema, a saved pipeline and a saved version
  from Beta 5 were opened with the checked commit; see the
  [compatibility statement](beta6-compatibility.md).

## Generated pipelines (2026-11-27)

Two pipelines were designed with the chat agent, generated, run and exported on
a local stack running commit `20683d5`, with `openai/gpt-oss-120b`. The record is
in [the evidence file](evidence/beta6-2026-11-27/generated-pipelines.json).

| Pipeline | Design and code generation | Run | Argo export |
|---|---|---|---|
| Source with three text files → Parallel Map → word count → Collect → merge → Destination | 6 steps, all validated | **Failed on `20683d5`**; succeeded with the fix below (total 27 words, as expected) | **Unrunnable on `20683d5`**; 6 of 6 steps succeeded with the fix below |
| Source → Text Anonymizer plugin → Destination | 3 steps, plugin step with typed ports | Succeeded, 3 identifiers removed, with the mock tool served on the host | Not done |

Two defects were found and fixed with this record:

- A pipeline with a Parallel Map could not run from the application. Lifting a
  step into the map rewrites its `node-manifest.json` but kept the stored size
  and checksum, so the run bundle failed its integrity check.
- The Argo export of such a pipeline used the old workflow, which names an image
  the bundle never builds; the workflow stayed in `ImagePullBackOff`.

One limit was found and not fixed: run containers are not on the Compose
network, so the offline plugin example in [Plugins](../plugins.md)
(`http://mock-tool:8099`) passes **Check endpoint** but is unreachable from a
run.

Because of the two fixes, `20683d5` is no longer the candidate. The candidate
checks at the end of this page are repeated on the commit that contains them.

## Not done yet

- The generated-pipeline check **on the deployed instance**. It was done on a
  local stack instead (see above).
- The Argo bundle with reviewed models, and the run with models included in the
  image ([Argo bundle release check](argo-bundle-verification.md), sections 2
  and 5).
- `inlumen-agents` 1.0.0b6 on PyPI.
- Source archive, installation instructions and release receipt.
- User acceptance, tag and release assets.

## Final candidate gates

Before publishing, on the final merged candidate:

- Full CI: backend, agents library, codegen, runner, graph isolation, frontend,
  browser tests, shared-file consistency and Compose checks.
- Deterministic quickstart repeated; result JSON and checksum recorded.
- Candidate SHA, source archive SHA-256, installation guide and release receipt.
- Review of the draft release before the prerelease tag is created. A published
  tag is not moved.

## Limits

Automated frontend tests use mocked API responses. The checks on this page do
not establish a real identity-provider login, multi-user isolation, restart
recovery, a complete workspace backup and restore, capacity, or the quality of
generated code.
