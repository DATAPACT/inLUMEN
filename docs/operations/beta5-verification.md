# Beta 5 implementation verification

Implementation commit: `4b64b48de350f3e126eb261d5d040c092dbebe7b` (merge of PR
[#136](https://github.com/DATAPACT/inLUMEN/pull/136), 2026-09-16). The final
release candidate will also include the release-preparation documentation commit;
repeat candidate checks against that exact source before publication.

## Results so far

- GitHub PR CI: all six checks passed on PR head `bd0bf323e43fdac9434cea36eaaf7440021789f2`.
- Frontend code ZIP unit tests: 3 passed locally.
- Frontend code ZIP browser test: passed locally. It checks placement beside
  upload, absence of the top-bar Package action, exact code bytes, exclusion of
  source data, and matching the downloaded archive with the existing uploader.
  API data in this browser test is simulated; it does not test a deployed service.
- Frontend typecheck: passed locally.
- Merge CI for `4b64b48de350f3e126eb261d5d040c092dbebe7b`: all six checks passed.
- Deterministic quickstart bundle and native Dagster materialization passed on
  Docker Desktop 29.7.2, ARM64. The output was 3 orders totaling 49.75; the
  result checksum and run details are in
  [the evidence record](evidence/beta5-2026-09-16/quickstart.json).

## Final candidate gates

Before publishing, rerun full CI and the checks below against the final merged
candidate, then record versions, outputs, and checksums:

- Full backend, codegen, frontend, runner, graph isolation, and Compose checks.
- Shared-file consistency, production Compose validation, source-archive Docker
  builds, and quickstart configuration safety checks.
- Deterministic native Dagster quickstart using the supplied order-summary sample;
  retain the result JSON and checksums.
- Candidate SHA, source archive SHA-256, installation guide, release receipt, and
  compatibility limits.
- Review the draft release contents before creating the immutable prerelease tag.

Automated frontend tests use mocked API responses. They do not establish a live
deployment, real identity-provider login, multi-user isolation, restart recovery,
or a complete workspace backup and restore.
