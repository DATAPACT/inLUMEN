# Deploy this branch and test concurrent pipeline design

Use `codex/multi-user-production-architecture` throughout. No merge into `main`
is needed. Use a dedicated staging VM and synthetic test accounts/data for the
first run. The load generator runs on a separate machine, so its browsers do not
consume the application VM's CPU and memory.

## 1. Deploy the branch

Install Docker Engine, its Compose plugin and Git on the VM, then:

```sh
git clone --single-branch --branch codex/multi-user-production-architecture https://github.com/DATAPACT/inLUMEN.git
cd inLUMEN
git rev-parse HEAD
cp .env.production.example .env.production
chmod 600 .env.production
```

Record the commit SHA and VM CPU/RAM for each experiment. Fill in the production
env file using [the production deployment guide](production-multi-user.md).
Keep the encryption key stable across restarts and back it up with the database.
The LLM provider/key is configured through the application admin UI, not this file.

Reuse the existing **inlumen** Keycloak realm and its public PKCE client. Add the
VM application's HTTPS URL to the client's redirect URIs (`https://HOST/*`) and
web origins (`https://HOST`), preserving existing entries. Keycloak's issuer must
be reachable from both browsers and the VM. Assign `inlumen-admin` only to your
application administrator; the username `admin` alone does not grant permission.

Create a Cloudflare Tunnel public hostname pointing to `http://inlumen-frontend:8080`
and set its token in `.env.production`. The Compose tunnel container joins the
application network. No database or application origin ports need publishing.
If Cloudflare Access protects the hostname, the load browsers also need an
approved Access login; this runner automates the Keycloak form only.

For the initial 20-user chat experiment, consider setting:

```dotenv
INLUMEN_WEB_WORKERS=2
INLUMEN_WEB_THREADS=16
```

The unchanged defaults are 2 workers × 4 threads: eight simultaneous request
handlers, which can queue 20 synchronous chats. More threads are an experiment,
not a capacity guarantee: watch memory, database connections and LLM rate limits.
Codegen/runner queue limits are separate and do not limit pipeline-design chats.

```sh
docker compose --env-file .env.production -f docker-compose-prod.yml config --quiet
docker compose --env-file .env.production -f docker-compose-prod.yml up -d --build
docker compose --env-file .env.production -f docker-compose-prod.yml ps
docker compose --env-file .env.production -f docker-compose-prod.yml logs migrate backend
```

Log in once as administrator, open **Settings → Manage shared LLM**, save the
provider/model/key, and enable sharing. Verify one ordinary user can select
**Application-provided LLM** and design a pipeline manually.

For subsequent branch updates, use `git pull --ff-only` in the clean VM checkout,
record the new SHA, and repeat the Compose build/start. Back up persistent data
before updates; do not run `down -v` against data you want to retain.

## 2. Prepare the load generator and test users

Check out the same branch on a separate machine. From its `frontend` directory:

```sh
npm ci
npx playwright install chromium
npm run stress:users -- --count 20
```

Use Node 22.12+ and install Playwright's OS dependencies on Linux if requested
(`npx playwright install --with-deps chromium`). The generator writes:

- `loadtest/accounts.local.json`: usernames/passwords read by the runner.
- `loadtest/keycloak-users.local.json`: Keycloak partial-import payload.

Both contain secrets, are Git-ignored at these default locations, and are created
with owner-only permissions. Existing files are never overwritten. Custom output
paths must also be kept out of Git. Do not attach these files to reports.

In the Keycloak admin console, select the **existing inlumen realm**, choose
**Partial import**, upload `keycloak-users.local.json`, select users and choose
**Fail** if a resource exists. Do not replace the realm or overwrite real users.
The generated accounts have completed names, synthetic `example.invalid` emails,
permanent random passwords and no requested admin roles. They are intended only
for tests and cannot receive email. If your realm requires real verified email,
MFA or other actions, provision compliant dedicated accounts first; do not weaken
normal-user policies. The automated login expects the standard Keycloak username
and password form with no outstanding profile/password/MFA steps. Existing test
accounts can instead be supplied as a JSON array of `{ "username": "...",
"password": "..." }` through `--accounts PATH`.

