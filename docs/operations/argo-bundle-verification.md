# Argo bundle release check

inLUMEN's responsibility ends when it produces the Argo bundle (issue
[#161](https://github.com/DATAPACT/inLUMEN/issues/161)). Building the image and
running the workflow happen outside inLUMEN, so neither the unit tests nor CI run
a bundle on Argo. Run this check by hand on a local cluster before each release,
and record the results at the end of this file.

The unit tests cover the bundle layout, the generated scripts (against fake
`kubectl` and `argo`), the model store and the workflow structure. This check
covers what they cannot: whether the image builds, and whether the workflow runs
on a current Argo Workflows release.

## What you need

Docker, `k3d`, `kubectl`, the `argo` CLI and `make`, plus internet access for the
image build and for the model download.

## 1. Cluster with Argo Workflows

```bash
k3d cluster create inlumen
kubectl create namespace argo
kubectl apply --server-side -n argo -f https://github.com/argoproj/argo-workflows/releases/download/v4.1.5/quick-start-minimal.yaml
kubectl -n argo rollout status deployment/workflow-controller deployment/minio
```

Server-side apply is required: the Argo 4 CRDs are too large for a normal
`kubectl apply`. Use the latest Argo release and update the version here when it
changes.

## 2. Export two bundles from inLUMEN

Export with **Export Argo bundle** in the deployment panel:

- a pipeline **without** reviewed models (for example Source → Task → File
  Destination);
- a pipeline **with** reviewed models (for example the uploaded-audio pipeline:
  Speech-to-Text and Sentiment).

## 3. Run each bundle

From each unzipped bundle:

```bash
make -C argo image k3d-import K3D_CLUSTER=inlumen
make -C argo setup NAMESPACE=argo
make -C argo submit NAMESPACE=argo
```

If the bundle has `.env.example`, first copy it to `.env`, fill it in, and run
`make -C argo secrets NAMESPACE=argo`.

Follow the runs in the Argo UI: `kubectl -n argo port-forward svc/argo-server 2746:2746`,
then open https://localhost:2746.

## 4. Expected results

- The image builds in a few minutes and installs CPU-only PyTorch (no `nvidia-*`
  packages).
- Every step succeeds.
- With reviewed models, `inlumen-model-prefetch` runs first and logs `verified`
  for each model; a second run logs `verified cache hit` instead of downloading.
- Each step's pod has the CPU and memory requests and limits of its resource
  profile (`kubectl -n argo get pod <pod> -o jsonpath='{.spec.containers[0].resources}'`).
- The final results are under `inlumen/output/<run id>/` in the `my-bucket`
  bucket (download them from the Argo UI, or with an S3 client after
  `kubectl -n argo port-forward svc/minio 19000:9000`).

## 5. Models baked into the image

For the bundle with reviewed models, check that it also runs without the model
volume and without internet access:

```bash
kubectl -n argo delete pvc inlumen-model-store --ignore-not-found
make -C argo image k3d-import MODELS=bake ASR_PROFILE=fast K3D_CLUSTER=inlumen
make -C argo setup MODELS=bake NAMESPACE=argo
kubectl -n argo apply -f - <<'YAML'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: inlumen-no-internet
spec:
  podSelector:
    matchExpressions:
      - {key: workflows.argoproj.io/workflow, operator: Exists}
  policyTypes: ["Egress"]
  egress:
    - to:
        - ipBlock: {cidr: 10.0.0.0/8}
        - ipBlock: {cidr: 172.16.0.0/12}
        - ipBlock: {cidr: 192.168.0.0/16}
YAML
make -C argo submit MODELS=bake ASR_PROFILE=fast NAMESPACE=argo
kubectl -n argo delete networkpolicy inlumen-no-internet
```

The policy lets workflow pods reach the cluster (k3s enforces it) but not the
internet, so Hugging Face is unreachable during the run. Expected: the image
build logs `verified` for each model, there is no `inlumen-model-prefetch` task,
no PVC exists, and every step succeeds.

## 6. Clean up

```bash
k3d cluster delete inlumen
```

## Results

### 2026-10-19 (issue #161, phase 1 and model store)

Environment: Docker 29.8.0 on macOS ARM64, k3d v5.9.0 (k3s v1.35.5), Argo
Workflows v4.1.5 quick-start.

- Order-summary pipeline (Source → Task → File Destination), bundle built offline
  from `examples/order-summary` with an added `inlumen.task.json`: 3/3 steps
  succeeded; `inlumen/output/<run id>/result/data/summary.json` matched
  `examples/order-summary/expected-summary.json` (3 orders, 49.75).
- Uploaded-audio pipeline exported from the inLUMEN UI (4 steps, Whisper and
  RoBERTa sentiment): 5/5 tasks succeeded in 1m37s with the `fast` ASR profile.
  The prefetch task downloaded and verified `Systran/faster-whisper-small` and
  reused the verified RoBERTa model from an earlier run.
- Found and fixed while testing: MinIO images are no longer published on Docker
  Hub (the v3.6 quick-start fails; v4.1.5 uses a maintained replacement), the
  large CRDs need server-side apply, results reaching a Destination were not
  stored, the image installed the CUDA build of PyTorch, and reviewed models had
  no model store.

### 2026-10-19 (issue #161, phase 2: baked models, resources, plugin URLs)

Environment: Docker 29.8.0 on macOS ARM64, k3d v5.9.0 (k3s v1.35.5), Argo
Workflows v4.1.5 quick-start.

- Uploaded-audio pipeline (4 steps, Whisper and RoBERTa sentiment), bundle
  built with this branch from the pipeline in a local inLUMEN instance.
- `make -C argo image MODELS=bake ASR_PROFILE=fast`: the build downloaded and
  verified `Systran/faster-whisper-small` and the RoBERTa sentiment model into
  the image (2.7 GB).
- `setup` and `submit` with `MODELS=bake`, no PVC in the namespace, and the
  egress policy above (checked first: a labelled pod could reach the in-cluster
  object store but not huggingface.co, once the policy applied about a second
  after the pod started): 4/4 steps succeeded in 1m28s with no prefetch task;
  the result was stored under `inlumen/output/bake-1/step-4/data/`.
- Pods had the profile resources: 1 CPU / 1 GiB for the source and destination,
  4 CPU / 4 GiB for the two model steps; none mounted a model volume.
- Not run on a cluster: `ACCELERATOR=gpu` (no GPU available) and plugin URL
  parameters; both are covered by unit tests only.
