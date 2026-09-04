# Production multi-user deployment

This deployment runs one inLUMEN installation on a VM and gives every Keycloak
identity a private personal workspace. The browser sends its Keycloak access
token; the backend validates issuer, signature, expiry, and audience, then maps
the immutable `(issuer, subject)` pair to an internal user and workspace.

The workspace ID is selected by the authenticated backend. A browser may send
`X-InLumen-Workspace-Id` to select another workspace it belongs to, but it
cannot use that header to gain membership. Requests for unknown or unauthorized
workspaces return 404.

## Isolation boundaries

- PostgreSQL rows use `workspace_id` in their primary keys and filters. This
  includes code-generation jobs, pipeline runs, node secrets, chat state, and
  chatbot configurations.
- Neo4j nodes receive a server-derived workspace label. All graph sessions add
  that label, and raw Cypher is rejected unless every node is introduced through
  an inLUMEN-owned label.
- MinIO bucket names contain a non-reversible workspace digest. Generic bucket
  endpoints reject buckets outside the current workspace.
- Runner artifacts are stored below a workspace-specific directory. The runner
  and codegen APIs require the backend's private service token and trusted
  workspace header in production.
- With `AUTH_ENABLED=false`, the application uses the fixed `local-workspace`
  identity and retains the prior single-user behavior.

## Keycloak setup

Create or reuse a realm and a public client such as `inlumen-frontend`:

1. Enable the authorization-code flow and PKCE S256. Do not create a browser
   client secret.
2. Add `https://inlumen.example.com/*` as a valid redirect URI and
   `https://inlumen.example.com` as a web origin.
3. Ensure the access token contains either the configured audience, `azp`, or
   `client_id`. Set `KEYCLOAK_AUDIENCE` to that value.
4. Use the realm's exact HTTPS issuer and JWKS URLs in the production env file.

The application does not trust email or username as an identifier. Renaming a
Keycloak user therefore does not move or duplicate their data.

## VM and Cloudflare setup

1. Install Docker Engine and the Compose plugin. Keep Docker, PostgreSQL,
   Neo4j, MinIO, backend, runner, and codegen ports closed to the Internet.
2. Copy `.env.production.example` to `.env.production`, replace every sample
   secret, and pin infrastructure image versions or digests tested in staging.
3. Create a remotely managed Cloudflare Tunnel and route the public inLUMEN
   hostname to `http://frontend:8080`. Put its token in `.env.production`.
4. Start the stack:

   ```sh
   docker compose --env-file .env.production -f docker-compose-prod.yml up -d --build
   ```

5. Check startup and the one-shot database migration:

   ```sh
   docker compose --env-file .env.production -f docker-compose-prod.yml ps
   docker compose --env-file .env.production -f docker-compose-prod.yml logs migrate backend
   ```

The production Compose file fails closed: it always sets `AUTH_ENABLED=true`,
requires Keycloak and database settings, serves a compiled frontend through
Nginx, runs the backend with Gunicorn, publishes no origin ports, and exposes
only the frontend to Cloudflare Tunnel over the private Compose network.

## Operations

### Auth-mode changes

`AUTH_ENABLED=false` is deliberately a single shared local identity, not a
multi-user mode. The gateway records the selected mode in PostgreSQL and
refuses to start if a deployment's setting changes. This prevents a browser or
operator from accidentally treating local data as account data (or vice versa).

Use a distinct Compose project and distinct PostgreSQL, Neo4j, MinIO, and
runner volumes for unauthenticated local testing. If a one-time transition is
unavoidable, make verified backups, set
`INLUMEN_ALLOW_AUTH_MODE_SWITCH=true` only for that deployment startup, and
run an explicit data migration that assigns the local workspace to one named
account. The override does not infer ownership or transfer browser-local
drafts, secrets, or provider keys.

### Browser state and account switching

Authenticated browser drafts, chat history/session IDs, generation-run IDs,
model configuration metadata, and selections
are namespaced by the server-resolved user ID and workspace ID. Logout invalidates
in-flight storage handles; account/workspace changes remount private UI and its
query cache. Do not use two tabs in one browser profile as independent Keycloak
login sessions; use separate profiles for simultaneous two-user testing.

Legacy, unscoped browser entries are left intact but are **not imported into an
authenticated account**, because their owner is unknown. Users may need to
re-enter their LLM API key once so it can be encrypted in their account's
workspace. Existing server-owned workspace data is not
deleted. Auth-disabled local mode continues to use the legacy keys. Browser
namespacing prevents accidental application-level mixing; it is not encryption
or protection against someone with access to the browser profile/DevTools.

For the graph regression/integration test (no LLM calls), run:

```sh
docker compose exec -T -e RUN_NEO4J_INTEGRATION=1 backend python -m unittest discover -s tests -p test_agent_workspace_queries.py
```

This executes the actual agent-generated queries and workspace validator against
Neo4j in two synthetic workspaces, then rolls back the entire transaction.

### Clock synchronization and authentication

Keep the application VM and Keycloak host synchronized using the host's NTP
service (for example, chrony or systemd-timesyncd). Containers inherit their
host/kernel clock; do not run an NTP daemon inside each application container.
On Linux, check `timedatectl status` and, when using chrony, `chronyc tracking`.
Monitor clock offset and synchronization failures on both hosts. Docker Desktop
also depends on its Linux VM clock; check it after the machine resumes from sleep.

`KEYCLOAK_CLOCK_SKEW_SECONDS` defaults to 5 and accepts integers from 0 through
60. Invalid values fail startup. This small PyJWT leeway applies to `iat`,
`nbf`, and `exp` (including at most that many extra seconds after expiry);
signature, issuer, and audience validation remain enabled. It is not a remedy
for sustained clock drift. Keep the default unless measured operational needs
justify a different bounded value; fix host time synchronization first.

A 401 with code `token_not_yet_valid` indicates token timing outside this
tolerance. The frontend reports this separately from Keycloak login failures.
Never log bearer tokens or paste them into third-party JWT debugging sites.
See the [PyJWT leeway documentation](https://pyjwt.readthedocs.io/en/stable/usage.html#expiration-time-claim-exp).

Back up the PostgreSQL, Neo4j, MinIO, runner-artifact, and model-store volumes.
Test restores regularly. Rotate the two internal service keys and the MinIO,
Neo4j, PostgreSQL, and node-secret encryption credentials under a planned
maintenance window. Losing `INLUMEN_SECRET_ENCRYPTION_KEY` makes stored node
secrets unreadable.

Generated-code validation currently needs the Docker socket. On a single VM,
treat the codegen container as privileged infrastructure: restrict VM access,
never expose the service, and enforce resource limits. For a higher assurance
deployment, move codegen and pipeline execution to a dedicated worker VM with
a rootless container runtime; the workspace protocol and PostgreSQL schema stay
the same.

Before upgrades, run the full regression suite and `npm audit`. The current
React Router 6 line retains two moderate upstream advisories that require a
breaking React Router 7 migration; the server-rendering issue does not apply to
this client-only SPA, but the migration should be tracked.