Authentication uses the normal browser login; enabling password/direct grants
or creating a new realm is unnecessary. See [Keycloak authentication flows](https://www.keycloak.org/securing-apps/oidc-layers).

## 3. Validate without LLM calls, then increase load

Replace the example URLs below. Preflight logs in two distinct users, selects their default workspaces and the
shared LLM, and verifies cross-user workspace reads are denied. It does not clear
workspace content or send chat prompts.

```sh
npm run stress -- --url https://inlumen.example.com --issuer https://identity.example.com/realms/inlumen --users 2 --preflight
npm run stress -- --url https://inlumen.example.com --issuer https://identity.example.com/realms/inlumen --users 1
npm run stress -- --url https://inlumen.example.com --issuer https://identity.example.com/realms/inlumen --users 5
npm run stress -- --url https://inlumen.example.com --issuer https://identity.example.com/realms/inlumen --users 10
npm run stress -- --url https://inlumen.example.com --issuer https://identity.example.com/realms/inlumen --users 20 --rounds 3
```

Each user has an isolated [Playwright browser context](https://playwright.dev/docs/browser-contexts)
and a separate Keycloak identity. All users prepare first, then click Send together.
**Before every live round, the runner clicks Clear all in each participating
user’s default workspace. This deletes pipelines, files, runs, outputs, chat,
provenance, node secrets and generation history there.** Use only accounts whose
workspace content you intend to erase. No new workspaces are created. After the
run, log in normally as that user to see the final round’s pipeline. If the final
design fails validation and is rolled back, the canvas can be empty. Earlier
rounds are overwritten, and older test workspaces from previous runner versions
are left untouched. The default prompt requests a connected CSV
transformation pipeline, without code generation or execution. `--prompt-file`
customizes it; avoid execution requests or sensitive data in the first experiment.
`--ramp-seconds 30` spreads submissions over 30 seconds instead of a simultaneous
burst. `--headed` helps diagnose login/UI setup. `--timeout-seconds` defaults to180.

This is a real paid workload. N × rounds counts design scenarios, **not** provider
calls: the agent can make several LLM calls per scenario. Set a suitable provider
budget and quota before running. The runner stops subsequent rounds after a
failure and does not replay failed chat requests. A timed-out request can still
be running on the server; inspect it before starting another test.

## 4. Read results and decide capacity

Each run writes `frontend/loadtest/results/<run-id>/report.json` and exits nonzero
on failure. Reports contain success/failure counts, successful-scenario p50/p95/max
latency, observed overlapping chat requests, HTTP status/request IDs where present,
and the default workspace IDs used. Passwords, tokens, prompts and raw server errors are
not saved. The load-generator SHA is recorded; record the deployed server SHA
separately. Workspace IDs/user IDs in reports are operational metadata.

Latency covers sending the prompt through canvas display and persisted graph
verification. Login and workspace setup are excluded. A passing scenario requires
a successful response, a passed graph guardrail, a nonempty graph with valid edges,
visible canvas nodes and a persisted graph. This checks basic functionality, not
the semantic correctness of the designed pipeline. Cross-workspace denial is a
smoke check, not a comprehensive isolation/security test.

Observe the VM alongside the test:

```sh
docker stats
docker compose --env-file .env.production -f docker-compose-prod.yml logs --since 10m backend
```

Record CPU, memory, database pressure and provider token/rate-limit metrics.
Evaluate failures alongside latency: reported percentiles include successes only.
Choose your acceptable response-time target before judging results. Repeat short
runs before longer soak tests; a single 20-user burst is not proof of sustained
production capacity. Browser-generator CPU saturation can also skew results.

Cloudflare's default proxy read timeout is **125 seconds**, so a synchronous chat
may receive HTTP524 even though Nginx/Gunicorn allow 1800 seconds. Increasing the
runner timeout or Gunicorn timeout cannot fix that boundary. If observed, the
application needs background chat jobs with polling/streaming designed for the
proxy, or a suitable Cloudflare plan/configuration. See [Cloudflare error524](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/error-524/).

Final results remain in each user’s default workspace for inspection. Delete only
identified test data through supported administration, or reset a dedicated
throwaway staging deployment after collecting results. Disable/delete the test
Keycloak accounts when testing is finished. Do not delete shared production volumes.

## Offline harness verification

```sh
cd frontend
npm run test:stress
```

These tests use local HTTP fixtures and Chromium to exercise concurrent isolated
sessions, workspace denial, repeated rounds, preflight and HTTP524 handling.
They incur no LLM charges and do not establish real VM/Keycloak capacity.

## Shared Cloudflare connector (single application Compose file)

Cloudflare runs as separate shared infrastructure. Start that stack before
inLUMEN so its Docker network exists, then set the actual network name in
`.env.production`:

```dotenv
INLUMEN_TUNNEL_NETWORK=cloudflare_default
```

Route the hostname to `http://inlumen-frontend:8080`. The inLUMEN stack contains
no connector and needs no tunnel token, profile, or external-network toggle.
Remove obsolete `COMPOSE_PROFILES=standalone-tunnel`,
`CLOUDFLARE_TUNNEL_TOKEN`, `CLOUDFLARED_IMAGE`, and
`INLUMEN_TUNNEL_NETWORK_EXTERNAL` entries from inLUMEN's environment file when
upgrading. Keep the tunnel token in the shared Cloudflare stack's environment.

Only the frontend joins the external tunnel network; application services use
the private network. `INLUMEN_PRIVATE_NETWORK` can preserve its existing name.
Always use `docker compose --env-file .env.production -f docker-compose-prod.yml`.
No Compose override is required. No application host ports are published.
