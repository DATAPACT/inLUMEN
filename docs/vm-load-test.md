# Deploy a candidate and test concurrent pipeline design and execution

Select an exact candidate commit or release tag from the repository. Use a
dedicated staging VM and synthetic test accounts/data for the first run.
The load generator runs on a separate machine, so its browsers do not
consume the application VM's CPU and memory.

## 1. Deploy the candidate

Install Docker Engine, its Compose plugin and Git on the VM. Replace
`CANDIDATE_REF` below with the selected commit SHA or release tag:

```sh
git clone https://github.com/DATAPACT/inLUMEN.git
cd inLUMEN
git checkout --detach CANDIDATE_REF
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

Start the separate shared Cloudflare connector stack and set
`INLUMEN_TUNNEL_NETWORK` in `.env.production` to its existing Docker network
(for example, `cloudflare_default`). Configure the tunnel's public hostname to
route to `http://inlumen-frontend:8080`. The application frontend joins that
network; the inLUMEN Compose file does not run a connector or accept a tunnel
token. Keep the token in the shared connector stack. No database or application
origin ports need publishing. See the
[shared ingress instructions](production-multi-user.md#shared-cloudflare-connector-single-application-compose-file).
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

### Bounded execution budget

For an 8-vCPU/30-GiB VM that also hosts Keycloak and other applications,
Production Compose supplies these configurable execution defaults:

```dotenv
RUNNER_MAX_OUTSTANDING_RUNS=4
RUNNER_MAX_GLOBAL_OUTSTANDING_RUNS=50
CODEGEN_EXECUTION_MAX_ACTIVE_RUNS=2
CODEGEN_EXECUTION_CPU_BUDGET=4
CODEGEN_EXECUTION_MEMORY_GIB=8
CODEGEN_ML_CPU_THREADS=2
RUNNER_DAGSTER_TIMEOUT_SECONDS=1800
```

Up to 50 runs can be accepted across workspaces; only two execution jobs enter
image building, model preparation or execution at once. Each ML worker receives
2 CPUs and 4 GiB. The FIFO controller also checks the shared 4-CPU/8-GiB budget,
clamped to Docker's detected host capacity after its existing reserve. Runtime
containers use CPU quotas and memory limits. SDK build containers use memory
limits and a restricted CPU set; common dependency layers are cached.
Inference thread counts follow the allocation. The remaining host capacity is
available to chats and other services; this is a fixed budget, not a measurement
of free RAM or a promise that unrelated workloads will fit.

Keep **one codegen replica with one Uvicorn process**. Resource admission is
process-local; adding replicas/processes multiplies the execution budget.
Runner outstanding-run limits use the shared database. A run's 1800-second
deadline includes capacity waiting, model preparation and execution. Additional
submissions beyond the global queue limit receive 429. Generation queue limits
are separate; importing this provided ZIP makes no code-generation LLM calls.
Before admission, the backend freezes each run's uploaded audio and code into
its executable snapshot. That preparation is limited to one request per backend
process (two with the production worker count), so 20 simultaneous submissions
do not all stage and encode WAV files in backend memory at once.

These settings are in the candidate checkout and take effect after its images
are built and deployed. Existing containers do not acquire the new limits by
editing `.env.production` alone. Record the deployed commit, effective container
environment and VM resource usage before certifying readiness.

```sh
docker compose --env-file .env.production -f docker-compose-prod.yml config --quiet
docker compose --env-file .env.production -f docker-compose-prod.yml up -d --build
docker compose --env-file .env.production -f docker-compose-prod.yml ps
docker compose --env-file .env.production -f docker-compose-prod.yml logs migrate backend
```

Log in once as administrator, open **Settings → Manage shared LLM**, save the
provider/model/key, and enable sharing. Verify one ordinary user can select
**Application-provided LLM** and design a pipeline manually.

For a subsequent candidate, run `git fetch origin --tags` in the clean VM
checkout, then `git checkout --detach CANDIDATE_REF` with the new commit or tag.
Record the SHA and repeat the Compose build/start. Back up persistent data
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
npm run stress -- --url https://inlumen.example.com --issuer https://identity.example.com/realms/inlumen --users 1 --scenario audio-session --code-zip /path/pipeline-code.zip --audio-file /path/recording.wav
npm run stress -- --url https://inlumen.example.com --issuer https://identity.example.com/realms/inlumen --users 5 --scenario audio-session --code-zip /path/pipeline-code.zip --audio-file /path/recording.wav
npm run stress -- --url https://inlumen.example.com --issuer https://identity.example.com/realms/inlumen --users 10 --scenario audio-session --code-zip /path/pipeline-code.zip --audio-file /path/recording.wav
npm run stress -- --url https://inlumen.example.com --issuer https://identity.example.com/realms/inlumen --users 20 --scenario audio-session --code-zip /path/pipeline-code.zip --audio-file /path/recording.wav
```

Each user has an isolated [Playwright browser context](https://playwright.dev/docs/browser-contexts)
and a separate Keycloak identity. All users prepare first, then click Send together.
**Before every live round, the runner clicks Clear all in each participating
user’s default workspace. This deletes pipelines, files, runs, outputs, chat,
provenance, node secrets and generation history there.** Use only accounts whose
workspace content you intend to erase. With the default workspace mode, no new workspaces are created; use `--workspace-mode isolated` for participant rehearsals. After the
run, log in normally as that user to see the final round’s pipeline. If the final
design fails validation and is rolled back, the canvas can be empty. Earlier
rounds are overwritten, and older test workspaces from previous runner versions
are left untouched. The default `design` scenario sends a single CSV pipeline
design request and supports a custom `--prompt-file`. The optional `audio-session`
scenario exercises design, extension, package import and execution with two messages:

1. Create a pipeline that transcribes an uploaded audio recording, analyzes its sentiment, and outputs the results.
2. Extend the pipeline by adding named entity recognition after transcription, followed by data anonymization before sentiment analysis.

Provide the code ZIP and WAV through `--code-zip` and `--audio-file`; workload
assets and generated results are local inputs, not repository dependencies.
The ZIP must contain four portable Task folders publishing `transcription.json`,
`entities.json`, `anonymized.json` and `sentiment.json`. Each Task should read its
single input through `INLUMEN_INPUT_MANIFEST` without embedding graph IDs or
assuming fixed port names. See [the Task package contract](task-packages.md).
There is a barrier between the two design requests. The harness verifies both
graphs' roles and connections, reviews and applies any proposed graph through
**Apply to canvas**, then verifies the saved graph. It opens **Upload code ZIP**, explicitly matches
the four packages to each user's generated node IDs, reviews and imports the
same bytes, attaches the same WAV to the Source, then clicks **Run current pipeline**.
Polling queued work does not resubmit it. This makes 40 design requests and 20
pipeline runs for a 20-user round; the agent may make multiple provider calls
for each design request. Keep the WAV duration fixed and rehearse cold caches
as well as warm runs: models are cached separately per workspace, so warming
one participant does not warm everyone's model cache.

`--run-timeout-seconds` defaults to 1800 and bounds waiting for a submitted run.
An unknown submission or polling outcome stops further rounds and leaves
potentially active jobs for inspection. A 429 is reported as a failure without
retrying. `--scenario design` retains the previous single CSV design request;
only that scenario accepts `--prompt-file`.
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
latency, stage p50/p95 times, observed overlapping chat requests, HTTP status/request IDs,
run IDs, observed queue duration, allocation samples and the workspace IDs used.
Reports also record both shared file SHA256 hashes. Passwords, tokens, prompts,
transcripts, audio contents and raw server errors are
not saved. The load-generator SHA is recorded; record the deployed server SHA
separately. Workspace IDs/user IDs in reports are operational metadata.

Session latency covers both designs, import, upload and completed execution;
login and workspace setup are excluded. Per-stage timings separate design latency
from execution waiting. The audio scenario requires the intended graph order,
successful four-Task import, successful execution, all four JSON outputs and
matching text handoffs through NER, anonymization and sentiment. Original nested
transcription fields must be absent from the anonymized output. Passing also
requires observed ML allocations within the configured per-run limits; missing
allocation metadata fails the check. The defaults are 2 CPUs/4 GiB, matching the
production ML settings. Use `--max-run-cpus` and `--max-run-memory-gib` when testing
a deployment with different settings. These flags set test expectations; they do
not change server capacity. The selected limits are recorded in the report.
Aggregate concurrency is verified separately with admission tests and live VM
resource monitoring; per-user polling is not a simultaneous host-resource sample.
This does not evaluate model accuracy or prove complete personal-data removal. The legacy
design scenario retains its structural graph checks. Cross-workspace denial is a
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

These tests use local HTTP fixtures and Chromium to exercise a full 20-user
two-message session, distinct IDs/ports, identical uploads, queued runs and
artifact verification, plus failures in the second chat, package validation,
execution and artifact delivery. They also cover workspace denial, repeated
design-only rounds, preflight and HTTP524 handling. Codegen admission tests submit
20 threaded jobs against the VM budget and verify peak resource use.
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

To rehearse direct application without the Review AI dialog, pass `--review-ai-changes false`. The runner sets the switch in each browser and verifies both chat requests use that preference. Omit the option to retain the deployment/browser preference. Set `VITE_REVIEW_AI_CHANGES_DEFAULT=false` in the VM’s private `.env.production` and rebuild the frontend to start session browsers with review off. A changed deployment default applies once to existing browsers too; subsequent user choices are retained until that default changes again.

## Participant rehearsals and bulk reset

### Bash launcher

Run `bash scripts/stress-test.sh --help` from the repository root for all options.
The launcher defaults to the deployed production app and realm, 39 users from
the private participant roster, isolated rehearsal workspaces, one audio-session
round, synchronized execution submissions, and Review AI off. File paths are
resolved against the caller's working directory, so the launcher also works
when invoked through an absolute path. Run it on a separate load-generator
machine with Node.js, frontend dependencies and Playwright Chromium installed.

```sh
bash scripts/stress-test.sh --users 2 --preflight
bash scripts/stress-test.sh --users 39 \
  --accounts frontend/loadtest/accounts.participants.local.json \
  --code-zip /path/pipeline-code.zip --audio-file /path/recording.wav \
  --ssh-host operator@vm --warm-models-script /path/on/vm/warm-models.py \
  --monitor-vm
```

`--url` and `--issuer` override the deployment. `--users`, `--accounts`,
`--rounds`, `--scenario`, `--no-synchronized-run`, timeouts and allocation
ceilings control the workload. The launcher does not alter VM worker limits.
For the original disposable accounts, set `--users 20 --accounts
frontend/loadtest/accounts.local.json`; isolated workspaces still remain the
default. Choose `--workspace-mode default` only when their default workspace
contents may be erased. Preflight defaults to existing workspaces and does not
prepare caches, clear workspaces or send LLM requests. Explicit isolated
preflight creates workspaces. `--dry-run` validates local inputs and prints
the command without contacting the deployment or creating reports.

To leave the stress-test pipeline and runs in the exact workspaces opened by
normal participant login, add **`--workspace-mode default`**. This clears those
participants' default workspace contents before each live round; isolated mode
remains the safe default for rehearsals. The runner verifies the headerless
`/api/session` workspace and user identity before clearing. Each round logs
the username, test workspace ID and normal-login workspace ID; the report's
`workspaces` entries retain `username`, `workspace_id` and `login_workspace_id`.
In default mode the two IDs match. Compare this ID with **Current workspace**
under the signed-in user's account menu. Reload an existing tab once after
upgrading the application, then keep it open as an observer: saved chat and
canvas changes refresh about every three seconds, and the Run panel discovers
new workspace runs about every five seconds. Hidden tabs poll every fifteen
seconds and refresh when shown. Select Run to observe execution/queue progress.
With `--headed`, you can also watch the actual automated browser.

Every live invocation uses a new private directory beneath `state/stress-tests`
(override with `--output`). It contains `stress.log` and a nested
`loadtest-*/report.json`; the test's exit status propagates to Bash. With
`--monitor-vm`, `vmstat.log` records CPU, free memory and swap activity every
five seconds, and `monitor-errors.log` captures SSH errors. The first vmstat
CPU row is the average since boot; subsequent rows describe sample intervals.
Monitoring is stopped when the launcher exits. These samples alone do not
establish output correctness, OOM events or container restart counts.

Fresh isolated workspaces have separate model stores. `--warm-models-script`
invokes an **existing operator-provided Python preparer on the selected VM**,
passing workspace mappings as JSON on stdin before workspace preparation each
round. Both the SSH host and absolute preparer path are required; SSH must work
without interactive authentication. The launcher does not install that script
or copy model weights. The meeting VM already has its session cache preparer.
Without it, cold workspaces may download models and use considerably more disk
space; check VM disk capacity before a large cold-cache rehearsal. Keep cache
preparers, participant identities and session assets private. The reusable CLI
also exposes `--warm-models-host` and `--warm-models-script` for this hook.

Keep the original 20 disposable accounts and the meeting roster in separate,
private `loadtest/accounts*.local.json` files. Run the original 20-user scenario
as before. For participant accounts, use **isolated** workspaces so rehearsals
leave their session workspaces and files untouched:

```sh
npm run stress -- --url https://inlumen.example.com --issuer https://identity.example.com/realms/inlumen --accounts loadtest/accounts.participants.local.json --users 39 --workspace-mode isolated --scenario audio-session --code-zip /path/pipeline-code.zip --audio-file /path/recording.wav --review-ai-changes false
```

Add `--synchronized-run` for a second experiment: each successfully prepared
browser waits until all other successful browsers have uploaded the ZIP and
WAV, then they click Run together. Failed preparations release their barrier
slot; failures remain failures in the report. Each report identifies every
rehearsal workspace. Isolated preflight also creates rehearsal workspaces;
normal preflight remains read-only.

The production queue defaults to 50 outstanding executions globally and four
per workspace. The codegen pending limit also defaults to 50. Keep two active
jobs (4 CPU / 8 GiB combined) as the baseline. Compare with three active jobs,
6 CPU / 12 GiB combined, on the actual VM before selecting three. Keep two CPU
threads per ML job. Include CPU, available memory, swap, OOM kills, container
restarts, API latency, output correctness, waiting time, and execution time in
that comparison. Do not assume additional concurrency reduces completion time.

The Run panel shows anonymous shared active/queued counts and the selected run's
queue position. Queue admission is FIFO in the single codegen process; position
can change when another run is cancelled. Polling uses incremental events,
non-overlapping requests and a slower interval in hidden tabs. Delayed
observations are labelled; queue waiting and worker execution have separate
clocks. No finish-time estimate is shown without measured evidence.

To preview a reset of **all owned workspaces** belonging to a supplied roster:

```sh
npm run stress:reset -- --url https://inlumen.example.com --issuer https://identity.example.com/realms/inlumen --accounts loadtest/accounts.participants.local.json
```

Pause the session, wait for AI edits and uploads to finish, and close participants' inLUMEN tabs before applying. Add
`--apply` to perform that exact allowlisted reset. To include the original 20
accounts, supply a private, deduplicated combined account file or run the tool
once per account file. The utility authenticates each account, preflights the
entire ownership list, cancels active pipeline/generation work, waits for it to
stop, clears workspace content through the existing authenticated API, and
verifies an empty graph. It refuses non-owned memberships and stops on errors;
it does not roll back previously completed resets. Re-preview before retrying.

Reset removes graphs, uploaded code/data, outputs, provenance, run/generation
history, server-side chat sessions and node secrets. It retains Keycloak users,
passwords, workspace memberships, shared LLM configuration and model cache
volumes. The reset clears durable chat transcripts too; open tabs recover the
empty conversation on their next successful refresh. Browser-local settings and
explicitly saved transcript copies remain on those browsers. Reset does not delete
workspace memberships: rehearsal workspace shells remain listed. Never place
rosters, credentials, audio, ZIPs or raw reports in Git.

### Measured rehearsal on 1 October 2026

The 8-vCPU/30-GiB VM passed the original 20-account audio workflow with two
workers and the final 39-participant workflow with three workers. The 39 Run
clicks were synchronized. Both used the same provided ZIP and unchanged
196.16-second WAV, with Review AI off. All four Task outputs and downstream
artifact handoffs were verified for every successful workflow.

| Passed workflow | Time from first design request to last completed user | Mean user workflow | Mean worker job |
| --- | --- | --- | --- |
| 20 users, two workers | 7m 38s | 5m 5s | 35.4s |
| 39 users, three workers | 11m 19s | 7m 11s | 42.8s |

Including browser login, preparation and rehearsal model-cache provisioning,
the load-generator totals were 7m 56s and 13m 1s respectively. Participant
session workspaces were preserved; their model caches were prepared separately.

Earlier synchronized execution batches completed 37 jobs in 680.9 seconds
with two workers and 35 jobs in 487.3 seconds with three workers. Normalizing
by completed jobs gives approximately 32% more throughput with three workers.
This comparison is approximate: successful-user counts and run times differed.
Earlier design failures exposed a rejected scoped predecessor query and missing
steps in compound insertion requests; both have regression fixes. Two chat
HTTP 500 responses in the intermediate rehearsal did not recur in the final
39-user run; their original response bodies were unavailable for diagnosis.
The final run passed all 78 design requests, 39 ZIP imports, 39 audio uploads
and 39 executions, including one automatic compound-insertion repair.

For the final 39-user workload, sampled host CPU peaked at 93.4%, available
memory stayed above 12.01 GiB, swap usage increased by 422.75 MiB, and there
were no OOM kills or application restarts. The observed maximum was three
execution containers, each bounded to 2 CPUs and 4 GiB. Mean admission wait
was 4m 7s; the last execution finished about 9m 5s after the first was submitted.
Live UI checks verified anonymous workload counts, queue position, reload
recovery without another submission, and queued cancellation.

The tested VM was left with `CODEGEN_EXECUTION_MAX_ACTIVE_RUNS=3`,
`CODEGEN_EXECUTION_CPU_BUDGET=6` and `CODEGEN_EXECUTION_MEMORY_GIB=12`. Portable
Compose defaults remain two workers. Keep the measured VM limits configurable
and retain the single codegen process. Private rosters, audio, ZIPs and raw
reports are excluded from Git.


### Durable chat and observation across browsers

PostgreSQL stores visible chat messages separately from agent checkpoints. The
backend derives workspace and owner IDs from the verified session, selects one
active conversation per user/workspace, and assigns conversation/message UUIDs
and ordered message sequences. A supplied conversation ID cannot grant access
to another participant's messages. Separate users in a shared workspace retain
private conversations, while canvas and run history remain workspace resources.

The user message is committed before the model starts; the assistant reply,
request status and response receipt are committed before returning success.
Turn IDs prevent duplicate model calls when the same request is retried, and a
workspace lease prevents competing chat edits across workers. Cancellation is
stored centrally, checked every five seconds, and scoped to the same owner and
workspace. A worker heartbeat older than two minutes marks an interrupted turn
as failed rather than replaying a potentially paid request.

History reads use revision cursors, stable message IDs and pages of at most 100
messages. Older messages can be loaded from the chat panel. Failed polls preserve
visible history and back off; an unsaved local canvas draft blocks background
graph replacement. Reset refuses active chat turns before clearing workspace
stores. Production uses the existing database/backup policy; unauthenticated
local development uses `backend/state/conversations.sqlite3` (override with
`INLUMEN_CHAT_DB_PATH`). Existing browser-only transcripts are not imported.

Deploy the new backend migration before starting the upgraded backend/frontend.
The new transcript tables are additive; no participant workspace is reset by
this migration. This change supplies observation and recovery, not token-by-token
streaming or an asynchronous replacement for the synchronous chat endpoint.
