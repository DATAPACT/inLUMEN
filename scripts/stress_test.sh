#!/usr/bin/env bash
# Run the existing Playwright stress scenario from a separate load-generator host.
set -euo pipefail
umask 077

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CALLER_DIR="$PWD"

usage() {
  cat <<'HELP'
Usage: bash scripts/stress-test.sh [options]

Targets the deployed inLUMEN application by default. Live tests send paid LLM
requests and clear the selected test workspaces. Isolated workspaces protect
participants' default workspaces. Run from your Mac or another load generator.

  --url URL                 App origin (default: https://inlumen-ui.zooverse.dev)
  --issuer URL              Keycloak realm (default: https://keycloak.zooverse.dev/realms/inlumen)
  --accounts FILE           JSON account roster (default: private participant roster)
  --users N                 Concurrent users (default: 39)
  --scenario NAME           audio-session (default) or design
  --code-zip FILE            Required for a live audio session
  --audio-file FILE          Required for a live audio session
  --rounds N                Rounds (default: 1)
  --workspace-mode MODE     isolated (default) or default (matches normal login; clears it)
  --synchronized-run        Submit execution runs together (default for audio)
  --no-synchronized-run     Submit each run as soon as its user is ready
  --ramp-seconds N          Chat ramp interval (default: 0)
  --timeout-seconds N       Design timeout (default: 240)
  --run-timeout-seconds N   Execution timeout including queue wait (default: 1800)
  --review-ai-changes BOOL  true or false (default: false)
  --max-run-cpus N          Per-run CPU allocation ceiling (default: 2)
  --max-run-memory-gib N    Per-run RAM allocation ceiling (default: 4)
  --output DIR             Report parent (default: state/stress-tests)
  --ssh-host USER@HOST      VM SSH target for cache preparation/monitoring
  --warm-models-script PATH Existing VM Python cache preparer; requires --ssh-host
  --monitor-vm             Save vmstat samples; requires --ssh-host and vmstat
  --preflight              Login/access checks only; defaults to existing workspaces
  --headed                 Show browser windows
  --dry-run                Print the command without accessing the deployment
  --help                   Show this help

Relative file paths are resolved from the directory where you invoke this script.
Credentials are read from the account file, never supplied on the command line.
Cache preparation is skipped during preflight. VM worker limits are not changed.
Default mode verifies that the test workspace matches normal login before clearing.
Open tabs in the same user/workspace refresh saved chat, canvas and run history.
HELP
}

fail() { printf 'Stress test: %s\n' "$*" >&2; exit 2; }
absolute_path() {
  case "$1" in /*) printf '%s' "$1";; *) printf '%s/%s' "$CALLER_DIR" "$1";; esac
}

url='https://inlumen-ui.zooverse.dev'
issuer='https://keycloak.zooverse.dev/realms/inlumen'
accounts="$ROOT/frontend/loadtest/accounts.participants.local.json"
users=39 scenario=audio-session code_zip='' audio_file='' rounds=1
workspace_mode=isolated workspace_explicit=false synchronized=auto
ramp=0 timeout=240 run_timeout=1800 review=false max_cpus=2 max_memory=4
output="$ROOT/state/stress-tests" ssh_host='' warm_script=''
monitor=false preflight=false headed=false dry_run=false

while (($#)); do
  case "$1" in
    --help|-h) usage; exit 0;;
    --preflight) preflight=true; shift;;
    --headed) headed=true; shift;;
    --dry-run) dry_run=true; shift;;
    --monitor-vm) monitor=true; shift;;
    --synchronized-run) synchronized=true; shift;;
    --no-synchronized-run) synchronized=false; shift;;
    --url|--issuer|--accounts|--users|--scenario|--code-zip|--audio-file|--rounds|--workspace-mode|--ramp-seconds|--timeout-seconds|--run-timeout-seconds|--review-ai-changes|--max-run-cpus|--max-run-memory-gib|--output|--ssh-host|--warm-models-script)
      (($# >= 2)) && [[ -n "$2" && "$2" != --* ]] || fail "Missing value for $1"
      case "$1" in
        --url) url="$2";; --issuer) issuer="$2";; --accounts) accounts="$(absolute_path "$2")";;
        --users) users="$2";; --scenario) scenario="$2";;
        --code-zip) code_zip="$(absolute_path "$2")";; --audio-file) audio_file="$(absolute_path "$2")";;
        --rounds) rounds="$2";; --workspace-mode) workspace_mode="$2"; workspace_explicit=true;;
        --ramp-seconds) ramp="$2";; --timeout-seconds) timeout="$2";; --run-timeout-seconds) run_timeout="$2";;
        --review-ai-changes) review="$2";; --max-run-cpus) max_cpus="$2";; --max-run-memory-gib) max_memory="$2";;
        --output) output="$(absolute_path "$2")";; --ssh-host) ssh_host="$2";; --warm-models-script) warm_script="$2";;
      esac
      shift 2;;
    *) fail "Unknown option: $1 (see --help)";;
  esac
done

for endpoint in "$url" "$issuer"; do
  [[ "$endpoint" == https://* || "$endpoint" == http://* ]] || fail 'URLs must use HTTP or HTTPS'
  [[ "$endpoint" != *[@?#]* ]] || fail 'URLs must not include credentials, query strings or fragments'
done
[[ "$scenario" == audio-session || "$scenario" == design ]] || fail 'Invalid scenario'
[[ "$workspace_mode" == isolated || "$workspace_mode" == default ]] || fail 'Invalid workspace mode'
[[ "$review" == true || "$review" == false ]] || fail 'Review AI changes must be true or false'
for value in "$users" "$rounds" "$timeout" "$run_timeout" "$max_cpus" "$max_memory"; do
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || fail 'Counts, timeouts and allocation limits must be positive integers'
done
[[ "$ramp" =~ ^(0|[1-9][0-9]*)$ ]] || fail 'Ramp seconds must be a nonnegative integer'
if [[ "$preflight" == true && "$workspace_explicit" == false ]]; then workspace_mode=default; fi
if [[ "$synchronized" == auto ]]; then
  synchronized=false
  if [[ "$scenario" == audio-session ]]; then synchronized=true; fi
fi
[[ "$synchronized" != true || "$scenario" == audio-session ]] || fail 'Synchronized runs require audio-session'
if [[ -n "$ssh_host" ]]; then
  [[ "$ssh_host" =~ ^[A-Za-z0-9_][A-Za-z0-9_.@-]*$ ]] || fail 'Invalid SSH host (SSH aliases are supported)'
fi
if [[ -n "$warm_script" ]]; then
  [[ -n "$ssh_host" ]] || fail '--warm-models-script requires --ssh-host'
  [[ "$warm_script" =~ ^/([A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+$ ]] || fail 'Cache preparer must be an absolute VM path without shell syntax'
fi
[[ "$monitor" != true || -n "$ssh_host" ]] || fail '--monitor-vm requires --ssh-host'
[[ -r "$accounts" ]] || fail "Account file is not readable: $accounts"
if [[ "$scenario" == audio-session && "$preflight" == false ]]; then
  [[ -n "$code_zip" && -r "$code_zip" ]] || fail 'Provide a readable --code-zip file'
  [[ -n "$audio_file" && -r "$audio_file" ]] || fail 'Provide a readable --audio-file file'
fi
command -v node >/dev/null || fail 'Node.js is required'
if [[ "$dry_run" == false && ( "$monitor" == true || ( -n "$warm_script" && "$preflight" == false ) ) ]]; then
  command -v ssh >/dev/null || fail 'SSH is required for the selected VM options'
fi

args=("$ROOT/frontend/loadtest/run.mjs" --url "$url" --issuer "$issuer" --accounts "$accounts"
  --users "$users" --rounds "$rounds" --scenario "$scenario" --workspace-mode "$workspace_mode"
  --ramp-seconds "$ramp" --timeout-seconds "$timeout" --run-timeout-seconds "$run_timeout"
  --review-ai-changes "$review" --max-run-cpus "$max_cpus" --max-run-memory-gib "$max_memory")
if [[ -n "$code_zip" ]]; then args+=(--code-zip "$code_zip"); fi
if [[ -n "$audio_file" ]]; then args+=(--audio-file "$audio_file"); fi
if [[ "$synchronized" == true ]]; then args+=(--synchronized-run); fi
if [[ "$preflight" == true ]]; then args+=(--preflight); fi
if [[ "$headed" == true ]]; then args+=(--headed); fi
if [[ -n "$warm_script" && "$preflight" == false ]]; then
  args+=(--warm-models-host "$ssh_host" --warm-models-script "$warm_script")
fi

if [[ "$dry_run" == true ]]; then
  printf 'Target: %s\n' "$url"
  printf 'Command: '; printf '%q ' node "${args[@]}" --output "$output"; printf '\n'
  if [[ "$monitor" == true ]]; then printf 'VM monitoring: %s (vmstat every 5 seconds)\n' "$ssh_host"; fi
  exit 0
fi

mkdir -p -- "$output"
run_dir="$(mktemp -d "$output/stress-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")"
args+=(--output "$run_dir")
monitor_pid=''
cleanup() {
  if [[ -n "$monitor_pid" ]]; then
    kill "$monitor_pid" 2>/dev/null || true
    wait "$monitor_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

printf 'Target: %s\nUsers: %s; workspace mode: %s\nReports and logs: %s\n' "$url" "$users" "$workspace_mode" "$run_dir"
if [[ "$monitor" == true ]]; then
  ssh -n -o BatchMode=yes -o ConnectTimeout=15 "$ssh_host" 'command -v vmstat >/dev/null' || fail 'Cannot start VM monitoring'
  ssh -n -o BatchMode=yes -o ConnectTimeout=15 "$ssh_host" 'LC_ALL=C vmstat -w 5' >"$run_dir/vmstat.log" 2>"$run_dir/monitor-errors.log" &
  monitor_pid=$!
fi
if [[ "$scenario" == audio-session && "$preflight" == false && -z "$warm_script" ]]; then
  printf 'Model caches are not prepared by this command; cold workspaces may need downloads and additional disk space.\n'
fi
cd -- "$ROOT"
node "${args[@]}" 2>&1 | tee "$run_dir/stress.log"
if [[ -n "$monitor_pid" ]] && ! kill -0 "$monitor_pid" 2>/dev/null; then
  printf 'VM monitor stopped early; inspect %s/monitor-errors.log\n' "$run_dir" >&2
  exit 1
fi
