"""Helper files that make an exported Argo bundle self-contained.

inLUMEN's responsibility ends when the bundle is produced. These files let the
deploying organisation build the shared image, upload the root inputs (or copy
them into the image), create the cluster objects and submit the workflow with
its own tools. Nothing here contacts a registry, a cluster or the network at
export time.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

ARTIFACT_REPOSITORY_CONFIGMAP = "inlumen-artifact-repositories"
ARTIFACT_REPOSITORY_KEY = "minio"
RUNTIME_SECRET_NAME = "inlumen-runtime-secrets"
SERVICE_ACCOUNT_NAME = "inlumen-workflow"
IMAGE_NAME = "inlumen-pipeline"
MODEL_PREFETCH_TASK = "inlumen-model-prefetch"
MODEL_STORE_VOLUME = "model-store"
MODEL_STORE_CLAIM = "inlumen-model-store"
MODEL_ROOT = "/models"
GPU_RESOURCE = "nvidia.com/gpu"
PLUGIN_BASE_URL_ENV = "INLUMEN_PLUGIN_BASE_URL"

# One workflow file per way of running the bundle. `make -C argo submit`
# picks the one matching MODELS and ACCELERATOR.
WORKFLOW_PATH = "argo/workflow.yaml"


def workflow_path(*, models_baked: bool = False, gpu: bool = False) -> str:
    suffix = ("-gpu" if gpu else "") + ("-models-baked" if models_baked else "")
    return f"argo/workflow{suffix}.yaml" if suffix else WORKFLOW_PATH


# The same profiles as codegen/app/resource_policy.py, which sizes local runs;
# test_argo_bundle checks that the two stay in sync. The backend image does not
# ship the codegen package, hence the copy.
GIB = 1024**3
RESOURCE_PROFILES = {
    "lightweight": {"cpu": 1, "memory_bytes": 1 * GIB},
    "standard": {"cpu": 2, "memory_bytes": 2 * GIB},
    "ml_cpu": {"cpu": 4, "memory_bytes": 4 * GIB},
}
_ML_RESOURCE_CLASSES = {"gpu_preferred", "heavy_cpu_or_gpu"}
_STANDARD_RESOURCE_CLASSES = {"lightweight_cpu"}
_ML_REQUIREMENT_MARKERS = (
    "torch",
    "tensorflow",
    "transformers",
    "faster-whisper",
    "ctranslate2",
    "onnxruntime",
)
_STANDARD_REQUIREMENT_MARKERS = (
    "numpy",
    "pandas",
    "polars",
    "pyarrow",
    "scikit-learn",
    "scipy",
)
_QUANTITY_RE = re.compile(r"^[0-9]+(\.[0-9]+)?(m|k|Ki|M|Mi|G|Gi|T|Ti)?$")


def select_resource_profile(
    model_resource_classes: Iterable[str],
    requirements: Iterable[str],
) -> str:
    """Name the profile codegen/app/resource_policy.py would pick for a step."""
    classes = [str(item or "").strip().lower() for item in model_resource_classes]
    if classes and (set(classes) & _ML_RESOURCE_CLASSES or "" in classes):
        return "ml_cpu"
    if set(classes) & _STANDARD_RESOURCE_CLASSES:
        return "standard"
    text = "\n".join(requirements).lower()
    if any(marker in text for marker in _ML_REQUIREMENT_MARKERS):
        return "ml_cpu"
    if any(marker in text for marker in _STANDARD_REQUIREMENT_MARKERS):
        return "standard"
    return "lightweight"


def _quantity(field_name: str, value: Any) -> str:
    text = str(value).strip()
    if isinstance(value, bool) or not _QUANTITY_RE.fullmatch(text):
        raise ValueError(
            f"capabilities.resources.{field_name} must be a Kubernetes quantity "
            f"such as 2, 500m or 4Gi; got {value!r}."
        )
    return text


def step_resources(profile: str, declared: Any = None, *, gpu: bool = False) -> dict:
    """Requests and limits for one step: its profile, then what it declares.

    Requests equal limits, like the CPU and memory caps of local runs. With
    `gpu`, the step also gets `declared["gpu"]` (default 1) NVIDIA GPUs.
    """
    declared = declared if isinstance(declared, dict) else {}
    defaults = RESOURCE_PROFILES[profile]
    cpu = _quantity("cpu", declared.get("cpu", defaults["cpu"]))
    memory = (
        _quantity("memory", declared["memory"])
        if "memory" in declared
        else f"{defaults['memory_bytes'] // GIB}Gi"
    )
    amounts = {"cpu": cpu, "memory": memory}
    resources = {"requests": dict(amounts), "limits": dict(amounts)}
    if gpu:
        count = str(declared.get("gpu", 1)).strip()
        if not count.isdigit() or int(count) < 1:
            raise ValueError(
                f"capabilities.resources.gpu must be a whole number of GPUs; got {declared.get('gpu')!r}."
            )
        resources["limits"][GPU_RESOURCE] = count
    return resources


def wants_gpu(profile: str, declared: Any = None) -> bool:
    """Steps on the model profile, or that ask for a GPU, get one on GPU runs."""
    declared = declared if isinstance(declared, dict) else {}
    if "gpu" in declared:
        return str(declared["gpu"]).strip() not in {"", "0", "false", "False"}
    return profile == "ml_cpu"

# How root steps get their input files. "artifact" (the default) keeps data out
# of the image: `make -C argo inputs` packs inputs/<step>/ and uploads it to the
# artifact repository, and the workflow downloads it at run time. "embedded"
# copies inputs/ into the image, which then must stay private.
INPUTS_ARTIFACT = "artifact"
INPUTS_EMBEDDED = "embedded"
INPUT_MODES = (INPUTS_ARTIFACT, INPUTS_EMBEDDED)
INPUT_PREFIX = "inlumen/input"

# Defaults match the Argo Workflows quick-start (S3 service `minio:9000`,
# bucket `my-bucket`, credentials Secret `my-minio-cred`), so a local test
# cluster works without edits. Real clusters replace them.
QUICKSTART_ENDPOINT = "minio:9000"
QUICKSTART_BUCKET = "my-bucket"
QUICKSTART_CREDENTIALS_SECRET = "my-minio-cred"


@dataclass(frozen=True)
class ArgoEnvironmentParameter:
    flow_id: str
    parameter: str
    environment: str


@dataclass(frozen=True)
class ArgoInputStep:
    """A root step that downloads inputs/<directory>/ as <step>.tgz."""

    step: str
    directory: str
    parameter: str


@dataclass(frozen=True)
class ArgoSecretKey:
    flow_id: str
    key: str
    environment: str
    required: bool


@dataclass(frozen=True)
class ArgoPluginEndpoint:
    flow_id: str
    plugin: str
    parameter: str
    environment: str
    url: str


@dataclass(frozen=True)
class ArgoBundlePlan:
    image_tag: str
    model_store: bool = False
    # Whether some step can use a GPU, so ACCELERATOR=gpu means something.
    gpu: bool = False
    inputs: str = INPUTS_ARTIFACT
    input_steps: Sequence[ArgoInputStep] = field(default_factory=tuple)
    environment_parameters: Sequence[ArgoEnvironmentParameter] = field(default_factory=tuple)
    secrets: Sequence[ArgoSecretKey] = field(default_factory=tuple)
    plugin_endpoints: Sequence[ArgoPluginEndpoint] = field(default_factory=tuple)


def _bash_array(name: str, rows: Sequence[Sequence[str]]) -> str:
    if not rows:
        return f"{name}=()"
    lines = [f"{name}=("]
    lines.extend(f"  {shlex.quote('|'.join(row))}" for row in rows)
    lines.append(")")
    return "\n".join(lines)


_ENV_VALUE_FUNCTION = r'''# Read NAME from the environment first, then from the .env file. The file is
# parsed, not sourced, so values are never executed.
env_value() {
  local name="$1" line key value
  if [ -n "${!name:-}" ]; then
    printf '%s' "${!name}"
    return
  fi
  [ -f "$ENV_FILE" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    key="${line%%=*}"
    value="${line#*=}"
    key="${key#export }"
    key="${key//[[:space:]]/}"
    if [ "$key" = "$name" ]; then
      case "$value" in
        \"*\") value="${value:1:${#value}-2}" ;;
        \'*\') value="${value:1:${#value}-2}" ;;
      esac
      printf '%s' "$value"
      return
    fi
  done < "$ENV_FILE"
}'''


def create_secrets_script(plan: ArgoBundlePlan) -> str:
    rows = [
        (item.key, item.environment, "required" if item.required else "optional")
        for item in plan.secrets
    ]
    return f'''#!/usr/bin/env bash
# Generated by inLUMEN. Creates (or updates) the {RUNTIME_SECRET_NAME}
# Kubernetes Secret from .env, using the keys argo/workflow.yaml references.
# Values are passed to kubectl through private temporary files, never on the
# command line.
set -euo pipefail

ROOT="$(cd "$(dirname "${{BASH_SOURCE[0]}}")/.." && pwd)"
ENV_FILE="${{ENV_FILE:-$ROOT/.env}}"
NAMESPACE="${{NAMESPACE:-argo}}"

# <Secret key>|<.env variable>|<required or optional>
{_bash_array("SECRETS", rows)}

{_ENV_VALUE_FUNCTION}

umask 077
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

args=()
missing=()
index=0
for entry in ${{SECRETS[@]+"${{SECRETS[@]}}"}}; do
  IFS='|' read -r key name required <<<"$entry"
  value="$(env_value "$name")"
  if [ -z "$value" ]; then
    if [ "$required" = required ]; then
      missing+=("$name")
    fi
    continue
  fi
  index=$((index + 1))
  printf '%s' "$value" > "$work/$index"
  args+=("--from-file=$key=$work/$index")
done

if [ "${{#missing[@]}}" -gt 0 ]; then
  echo "Set these variables in $ENV_FILE (see .env.example): ${{missing[*]}}" >&2
  exit 1
fi

kubectl create secret generic {RUNTIME_SECRET_NAME} -n "$NAMESPACE" ${{args[@]+"${{args[@]}}"}} \\
  --dry-run=client -o yaml | kubectl apply -n "$NAMESPACE" -f -
'''


def upload_inputs_script(plan: ArgoBundlePlan) -> str:
    rows = [(item.step, item.directory) for item in plan.input_steps]
    return f'''#!/usr/bin/env bash
# Generated by inLUMEN. Packs inputs/<step>/ for each first step of the
# pipeline into <step>.tgz and uploads it to the artifact repository bucket,
# where the workflow downloads it at run time. The image holds no data.
#
# Bucket and credentials Secret are read from argo/k8s/artifact-repositories.yaml.
# Settings (from the environment or .env, all optional on the Argo quick-start):
#   S3_ENDPOINT   S3 URL reachable from this machine. When unset, the script
#                 forwards svc/$S3_SERVICE (default minio) to localhost.
#   AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
#                 When unset, they are read from the cluster's credentials Secret.
#   S3_BUCKET, S3_TOOL (aws or mc), INPUT_PREFIX, NAMESPACE
set -euo pipefail

ROOT="$(cd "$(dirname "${{BASH_SOURCE[0]}}")/.." && pwd)"
ENV_FILE="${{ENV_FILE:-$ROOT/.env}}"
NAMESPACE="${{NAMESPACE:-argo}}"
INPUT_PREFIX="${{INPUT_PREFIX:-{INPUT_PREFIX}}}"
REPOSITORY_FILE="$ROOT/argo/k8s/artifact-repositories.yaml"

# <step>|<directory under inputs/>
{_bash_array("STEPS", rows)}

{_ENV_VALUE_FUNCTION}

fail() {{
  echo "upload-inputs: $*" >&2
  exit 1
}}

# First value of a "key: value" line in artifact-repositories.yaml, after the
# line matching $1 (use "" for the whole file).
repository_value() {{
  local after="$1" key="$2"
  awk -v after="$after" -v key="$key" '
    after != "" && index($0, after) {{ found = 1; next }}
    (after == "" || found) && $1 == key ":" {{ print $2; exit }}
  ' "$REPOSITORY_FILE"
}}

setting() {{
  local value
  value="$(env_value "$1")"
  printf '%s' "${{value:-$2}}"
}}

S3_BUCKET="$(setting S3_BUCKET "$(repository_value "" bucket)")"
[ -n "$S3_BUCKET" ] || fail "no bucket: set S3_BUCKET, or the bucket in $REPOSITORY_FILE"

S3_TOOL="$(setting S3_TOOL "")"
if [ -z "$S3_TOOL" ]; then
  if command -v aws >/dev/null 2>&1; then S3_TOOL=aws
  elif command -v mc >/dev/null 2>&1; then S3_TOOL=mc
  else fail "install the AWS CLI (https://aws.amazon.com/cli/) or MinIO's mc (https://min.io/docs/minio/linux/reference/minio-mc.html)"
  fi
fi
case "$S3_TOOL" in aws|mc) ;; *) fail "S3_TOOL must be aws or mc, not $S3_TOOL" ;; esac

AWS_ACCESS_KEY_ID="$(setting AWS_ACCESS_KEY_ID "")"
AWS_SECRET_ACCESS_KEY="$(setting AWS_SECRET_ACCESS_KEY "")"
if [ -z "$AWS_ACCESS_KEY_ID" ] || [ -z "$AWS_SECRET_ACCESS_KEY" ]; then
  secret="$(repository_value accessKeySecret name)"
  access_key="$(repository_value accessKeySecret key)"
  secret_key="$(repository_value secretKeySecret key)"
  if [ -n "$secret" ] && command -v kubectl >/dev/null 2>&1 \\
    && AWS_ACCESS_KEY_ID="$(kubectl -n "$NAMESPACE" get secret "$secret" -o "jsonpath={{.data.$access_key}}" 2>/dev/null | base64 --decode)" \\
    && AWS_SECRET_ACCESS_KEY="$(kubectl -n "$NAMESPACE" get secret "$secret" -o "jsonpath={{.data.$secret_key}}" 2>/dev/null | base64 --decode)" \\
    && [ -n "$AWS_ACCESS_KEY_ID" ] && [ -n "$AWS_SECRET_ACCESS_KEY" ]; then
    echo "Using the S3 credentials from Secret $secret in namespace $NAMESPACE."
  else
    fail "no S3 credentials. Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in $ENV_FILE (or the environment), or make sure kubectl can read Secret ${{secret:-<credentials Secret>}} in namespace $NAMESPACE (NAMESPACE=...)."
  fi
fi
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY

umask 077
work="$(mktemp -d)"
forward_pid=""
cleanup() {{
  if [ -n "$forward_pid" ]; then
    kill "$forward_pid" 2>/dev/null || true
    wait "$forward_pid" 2>/dev/null || true
  fi
  rm -rf "$work"
}}
trap cleanup EXIT

S3_ENDPOINT="$(setting S3_ENDPOINT "")"
if [ -z "$S3_ENDPOINT" ]; then
  S3_SERVICE="$(setting S3_SERVICE minio)"
  S3_LOCAL_PORT="$(setting S3_LOCAL_PORT 19000)"
  command -v kubectl >/dev/null 2>&1 || fail "set S3_ENDPOINT to the S3 URL of the artifact repository (kubectl is needed to forward svc/$S3_SERVICE)"
  echo "S3_ENDPOINT is not set: forwarding svc/$S3_SERVICE in namespace $NAMESPACE to localhost:$S3_LOCAL_PORT."
  kubectl -n "$NAMESPACE" port-forward "svc/$S3_SERVICE" "$S3_LOCAL_PORT:9000" >"$work/port-forward.log" 2>&1 &
  forward_pid=$!
  for _ in $(seq 1 50); do
    grep -q "Forwarding from" "$work/port-forward.log" 2>/dev/null && break
    kill -0 "$forward_pid" 2>/dev/null || break
    sleep 0.2
  done
  grep -q "Forwarding from" "$work/port-forward.log" 2>/dev/null \\
    || fail "could not forward svc/$S3_SERVICE: $(cat "$work/port-forward.log"). Set S3_ENDPOINT to the S3 URL of the artifact repository."
  S3_ENDPOINT="http://127.0.0.1:$S3_LOCAL_PORT"
fi

upload() {{
  local file="$1" key="$2"
  if [ "$S3_TOOL" = aws ]; then
    AWS_DEFAULT_REGION="${{AWS_DEFAULT_REGION:-us-east-1}}" \\
    AWS_REQUEST_CHECKSUM_CALCULATION="${{AWS_REQUEST_CHECKSUM_CALCULATION:-when_required}}" \\
      aws --endpoint-url "$S3_ENDPOINT" s3 cp --only-show-errors "$file" "s3://$S3_BUCKET/$key"
  else
    local scheme="${{S3_ENDPOINT%%://*}}" host="${{S3_ENDPOINT#*://}}"
    MC_HOST_inlumen="$scheme://$AWS_ACCESS_KEY_ID:$AWS_SECRET_ACCESS_KEY@$host" \\
      mc cp --quiet "$file" "inlumen/$S3_BUCKET/$key" >/dev/null
  fi
}}

for entry in ${{STEPS[@]+"${{STEPS[@]}}"}}; do
  IFS='|' read -r step directory <<<"$entry"
  [ -d "$ROOT/inputs/$directory" ] || fail "missing directory inputs/$directory"
  # One top-level directory per archive: Argo unpacks it as the step's input
  # directory. COPYFILE_DISABLE stops macOS tar from adding ._ files.
  COPYFILE_DISABLE=1 tar -czf "$work/$step.tgz" --exclude .gitkeep --exclude .DS_Store \\
    -C "$ROOT/inputs" "$directory"
  key="$INPUT_PREFIX/$step.tgz"
  upload "$work/$step.tgz" "$key" \\
    || fail "upload to s3://$S3_BUCKET/$key at $S3_ENDPOINT failed (check S3_ENDPOINT, the bucket and the credentials)"
  echo "Uploaded inputs/$directory to s3://$S3_BUCKET/$key"
done
'''


def submit_script(plan: ArgoBundlePlan) -> str:
    rows = [(item.parameter, item.environment) for item in plan.environment_parameters]
    rows.extend((item.parameter, item.environment) for item in plan.plugin_endpoints)
    input_rows = [(item.parameter, item.step) for item in plan.input_steps]
    return f'''#!/usr/bin/env bash
# Generated by inLUMEN. Submits argo/workflow.yaml (or the WORKFLOW file) with
# the pipeline image, a per-run output prefix, the input archive keys (see
# upload-inputs.sh) and the non-secret runtime settings from .env. Extra
# arguments are passed to `argo submit` (for example --watch).
set -euo pipefail

ROOT="$(cd "$(dirname "${{BASH_SOURCE[0]}}")/.." && pwd)"
ENV_FILE="${{ENV_FILE:-$ROOT/.env}}"
WORKFLOW="${{WORKFLOW:-$ROOT/{WORKFLOW_PATH}}}"
IMAGE="${{IMAGE:?Set IMAGE to the pipeline image, for example with make -C argo submit}}"
NAMESPACE="${{NAMESPACE:-argo}}"
SERVICE_ACCOUNT="${{SERVICE_ACCOUNT:-{SERVICE_ACCOUNT_NAME}}}"
RUN_ID="${{RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}}"
OUTPUT_PREFIX="${{OUTPUT_PREFIX:-inlumen/output/$RUN_ID}}"
INPUT_PREFIX="${{INPUT_PREFIX:-{INPUT_PREFIX}}}"

# <workflow parameter>|<.env variable>
{_bash_array("ENV_PARAMETERS", rows)}

# <workflow parameter>|<step whose inputs archive it names>
{_bash_array("INPUT_PARAMETERS", input_rows)}

{_ENV_VALUE_FUNCTION}

args=(
  -n "$NAMESPACE"
  --serviceaccount "$SERVICE_ACCOUNT"
  -p "pipeline-image=$IMAGE"
  -p "output-artifact-prefix=$OUTPUT_PREFIX"
)
for entry in ${{INPUT_PARAMETERS[@]+"${{INPUT_PARAMETERS[@]}}"}}; do
  IFS='|' read -r parameter step <<<"$entry"
  args+=(-p "$parameter=$INPUT_PREFIX/$step.tgz")
done
for entry in ${{ENV_PARAMETERS[@]+"${{ENV_PARAMETERS[@]}}"}}; do
  IFS='|' read -r parameter name <<<"$entry"
  value="$(env_value "$name")"
  if [ -n "$value" ]; then
    args+=(-p "$parameter=$value")
  fi
done

echo "Results will be written under $OUTPUT_PREFIX/ in the artifact repository bucket."
argo submit "$WORKFLOW" "${{args[@]}}" "$@"
'''


def makefile(plan: ArgoBundlePlan, *, uv_version: str) -> str:
    options = []
    build_args = []
    variant_tag = ""
    workflow_suffix = ""
    if plan.gpu:
        options.append(
            "# CPU or GPU. gpu builds CUDA PyTorch and gives the model steps an\n"
            f"# {GPU_RESOURCE} limit; the cluster needs the NVIDIA device plugin.\n"
            "ACCELERATOR ?= cpu\n"
            "$(if $(filter-out cpu gpu,$(ACCELERATOR)),$(error ACCELERATOR must be cpu or gpu))"
        )
        build_args.append("--build-arg INLUMEN_ACCELERATOR=$(ACCELERATOR)")
        variant_tag += "$(if $(filter gpu,$(ACCELERATOR)),-gpu)"
        workflow_suffix += "$(if $(filter gpu,$(ACCELERATOR)),-gpu)"
    if plan.model_store:
        options.append(
            "# Where the reviewed models come from. volume: the inlumen-model-prefetch\n"
            f"# task fills the {MODEL_STORE_CLAIM} volume. bake: the image build downloads\n"
            "# them into the image, so runs need neither the volume nor internet access.\n"
            "MODELS ?= volume\n"
            "$(if $(filter-out volume bake,$(MODELS)),$(error MODELS must be volume or bake))\n"
            "# Speech-to-text model size: auto, fast, balanced or accuracy. With\n"
            "# MODELS=bake, build and submit with the same value.\n"
            "ASR_PROFILE ?="
        )
        build_args.extend([
            "--build-arg INLUMEN_MODELS=$(MODELS)",
            "--build-arg INLUMEN_ASR_PROFILE=$(or $(ASR_PROFILE),auto)",
            # BuildKit secret: the token never lands in an image layer.
            "$(if $(HF_TOKEN),--secret id=hf_token$(comma)env=HF_TOKEN)",
        ])
        variant_tag += "$(if $(filter bake,$(MODELS)),-models$(if $(ASR_PROFILE),-$(ASR_PROFILE)))"
        workflow_suffix += "$(if $(filter bake,$(MODELS)),-models-baked)"
    options_block = ("\n" + "\n\n".join(options) + "\n") if options else ""
    tag_note = "\n# The options above add a suffix, so each variant gets its own tag." if options else ""
    build_args_line = " ".join(build_args)
    model_store_setup = (
        "\t$(if $(filter volume,$(MODELS)),kubectl apply -n $(NAMESPACE) -f $(ARGO_DIR)/k8s/model-store-pvc.yaml,"
        "@echo \"MODELS=bake: the models are in the image, no model store volume needed.\")\n"
        if plan.model_store
        else ""
    )
    embedded = plan.inputs == INPUTS_EMBEDDED
    if embedded:
        inputs_recipe = "\t@echo \"The inputs are copied into the image (make image).\""
    elif plan.input_steps:
        inputs_recipe = (
            "\tNAMESPACE=$(NAMESPACE) ENV_FILE=$(ENV_FILE) INPUT_PREFIX=$(INPUT_PREFIX) "
            "bash $(ARGO_DIR)/upload-inputs.sh"
        )
    else:
        inputs_recipe = "\t@echo \"This pipeline has no input files to upload.\""
    tag_comment = (
        "# Image tag computed by inLUMEN from the requirements, step code and the\n"
        "# input files copied into the image."
        if embedded
        else "# Image tag computed by inLUMEN from the requirements and step code. The\n"
        "# image holds no data: make inputs uploads inputs/ to the artifact repository."
    )
    image_help = (
        "build the pipeline image $(IMAGE) (step code and inputs/)"
        if embedded
        else "build the pipeline image $(IMAGE) (step code, no data)"
    )
    inputs_help = (
        "inputs are in the image; nothing to upload"
        if embedded
        else "pack inputs/<step>/ and upload it to the artifact repository"
    )
    secrets_recipe = (
        "\tNAMESPACE=$(NAMESPACE) ENV_FILE=$(ENV_FILE) bash $(ARGO_DIR)/create-secrets.sh"
        if plan.secrets
        else "\t@echo \"This pipeline has no sensitive parameters.\""
    )
    asr_submit = "$(if $(ASR_PROFILE),INLUMEN_ASR_PROFILE=$(ASR_PROFILE) )" if plan.model_store else ""
    option_help = ""
    if plan.gpu:
        option_help += "\t@echo \"  ACCELERATOR=cpu|gpu   CPU (default) or NVIDIA GPU\"\n"
    if plan.model_store:
        option_help += (
            "\t@echo \"  MODELS=volume|bake    models in a shared volume (default) or in the image\"\n"
            "\t@echo \"  ASR_PROFILE=<profile> speech-to-text model size (auto, fast, balanced, accuracy)\"\n"
        )
    if option_help:
        option_help = "\t@echo \"Options (use the same ones for image and submit):\"\n" + option_help
    return f'''# Generated by inLUMEN. Builds and submits this pipeline on Argo
# Workflows. Run from the bundle root:   make -C argo help
# Every setting below can be overridden: make -C argo image REGISTRY=ghcr.io/acme

ARGO_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
ROOT := $(abspath $(ARGO_DIR)/..)
comma := ,
{options_block}
{tag_comment}
# When argo/requirements.lock exists (make lock), its checksum is appended.{tag_note}
EXPORT_TAG := {plan.image_tag}
LOCK_SUM := $(shell [ -f $(ARGO_DIR)/requirements.lock ] && cksum < $(ARGO_DIR)/requirements.lock | cut -d' ' -f1)
REGISTRY ?=
IMAGE_NAME ?= {IMAGE_NAME}
TAG ?= $(EXPORT_TAG)$(if $(LOCK_SUM),-$(LOCK_SUM)){variant_tag}
IMAGE ?= $(if $(REGISTRY),$(REGISTRY)/)$(IMAGE_NAME):$(TAG)
WORKFLOW_FILE ?= $(ARGO_DIR)/workflow{workflow_suffix}.yaml

NAMESPACE ?= argo
SERVICE_ACCOUNT ?= {SERVICE_ACCOUNT_NAME}
ENV_FILE ?= $(ROOT)/.env
INPUT_PREFIX ?= {INPUT_PREFIX}
ARGO_FLAGS ?= --watch
K3D_CLUSTER ?= k3s-default
KIND_CLUSTER ?= kind
UV ?= uv

.PHONY: help print-image print-workflow image push k3d-import kind-load lock setup inputs secrets submit

help:
\t@echo "Targets (run from the bundle root with make -C argo <target>):"
\t@echo "  image        {image_help}"
\t@echo "  push         push the image to REGISTRY"
\t@echo "  k3d-import   load the image into a local k3d cluster (no registry)"
\t@echo "  kind-load    load the image into a local kind cluster (no registry)"
\t@echo "  lock         resolve exact package versions into argo/requirements.lock"
\t@echo "  setup        apply the service account, RBAC, artifact repository{' and model store' if plan.model_store else ''}"
\t@echo "  inputs       {inputs_help}"
\t@echo "  secrets      create the {RUNTIME_SECRET_NAME} Secret from .env"
\t@echo "  submit       submit $(notdir $(WORKFLOW_FILE))"
{option_help}
print-image:
\t@echo $(IMAGE)

print-workflow:
\t@echo $(WORKFLOW_FILE)

image:
\tdocker build -f $(ARGO_DIR)/Dockerfile {build_args_line + " " if build_args_line else ""}-t $(IMAGE) $(ROOT)

push:
\t@test -n "$(REGISTRY)" || (echo "Set REGISTRY, for example make -C argo push REGISTRY=ghcr.io/acme" >&2; exit 1)
\tdocker push $(IMAGE)

k3d-import:
\tk3d image import $(IMAGE) -c $(K3D_CLUSTER)

kind-load:
\tkind load docker-image $(IMAGE) --name $(KIND_CLUSTER)

# Needs uv {uv_version} or newer on the machine that runs it. Commit the
# result: later builds then install exactly the same versions. PyTorch is
# locked to its CPU-only build, like the image; use TORCH_BACKEND=auto for CUDA.
TORCH_BACKEND ?= cpu
lock:
\tcd $(ARGO_DIR) && $(UV) pip compile requirements.txt -c constraints.txt \\
\t  --universal --python-version 3.11 --emit-index-url --torch-backend $(TORCH_BACKEND) -o requirements.lock

setup:
\tkubectl apply -n $(NAMESPACE) -f $(ARGO_DIR)/k8s/rbac.yaml
\tkubectl apply -n $(NAMESPACE) -f $(ARGO_DIR)/k8s/artifact-repositories.yaml
{model_store_setup}
inputs:
{inputs_recipe}

secrets:
{secrets_recipe}

submit:
\t{asr_submit}WORKFLOW=$(WORKFLOW_FILE) IMAGE=$(IMAGE) NAMESPACE=$(NAMESPACE) SERVICE_ACCOUNT=$(SERVICE_ACCOUNT) ENV_FILE=$(ENV_FILE) \\
\t  INPUT_PREFIX=$(INPUT_PREFIX) bash $(ARGO_DIR)/submit.sh $(ARGO_FLAGS)
'''


def model_store_pvc_manifest() -> str:
    return f'''# Generated by inLUMEN. The verified model store. The {MODEL_PREFETCH_TASK}
# task downloads the pinned model revisions into it once, checks their SHA-256
# manifests, and later runs reuse them. Model steps mount it read-only at
# {MODEL_ROOT}.
#
# ReadWriteOnce works on single-node clusters (k3d, kind, Docker Desktop).
# On multi-node clusters use a ReadWriteMany storage class, so steps on other
# nodes can mount it too. Size it for your models (about 2 GB per ASR model).
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {MODEL_STORE_CLAIM}
spec:
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: 20Gi
'''


def artifact_repositories_manifest() -> str:
    return f'''# Generated by inLUMEN. The artifact repository argo/workflow.yaml uses
# (spec.artifactRepositoryRef). The values below match the Argo Workflows
# quick-start, which runs an S3-compatible store as `minio:9000` with the bucket
# `{QUICKSTART_BUCKET}` and the credentials Secret `{QUICKSTART_CREDENTIALS_SECRET}`.
# For any other cluster, set your own endpoint, bucket and credentials Secret.
apiVersion: v1
kind: ConfigMap
metadata:
  name: {ARTIFACT_REPOSITORY_CONFIGMAP}
data:
  {ARTIFACT_REPOSITORY_KEY}: |
    s3:
      endpoint: {QUICKSTART_ENDPOINT}
      bucket: {QUICKSTART_BUCKET}
      insecure: true
      accessKeySecret:
        name: {QUICKSTART_CREDENTIALS_SECRET}
        key: accesskey
      secretKeySecret:
        name: {QUICKSTART_CREDENTIALS_SECRET}
        key: secretkey
'''


def rbac_manifest() -> str:
    return f'''# Generated by inLUMEN. The service account the workflow pods run as, with
# the permission the Argo executor needs to report step results
# (Argo Workflows 3.4 or newer).
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {SERVICE_ACCOUNT_NAME}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: {SERVICE_ACCOUNT_NAME}
rules:
  - apiGroups: ["argoproj.io"]
    resources: ["workflowtaskresults"]
    verbs: ["create", "patch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: {SERVICE_ACCOUNT_NAME}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: {SERVICE_ACCOUNT_NAME}
subjects:
  - kind: ServiceAccount
    name: {SERVICE_ACCOUNT_NAME}
'''


def runtime_secrets_example(plan: ArgoBundlePlan) -> str:
    lines = [
        "# Generated by inLUMEN. Example only: `make -C argo secrets` builds this",
        "# Secret from .env. Each key's .env variable is noted next to it.",
        "apiVersion: v1",
        "kind: Secret",
        "metadata:",
        f"  name: {RUNTIME_SECRET_NAME}",
        "type: Opaque",
        "stringData:",
    ]
    for item in plan.secrets:
        need = "required" if item.required else "optional"
        lines.append(f'  {json.dumps(item.key)}: ""  # .env: {item.environment} ({need})')
    return "\n".join(lines) + "\n"


def github_actions_template(plan: ArgoBundlePlan) -> str:
    paths = '["argo/**", "inputs/**", "nodes/**"]' if plan.inputs == INPUTS_EMBEDDED else '["argo/**", "nodes/**"]'
    return '''# Generated by inLUMEN. Builds and pushes the pipeline image to the GitHub
# Container Registry. Copy this file to .github/workflows/build-image.yml in
# the repository that holds the bundle to turn it on.
name: Build pipeline image

on:
  push:
    branches: [main]
    paths: PATHS
  workflow_dispatch:

permissions:
  contents: read
  packages: write

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Image name
        id: image
        run: |
          echo "image=$(make --no-print-directory -s -C argo print-image REGISTRY=ghcr.io/${GITHUB_REPOSITORY,,})" >> "$GITHUB_OUTPUT"
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      # Add image scanning (for example aquasecurity/trivy-action) and signing
      # (for example sigstore/cosign-installer) here, as your policy requires.
      # For another variant (for example MODELS=bake or ACCELERATOR=gpu), pass
      # the same options to print-image above and as build-args below
      # (INLUMEN_MODELS, INLUMEN_ASR_PROFILE, INLUMEN_ACCELERATOR).
      - uses: docker/build-push-action@v6
        with:
          context: .
          file: argo/Dockerfile
          push: true
          tags: ${{ steps.image.outputs.image }}
'''.replace("PATHS", paths)


def gitignore() -> str:
    return '''# Generated by inLUMEN. Keeps local values and run results out of git.
.env
outputs/**
!outputs/**/
!outputs/**/.gitkeep
'''


def dockerignore(*, exclude_inputs: bool = False) -> str:
    content = '''# Generated by inLUMEN. Keeps local values and run results out of image builds.
.env
.git
outputs
validation
'''
    if exclude_inputs:
        content += "# Input data is uploaded with make -C argo inputs, never built into the image.\ninputs\n"
    return content


def readme_section(plan: ArgoBundlePlan) -> str:
    embedded = plan.inputs == INPUTS_EMBEDDED
    uploads = not embedded and bool(plan.input_steps)
    registry_note = (
        "Use a **private** registry: the image contains `inputs/`."
        if embedded
        else "The image holds no data."
    )
    steps = [
        "**Image.** `make -C argo image push REGISTRY=registry.example.com/team` builds "
        f"`argo/Dockerfile` and pushes it. {registry_note} In CI, use `ci/github-actions/build-image.yml` (copy it to "
        "`.github/workflows/`); add image scanning and signing there as your policy requires.",
        "**Exact versions (recommended).** `make -C argo lock` resolves every package into "
        "`argo/requirements.lock`. Commit it: later builds install exactly those versions, "
        "and the tag gets the lock checksum.",
        "**Cluster objects.** Edit `argo/k8s/artifact-repositories.yaml` to point at your "
        "bucket and credentials Secret, then run `make -C argo setup NAMESPACE=<namespace>`.",
    ]
    if uploads:
        steps.append(
            "**Inputs.** `make -C argo inputs NAMESPACE=<namespace>` packs each "
            "`inputs/<step>/` into `<step>.tgz` and uploads it to the bucket under "
            f"`{INPUT_PREFIX}/`. It needs the AWS CLI or MinIO's `mc`. Set `S3_ENDPOINT` and, "
            "unless kubectl can read the credentials Secret named in "
            "`argo/k8s/artifact-repositories.yaml`, `AWS_ACCESS_KEY_ID` and "
            "`AWS_SECRET_ACCESS_KEY` in `.env` or the environment. Use the same "
            "`INPUT_PREFIX=` for `inputs` and `submit` to keep several input sets apart."
        )
    if plan.secrets:
        steps.append(
            "**Secrets.** Copy `.env.example` to `.env`, fill in the values, then run "
            "`make -C argo secrets NAMESPACE=<namespace>`. It creates the "
            f"`{RUNTIME_SECRET_NAME}` Secret with the keys `argo/workflow.yaml` uses "
            "(see `argo/k8s/runtime-secrets.example.yaml`)."
        )
    steps.append(
        "**Run.** `make -C argo submit REGISTRY=registry.example.com/team "
        "NAMESPACE=<namespace>`. Set `ARGO_FLAGS=` to return without watching."
    )
    if plan.model_store:
        steps.insert(
            3,
            "**Model store.** `make -C argo setup` also creates the "
            f"`{MODEL_STORE_CLAIM}` volume (`argo/k8s/model-store-pvc.yaml`). Use a "
            "ReadWriteMany storage class on multi-node clusters, and size it for your models. "
            "Without such storage or without internet access, use `MODELS=bake` (see \"Options\").",
        )
    inputs_quickstart = (
        "make -C argo inputs                                 # upload inputs/ to the bucket\n"
        if uploads
        else ""
    )
    if embedded:
        inputs_intro = (
            "The image also contains the files in `inputs/`, which the first steps read "
            "from `/workspace/inputs/<step>/`. The image tag "
            f"(`{plan.image_tag}`) is computed from the step code, requirements and input "
            "files, so the same pipeline with the same inputs always gets the same tag."
        )
        inputs_paragraph = (
            "**Keep the image private.** This bundle was exported with embedded inputs: "
            "because the image contains your input data, load it straight into a local "
            "cluster or push it only to a private registry. To run the pipeline on other "
            "data, replace the files under `inputs/<step>/` and build again. Export with "
            "artifact inputs (the default) to keep data out of the image."
        )
    else:
        inputs_intro = (
            "The image holds **no data**, so you can push it to a shared registry. The image "
            f"tag (`{plan.image_tag}`) is computed from the step code and requirements, so "
            "the same pipeline always gets the same tag, whatever its inputs."
        )
        inputs_paragraph = (
            "**Inputs travel as artifacts.** `make -C argo inputs` packs each "
            "`inputs/<step>/` into `<step>.tgz` and uploads it to the artifact repository "
            f"under `{INPUT_PREFIX}/`; the first steps download it when the workflow runs. "
            "To run the pipeline on other data, replace the files under `inputs/<step>/` "
            "and run `make -C argo inputs` again; no rebuild is needed. For a quick local "
            "test without the upload, export the bundle with embedded inputs instead "
            "(`?inputs=embedded` on the `argo-bundle.zip` API); that image then contains "
            "your data and must stay private."
            if uploads
            else "This pipeline has no input files to upload."
        )
    s3_client_need = (
        "- The AWS CLI or MinIO's `mc`, to upload the inputs\n" if uploads else ""
    )
    registry_need = (
        "- A private container registry, unless you load the image straight into a local cluster"
        if embedded
        else "- A container registry the cluster can pull from, unless you load the image straight into a local cluster"
    )
    inputs_troubleshooting = (
        "- `The specified key does not exist` (or `NoSuchKey`) when a first step loads `run-inputs`: "
        "run `make -C argo inputs`, with the same `NAMESPACE` and `INPUT_PREFIX` as `submit`.\n"
        "- `upload-inputs: no S3 credentials`: set `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` "
        "in `.env`, or run `make -C argo setup` and check that kubectl can read the credentials "
        "Secret named in `argo/k8s/artifact-repositories.yaml`.\n"
        if uploads
        else ""
    )
    own_cluster_steps = "\n".join(
        f"{index}. {step}" for index, step in enumerate(steps, start=1)
    )
    secrets_quickstart = (
        "cp .env.example .env   # then fill in the values\n"
        "make -C argo secrets\n"
        if plan.secrets
        else ""
    )
    models_section = (
        f"""**Reviewed models** stay out of the image by default. The first task of the workflow, `{MODEL_PREFETCH_TASK}`, downloads the pinned model revisions (listed in `argo/model-prefetch/model-requirements.json`) into the `{MODEL_STORE_CLAIM}` volume and verifies their SHA-256 manifests. The model steps mount that volume read-only at `{MODEL_ROOT}`. Only the first run downloads; later runs reuse the verified models. The prefetch task needs internet access to Hugging Face; set `HF_TOKEN` in `.env` (then `make -C argo secrets`) for gated models. To choose the speech-to-text model size, add `ASR_PROFILE=fast` (or `balanced`, `accuracy`; the default `auto` picks `balanced` on CPU and `accuracy` on GPU) to `make -C argo submit`, or set `INLUMEN_ASR_PROFILE` in `.env`. If your cluster has no shared storage or no internet access, bake the models into the image instead (`MODELS=bake`, see "Options" below).

