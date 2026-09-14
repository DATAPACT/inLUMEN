# Beta 3: real identity isolation and representative recovery

Date: September 14, 2026. Application source:
`1f39ecb` (merged PR #131). This is a verification revision, not a frozen
release candidate. [Machine-readable evidence](evidence/beta3-auth-2026-09-14/summary.json)
records the full SHA, synthetic workspace/version/run IDs, checksums, and limits.

## Topology

A separate `inlumen_beta3_auth` Compose project used fresh PostgreSQL, Neo4j,
MinIO, runner and codegen storage, a separate network, and loopback-bound host
ports. Source and application images came from the earlier isolated evaluation
checkout, updated to merged main. Unlike the default development Compose file,
Neo4j/MinIO data were named volumes so the named-volume backup tool covered them.
The frontend ran the Node 22 development image; this was not the production
Nginx/Gunicorn/Cloudflare topology. Host/Docker characteristics are recorded in
[the fresh-install walkthrough](beta3-local-walkthrough.md).

A disposable Keycloak 26.7.3 container used the official image digest recorded
in the evidence, `start-dev --import-realm`, and a local `beta3` realm. The
[Keycloak container guide](https://www.keycloak.org/server/containers) describes
this development import mechanism. Two synthetic users, Alice and Bob, had no
administrator role. The public browser client required PKCE S256. Backend
JWT verification used real Keycloak signatures/JWKS, issuer and audience;
no authentication or membership implementation was mocked. Direct grants were
enabled only on this disposable client for API checks. Browser logins exercised
the normal authorization-code flow. HTTP on loopback was test configuration,
not production guidance.

The existing application stack, external Keycloak realm, real accounts, and
provider settings were not part of this test. No LLM requests were made.

## Identity and data checks

Both users used the same node IDs and filenames with distinct input bytes:

| User | Input | Run | Verified summary |
| --- | --- | --- | --- |
| Alice | supplied three-order fixture | `c60cb5599c7e4ce8930f9bd42ff39808` | 3 orders, 49.75 |
| Bob | one order worth 7.00 | `45a925c2a24444c395c498b45677c869` | 1 order, 7.00 |

Through authenticated gateway requests, each user saved a named version,
reloaded the graph and attached files, submitted a native Dagster run, downloaded
its output, and downloaded its tested runtime snapshot. Each ZIP contained 32
entries. This exercises PostgreSQL job state, Neo4j graph state, MinIO files,
and runner artifacts under real user identities.

For **each** user, five requests with the other user's workspace header returned
404: session, graph, versions, run history, and input content. Another five direct
cross-user requests returned 404: the other bucket's input, run status, events,
bundle, and output. Three anonymous requests returned 401. Own graph labels,
version names, input hashes, outputs, and history remained correct. The exact
23 negative requests are in [isolation-checks.json](evidence/beta3-auth-2026-09-14/isolation-checks.json).

Real browser checks used one profile sequentially: Alice signed in and saw her
graph and saved versions; Alice signed out; Bob signed in and saw only his graph,
version and successful run. This checks account switching, not concurrent browser
sessions. API sessions for both users remained independent. Bob's graph and saved
version were also visible through the browser after application restoration.

## Recovery drill

No jobs were active during backup. A dummy node secret was saved for Alice;
Bob could not see its configured-secret metadata. The repository backup script
stopped the exact disposable project, archived its nine attached named volumes,
verified checksums, and restarted it:

```sh
python3 scripts/backup_volumes.py backup /tmp/inlumen-beta3-auth/backup \
  --project inlumen_beta3_auth
python3 scripts/backup_volumes.py restore /tmp/inlumen-beta3-auth/backup \
  --restore-prefix inlumen-beta3-restored-20260914
```

The [manifest](evidence/beta3-auth-2026-09-14/backup-manifest.json) and
[volume mapping](evidence/beta3-auth-2026-09-14/restore-mapping.json) record the
archives and new volumes. A second Compose project, `inlumen_beta3_restored`,
used only the restored application volumes, the retained encryption key,
and a new empty runtime directory/model-cache prefix. The source application
containers were stopped. The original disposable Keycloak container remained
the identity provider, preserving issuer/subject IDs; it was **not restored**.

The restored gateway became ready. Both users' graph, version, file, job-history,
and output checks passed again, as did all 23 negative authorization checks.
All 32 ZIP entry contents for each original run matched their pre-backup bytes;
ZIP container hashes need not match because archive timestamps can vary. Inside
the restored gateway, the retained key successfully decrypted Alice's dummy
secret; no key, token or secret value is included in the evidence.

Finally, Alice's graph and separately fetched input/code bytes were reconstructed
into a new empty workspace. Old file references were replaced by new uploads;
credentials were initially absent and a dummy credential was re-entered. A new
saved version and run `b5284d11dd6144f680214f973848138a` produced the original
3 orders/49.75 output. See [recovery-checks.json](evidence/beta3-auth-2026-09-14/recovery-checks.json).
This reconstruction used graph/file APIs, not browser JSON import, and did not
read a Beta 2 installation.

## Limits and release consequence

This is same-revision restoration of representative application data. It does
not certify Beta 2 upgrades, external identity-provider restoration, TLS/ingress,
whole-host loss, a populated model cache, active-job backup, or capacity. The
backup script does not automatically include bind mounts or detached model
volumes. The full preservation inventory and proposed fresh-install-only policy
are in [compatibility and preservation](beta3-compatibility.md).

All temporary application/Keycloak containers were stopped after verification;
test volumes, private configuration, and archives remain locally for review.
No release tag or publication was created. Final-candidate CI and release
packaging remain separate gates.
