# inLUMEN 1.0 Beta 5 — Runtime code exchange

Draft release notes. The candidate adds a code ZIP download beside the existing
code ZIP uploader. The final release will identify the accepted source commit and
include a checksummed source archive and installation guide.

## Changes

- Download attached Task code from **Library → Run → Runtime code** as
  `pipeline-code.zip`. Each Task has its own folder, including its node ID so
  duplicate labels remain distinct. The existing uploader recognizes these
  folders and retains its usual Task-name matching.
- The archive preserves attached code file names and contents. It excludes source
  data and does not package the pipeline design, reusable pipeline definitions,
  credentials, or other workspace state. No **Package** action is added to the
  top toolbar.
- Download stops with an error if a file is unavailable, there is no Task code,
  or the ZIP would exceed the uploader's 50 MB limit; it does not return a partial
  archive.

## Compatibility and evaluation

Beta 5 remains a source-distributed prerelease for controlled evaluation. It does
not change the artifact schema or certify an in-place upgrade. Follow the
[quickstart](prototype-quickstart.md) and
[compatibility and preservation guidance](operations/beta3-compatibility.md).
The archive is a runtime-code exchange format, not a full editor backup.

See [implementation verification](operations/beta5-verification.md) for test
results and limits. The next scoped backlog item is graph preview and confirmation
behavior in [issue #63](https://github.com/DATAPACT/inLUMEN/issues/63).

## Supported evaluation and limits

This is a source-distributed prerelease for controlled evaluation. No prebuilt
images or binaries are supplied; builds require Internet access. Review generated
code and dependencies before running them. The codegen service controls Docker
and must remain private. Dagster is native execution; Argo is export-only. Native
resource profiles are CPU-based. Nested reusable pipelines remain unsupported.
The release makes no new production TLS, migration, identity-provider recovery,
capacity, or model-quality claim.

The repository tag is the product version; component package versions remain
internal metadata. Artifact schema versions are unchanged. Source is licensed
under [Apache-2.0](../LICENSE); dependencies retain their own licenses. Report
ordinary bugs in [GitHub issues](https://github.com/DATAPACT/inLUMEN/issues), with
the source SHA and redacted reproduction details. Report vulnerabilities through
[private reporting](https://github.com/DATAPACT/inLUMEN/security/advisories/new);
see the [security policy](../SECURITY.md).