"""
        if plan.model_store
        else ""
    )
    option_sections = [
        f"""**CPU and memory.** Each step requests, and is limited to, the CPU and memory of its resource profile, the same profiles inLUMEN uses for local runs: `lightweight` (1 CPU, 1 GiB), `standard` (2 CPU, 2 GiB, for data-processing packages such as pandas) and `ml_cpu` (4 CPU, 4 GiB, for model steps and packages such as torch). A node can declare its own `cpu`, `memory` and `gpu` under `capabilities.resources` in its `node-manifest.json`; those values win. To change them for a cluster, edit `resources:` in the workflow files (they are plain YAML). A step stays `Pending` with `Insufficient cpu` or `Insufficient memory` when no node has that much free."""
    ]
    if plan.gpu:
        option_sections.append(
            f"""**GPU (`ACCELERATOR=gpu`).** `make -C argo image submit ACCELERATOR=gpu` builds the image with CUDA PyTorch (`--build-arg INLUMEN_ACCELERATOR=gpu`) and submits `argo/workflow-gpu.yaml`, where the model steps also get a `{GPU_RESOURCE}: 1` limit (or the `gpu` count their node declares) and `INLUMEN_ACCELERATOR=gpu`, as in the Dagster bundle. The cluster needs NVIDIA GPU nodes with the NVIDIA device plugin (or GPU operator); add node selectors or tolerations to the workflow file if your GPU nodes are tainted. If you use `make -C argo lock`, lock with `TORCH_BACKEND=auto` for GPU images. The image tag gets a `-gpu` suffix. The default, `ACCELERATOR=cpu`, installs CPU-only PyTorch and asks for no GPU."""
        )
    if plan.model_store:
        option_sections.append(
            f"""**Models in the image (`MODELS=bake`).** For clusters without ReadWriteMany storage or without internet access. `make -C argo image MODELS=bake` runs the same prefetch script, with the same SHA-256 verification, during `docker build`, into `{MODEL_ROOT}` in the image. `make -C argo setup submit MODELS=bake` then skips the volume and submits `argo/workflow-models-baked.yaml`, which has no `{MODEL_PREFETCH_TASK}` task and mounts no volume, so runs need no access to Hugging Face. The image is larger (about 0.5 to 2 GB per model) and its tag gets a `-models` suffix. Only the models for the chosen speech-to-text size are baked: pass the same `ASR_PROFILE` (and `ACCELERATOR`) to `image` and `submit`. For gated models, run `HF_TOKEN=... make -C argo image MODELS=bake`: the token reaches the build as a BuildKit secret and is not stored in the image. Check the model licences before pushing such an image to a shared registry. The default, `MODELS=volume`, keeps the models out of the image."""
        )
    options_section = "\n\n".join(option_sections)
    troubleshooting_extra = ""
    if plan.model_store:
        troubleshooting_extra += (
            "\n- `Reviewed model is not available in the verified local model store`: with the "
            f"default volume, check the logs of `{MODEL_PREFETCH_TASK}`; with `MODELS=bake`, "
            "build and submit with the same `ASR_PROFILE` and `ACCELERATOR`."
        )
    if plan.plugin_endpoints:
        troubleshooting_extra += (
            "\n- A plugin step fails with `Name or service not known`, a timeout or "
            "`Connection refused`: the pod cannot resolve or reach the plugin server. Check "
            "cluster DNS, egress rules and the plugin URL parameter (see \"Plugin steps\")."
        )
    plugins_section = ""
    if plan.plugin_endpoints:
        rows = "\n".join(
            f"| `{item.flow_id}` | {item.plugin or 'plugin'} | `{item.parameter}` | `{item.environment}` | `{item.url}` |"
            for item in plan.plugin_endpoints
        )
        plugins_section = f"""
