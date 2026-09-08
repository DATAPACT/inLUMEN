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
  that label. HTTP callers cannot submit raw Cypher; server-owned agent tools
  carry an in-process capability and parameterize literal values.
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
   hostname to `http://inlumen-frontend:8080`. Put its token in `.env.production`.
4. Start the stack:

   ```sh
   docker compose --env-file .env.production -f docker-compose-prod.yml up -d
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

## Application-provided LLM

Manage shared LLM access from an administrator account. No LLM environment
variables or container restarts are needed.

1. In your existing Keycloak realm, create a realm role named `inlumen-admin`.
2. Assign that role only to the account(s) that should administer this application.
   Do not make it a default role. Ensure the frontend client's role scope includes
   it and the access token carries it in `realm_access.roles`. Sign out and back
   in after assigning it. See [Keycloak role mappings](https://www.keycloak.org/docs/latest/server_admin/index.html#_role_mappings).
3. Open inLUMEN **Settings → Manage shared LLM**.
4. Choose the provider, chat model, coding model, and API key. Turn on
   **Enable for all users**, then **Save Configuration**.

The configuration appears as **Application-provided LLM** for authenticated users
in every authorized workspace. Users without an existing selection use it by
default. Personal configurations and workspace data remain separate.

Admins can edit the settings, replace the API key, or turn sharing off from the
same form. Leave the key field blank to retain its saved value. Changing the
provider URL requires entering a new key. Turning sharing off retains the saved
configuration so it can be enabled again later. Changes apply to new LLM requests;
a request already executing may finish with the settings it previously resolved.

The API checks the signed Keycloak realm role on every admin request. Workspace
ownership, username, email, and browser flags never confer this permission.
Role removal takes effect when existing access tokens expire or are replaced.
In unauthenticated local development only, the local user can administer shared
LLM settings; production requires Keycloak authentication.

Settings are persisted once in `application_llm_settings`. The key is encrypted
with the existing `INLUMEN_SECRET_ENCRYPTION_KEY` and is never returned to the
browser, including the admin form. Keep that existing infrastructure key stable
and backed up alongside PostgreSQL. The migration creates the settings table;
no new infrastructure secrets are required. Concurrent admin edits are protected
by a revision check; reopen the form if another admin saved first.

Clients reference `application-llm` as the credential ID. The gateway controls
the provider URL, models, routing, and the fixed 8192-token output cap for chat
and code generation. Client overrides cannot redirect the shared key. The
private codegen request carries the key in a header, excluded from stored job
snapshots. Disabled settings disappear from users' configuration lists when
refreshed, and stale requests are rejected.

Shared billing still requires provider capacity and spending limits for a paid
20-user stress test. Existing codegen/runner queue limits apply; per-user LLM
quotas and a global chat concurrency limit are separate features.

## Operations

### Auth-mode changes

`AUTH_ENABLED=false` is deliberately a single shared local identity, not a
multi-user mode. The gateway records the selected mode in PostgreSQL and
allows changes automatically only when `APP_ENV=development` (the default).
Other environments refuse to start if a deployment's setting changes without
an explicit migration override. Production always requires authentication. This prevents a browser or
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

Before upgrades, run the full regression suite, browser checks, and `npm audit`.
The frontend now uses React Router 7 and Vite 8 with a locked dependency tree.
Use Node 22.12+ (the pinned production Node image meets this requirement).

See [hardening and operations](hardening-and-operations.md) for the role policy,
revision conflicts, job recovery, image pins, evaluation suite, and backup/restore commands.

## Branch deployment and concurrent-user testing

See [the VM load-test guide](vm-load-test.md) for deploying the feature branch
without merging main, creating dedicated Keycloak test users, and running
automated concurrent pipeline-design scenarios.

## Existing Cloudflare connector (single Compose file)

The production file supports either its own connector or an existing one, with no
host port publishing. For a new connector keep `COMPOSE_PROFILES=standalone-tunnel`
from `.env.production.example` and set its tunnel token. For an existing connector,
set these in `.env.production` instead:

```dotenv
COMPOSE_PROFILES=
INLUMEN_TUNNEL_NETWORK=cloudflare_default
INLUMEN_TUNNEL_NETWORK_EXTERNAL=true
```

Use the existing connector's actual Docker network name. Route the hostname to
`http://inlumen-frontend:8080`. No token is needed in this file for an existing
connector. Only the frontend joins the tunnel network; application services use
the private network. `INLUMEN_PRIVATE_NETWORK` can preserve its existing name.
Always use `docker compose --env-file .env.production -f docker-compose-prod.yml`.
No Compose override is required. `expose` entries are container metadata, not
host port mappings; the file has no `ports` entries.

### Application image builds

Production Compose builds the backend, migration, runner, codegen, and frontend
images from this checkout by default. Plain `up` therefore does not try to pull
the local application image names from Docker Hub. Build layers remain cached.
Infrastructure images continue to use their configured registry images.

For deployment from prebuilt registry images, configure the `INLUMEN_*_IMAGE`
overrides and set `INLUMEN_APP_PULL_POLICY=always`.

Application containers have configurable memory/PID limits and bounded logs
(three 20 MB files per service). The frontend health check verifies Nginx's
`/healthz` endpoint; backend readiness remains a separate check. These limits
apply to the API/services, while isolated Dagster workers retain their own
resource allocation limits. Set `INLUMEN_BACKEND_MEM_LIMIT`,
`INLUMEN_RUNNER_MEM_LIMIT`, `INLUMEN_CODEGEN_MEM_LIMIT`, and
`INLUMEN_FRONTEND_MEM_LIMIT` in `.env.production` when workload measurements
justify different budgets.
