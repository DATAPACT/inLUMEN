import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from app.step_cache import StepCache, plan_step_cache


def node(flow_id, kind, *, parents=(), template="Custom Logic", execution="python-package", **extra):
    return {
        "id": flow_id, "kind": kind, "template": template, "parents": list(parents),
        "output_path": f"outputs/node-{flow_id}", "execution": {"kind": execution}, **extra,
    }


def write_bundle(root: Path, nodes, *, code="print(1)\n", params="parameters: {}\n"):
    (root / "run-spec.json").write_text(json.dumps({"nodes": nodes}))
    (root / "bundle-manifest.json").write_text(json.dumps({"nodes": [
        {"flow_id": item["id"], "asset_key": f"node_{item['id']}", "implementation": item.get("implementation", {})}
        for item in nodes
    ]}))
    for item in nodes:
        (root / f"nodes/node-{item['id']}").mkdir(parents=True, exist_ok=True)
        (root / f"nodes/node-{item['id']}/main.py").write_text(code)
        defs = root / f"dagster/src/inlumen_dagster_project/defs/node_{item['id']}"
        defs.mkdir(parents=True, exist_ok=True)
        (defs / "defs.yaml").write_text(params)
    (root / "dagster").mkdir(exist_ok=True)
    (root / "dagster/requirements.txt").write_text("dagster==1\n")


PIPELINE = [
    node("src", "source", template="File", execution="managed-adapter"),
    node("a", "task", parents=["src"]),
    node("b", "task", parents=["a"]),
    node("c", "task", parents=["src"]),
    node("dest", "destination", parents=["b"], template="File", execution="managed-adapter"),
]


def test_code_key_follows_code_parameters_and_runtime_only(tmp_path):
    keys = {}
    for name, kwargs in {"base": {}, "same": {}, "code": {"code": "print(2)\n"}, "params": {"params": "parameters: {k: 2}\n"}}.items():
        root = tmp_path / name
        root.mkdir()
        write_bundle(root, PIPELINE, **kwargs)
        keys[name] = plan_step_cache(root).steps["node-a"]["static_key"]
    assert keys["base"] == keys["same"]
    assert keys["code"] != keys["base"]
    assert keys["params"] != keys["base"]


def test_only_side_effect_free_steps_are_cacheable(tmp_path):
    nodes = [
        *PIPELINE,
        node("api", "source", template="REST API", execution="managed-adapter"),
        node("secret", "task", secret_parameters=[{"name": "API_KEY"}]),
        node("effects", "task", capabilities={"side_effects": ["sends email"]}),
        node("optout", "task", implementation={"cacheable": False}),
    ]
    write_bundle(tmp_path, nodes)
    cacheable = {flow: step["cacheable"] for flow, step in
                 ((step["flow_id"], step) for step in plan_step_cache(tmp_path).steps.values())}
    assert cacheable == {
        "src": True, "a": True, "b": True, "c": True, "dest": False,
        "api": False, "secret": False, "effects": False, "optout": False,
    }


def test_rerun_from_and_refresh_disable_lookups(tmp_path):
    write_bundle(tmp_path, PIPELINE)
    lookup = lambda plan: {step["flow_id"]: step["lookup"] for step in plan.steps.values()}
    assert lookup(plan_step_cache(tmp_path, rerun_from="a")) == {
        "src": True, "a": False, "b": False, "c": True, "dest": False,
    }
    assert not any(lookup(plan_step_cache(tmp_path, mode="refresh")).values())


class FakeClient:
    def __init__(self):
        self.objects = {}
        self.clock = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def bucket_exists(self, bucket):
        return True

    def list_objects(self, bucket, prefix="", recursive=False):
        return [SimpleNamespace(object_name=name, size=len(body), last_modified=modified)
                for (b, name), (body, modified) in sorted(self.objects.items()) if b == bucket and name.startswith(prefix)]

    def _put(self, bucket, name, body):
        self.clock += timedelta(seconds=1)
        self.objects[(bucket, name)] = (body, self.clock)

    def fput_object(self, bucket, name, path, content_type=""):
        self._put(bucket, name, Path(path).read_bytes())

    def put_object(self, bucket, name, data, length, content_type=""):
        self._put(bucket, name, data.read(length))

    def fget_object(self, bucket, name, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(self.objects[(bucket, name)][0])

    def remove_objects(self, bucket, delete_objects):
        for item in delete_objects:
            self.objects.pop((bucket, item._name), None)
        return iter(())


def receipt(published: Path, static_key: str, digest: str, **extra):
    (published / "output").mkdir(parents=True, exist_ok=True)
    (published / "output/result.json").write_text(json.dumps({"digest": digest}))
    return {"flow_id": "a", "status": "succeeded", "cached": False, "static_key": static_key,
            "input_digest": digest, "outputs": [{"path": "output/result.json", "sha256": "x"}],
            "_published_root": str(published), **extra}


def test_results_are_recorded_staged_and_pruned(tmp_path, monkeypatch):
    monkeypatch.setenv("INLUMEN_STEP_CACHE_KEEP", "2")
    (tmp_path / "bundle").mkdir()
    write_bundle(tmp_path / "bundle", PIPELINE)
    plan = plan_step_cache(tmp_path / "bundle")
    key = plan.steps["node-a"]["static_key"]
    client = FakeClient()
    cache = StepCache(client, "ws-1")

    skipped = [
        receipt(tmp_path / "cached", key, "d0", cached=True),
        receipt(tmp_path / "failed", key, "d0", status="failed"),
        receipt(tmp_path / "stale", "other-code", "d0"),
    ]
    assert cache.record(plan, skipped, run_id="r0") == 0
    for index in range(3):
        assert cache.record(plan, [receipt(tmp_path / f"run{index}", key, f"d{index}")], run_id=f"r{index}") == 1
    kept = sorted({name.split("/")[2] for (_b, name) in client.objects})
    assert kept == ["d1", "d2"]  # the oldest result was pruned

    staged = tmp_path / "staged"
    assert cache.stage(plan, staged) == 2
    assert json.loads((staged / "node-a/d2/files/output/result.json").read_text()) == {"digest": "d2"}
    keys = json.loads((staged / "keys.json").read_text())
    assert keys["node-a"] == {"flow_id": "a", "static_key": key, "cacheable": True, "lookup": True}
    assert keys["node-dest"]["cacheable"] is False

    no_lookup = tmp_path / "refresh"
    assert cache.stage(plan_step_cache(tmp_path / "bundle", mode="refresh"), no_lookup) == 0
    assert not (no_lookup / "node-a").exists()