### Plugin steps

Plugin steps call their tool server over HTTP(S) from inside the cluster. The pods of those steps need:

- **DNS** that resolves the server's host name (cluster DNS for an in-cluster service, upstream DNS for an external one);
- **egress** to that host and port: allow it in your `NetworkPolicy`, egress gateway or proxy (set `HTTPS_PROXY`/`NO_PROXY` on the templates if you use one);
- the plugin's credentials, if it needs any, in the `{RUNTIME_SECRET_NAME}` Secret (`make -C argo secrets`).

Each plugin step reads its server address from a workflow parameter. The default is the address in the plugin manifest; to use your own copy of the service, for example an in-cluster one, set the `.env` variable (or pass `ARGO_FLAGS="--watch -p <parameter>=<url>"`):

| Step | Plugin | Workflow parameter | `.env` variable | Default |
|---|---|---|---|---|
{rows}

For example `{plan.plugin_endpoints[0].environment}=http://<service>.<namespace>.svc.cluster.local:<port>`. Redirects are never followed, so use the final address.
"""
    return f"""
## Argo

inLUMEN's job ends with this bundle. You build the image, set up the cluster and run the workflow with your own tools; inLUMEN never contacts your registry or cluster.

The workflow uses **one shared image** for every step: the steps differ only in the command they run. {inputs_intro}

{models_section}{inputs_paragraph}

### What you need

- Docker, `kubectl`, the `argo` CLI and `make`
- A Kubernetes cluster with Argo Workflows 3.4 or newer
- An S3-compatible artifact repository, which Argo uses to pass inputs and results between steps (the Argo quick-start includes one)
{s3_client_need}{registry_need}

### Quickstart on a local k3d cluster

```bash
k3d cluster create inlumen
kubectl create namespace argo
kubectl apply --server-side -n argo -f https://github.com/argoproj/argo-workflows/releases/download/v4.1.5/quick-start-minimal.yaml
kubectl -n argo rollout status deployment/workflow-controller deployment/minio

make -C argo image k3d-import K3D_CLUSTER=inlumen   # no registry needed
make -C argo setup                                  # service account, RBAC, artifact repository
{inputs_quickstart}{secrets_quickstart}make -C argo submit
```

Follow the run in the Argo UI: `kubectl -n argo port-forward svc/argo-server 2746:2746`, then open https://localhost:2746 (accept the self-signed certificate).

The quick-start runs an S3-compatible store as the `minio` service, with the bucket `my-bucket` and the credentials `admin` / `password`; `argo/k8s/artifact-repositories.yaml` already points at it. {"With no `S3_ENDPOINT` set, `make -C argo inputs` forwards that service to localhost and reads the credentials from the cluster, so it needs no settings there. " if uploads else ""}Results are written under `inlumen/output/<run id>/` in that bucket: what each Destination receives goes to `<destination step>/<input port>/`, and outputs of final steps to `<step>/<output port>/`. You can download them from the workflow in the Argo UI, or with any S3 client after `kubectl -n argo port-forward svc/minio 19000:9000` (19000 because inLUMEN's own MinIO may use 9000).

### Steps on your own cluster

{own_cluster_steps}

### Options

{options_section}
{plugins_section}
Run `make -C argo help` for all targets and settings. The scripts in `argo/` are plain Bash, so you can also use them from your own automation.

### Troubleshooting

- `ImagePullBackOff`: the cluster cannot see the image. Push it to a registry the cluster can reach, or use `make -C argo k3d-import` / `kind-load` for local clusters.
- `artifact repository ... not found`: run `make -C argo setup` in the same namespace you submit to.
{inputs_troubleshooting}- `secret "inlumen-runtime-secrets" not found` or a missing key: run `make -C argo secrets`.
- `workflowtaskresults ... forbidden`: run `make -C argo setup`, and submit with the `{SERVICE_ACCOUNT_NAME}` service account (the default).
- A step stays `Pending` with `Insufficient cpu`, `memory` or `{GPU_RESOURCE}`: no node has that much free. Lower `resources:` in the workflow file, or add capacity.{troubleshooting_extra}
"""
