"""Exercise compiled packages and exported handoff with real child processes."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "codegen")]
from artifact_runtime import (
    ArtifactContractError,
    CONTRACT_ID,
    stage_bound_artifacts,
    validate_result,
)
from deployment_artifacts import _ARGO_PORT_RUNNER
from app.pipeline_compiler import compile_pipeline_nodes


def artifact(name="transcript", filename="transcript.json", **extra):
    return dict(
        name=name,
        filename=filename,
        kind="json",
        format="json",
        representation="file",
        schema={
            "type": "object",
            "required": ["transcript"],
            "properties": {"transcript": {"type": "string"}},
        },
        **extra,
    )


def write_json(root, spec, value):
    root.mkdir(parents=True, exist_ok=True)
    path = root / spec["filename"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


def test_only_declared_artifact_is_staged_and_fanout_is_identical(tmp_path):
    root = tmp_path / "published"
    spec = artifact()
    write_json(root, spec, {"transcript": "REDACTED"})
    write_json(
        root,
        artifact(filename=".inlumen-inputs/original.json"),
        {"transcript": "SECRET"},
    )
    binding = dict(
        source_dir=str(root),
        artifact=spec,
        source_node="anonymize",
        connection_id="anonymize->sentiment",
    )
    for target in ["sentiment", "archive"]:
        descriptors = stage_bound_artifacts([binding], tmp_path / target)
        assert len(descriptors) == 1
        assert descriptors[0]["source_node"] == "anonymize"
        assert descriptors[0]["connection_id"] == "anonymize->sentiment"
        assert (
            json.loads(Path(descriptors[0]["path"]).read_text())["transcript"]
            == "REDACTED"
        )
        assert not (tmp_path / target / ".inlumen-inputs").exists()


def test_bundle_and_join_are_one_descriptor_per_connection(tmp_path):
    root = tmp_path / "producer"
    (root / "model").mkdir(parents=True)
    (root / "model" / "weights.bin").write_bytes(b"weights")
    (root / "model" / "config.json").write_text("{}")
    bundle = dict(
        name="model", filename="model", kind="directory", representation="directory"
    )
    assert len(validate_result(root, [bundle])) == 1
    other = tmp_path / "other"
    spec = artifact()
    write_json(other, spec, {"transcript": "text"})
    result = stage_bound_artifacts(
        [
            dict(source_dir=str(root), artifact=bundle, connection_id="model->join"),
            dict(source_dir=str(other), artifact=spec, connection_id="data->join"),
        ],
        tmp_path / "join",
    )
    assert len(result) == 2
    assert Path(result[0]["path"]).is_dir()
    assert (Path(result[0]["path"]) / "weights.bin").read_bytes() == b"weights"


@pytest.mark.parametrize(
    "case", ["missing", "multiple", "extra", "schema", "wrong_type", "escaping_link"]
)
def test_publication_rejects_contract_violations(tmp_path, case):
    spec = artifact()
    path = write_json(tmp_path, spec, {"transcript": "valid"})
    actual = None
    if case == "missing":
        path.unlink()
    elif case == "multiple":
        actual = [spec, spec]
    elif case == "extra":
        (tmp_path / "log.txt").write_text("internal")
    elif case == "schema":
        path.write_text('{"transcript": 42}')
    elif case == "wrong_type":
        path.unlink()
        path.mkdir()
    elif case == "escaping_link":
        path.unlink()
        path.symlink_to("/etc/hosts")
    with pytest.raises(ArtifactContractError):
        validate_result(tmp_path, [spec], actual, producer="transcribe")


def test_collision_fails_without_overwriting_inputs(tmp_path):
    spec = artifact()
    source = tmp_path / "source"
    write_json(source, spec, {"transcript": "new"})
    target = tmp_path / "input"
    target.mkdir()
    (target / "previous.txt").write_text("previous")
    binding = dict(source_dir=str(source), artifact=spec)
    with pytest.raises(ArtifactContractError, match="collide"):
        stage_bound_artifacts([binding, binding], target)
    assert (target / "previous.txt").read_text() == "previous"


def test_compiled_anonymization_then_consumer_uses_only_bound_result(tmp_path):
    source = """import json
from pathlib import Path

def node_anonymize(inputs, output_dir, context):
    assert len(inputs) == 1
    data = json.loads(Path(inputs[0]['path']).read_text())
    data['transcript'] = data['transcript'].replace('Alice', '[NAME]')
    output = Path(output_dir) / 'anonymized.json'
    output.write_text(json.dumps(data))
    return [{'filename': 'anonymized.json', 'path': str(output)}]

def node_sentiment(inputs, output_dir, context):
    assert len(inputs) == 1
    data = json.loads(Path(inputs[0]['path']).read_text())
    assert data['transcript'] == '[NAME] is happy'
    output = Path(output_dir) / 'sentiment.json'
    output.write_text(json.dumps(data))
    return [{'filename': 'sentiment.json', 'path': str(output)}]
"""
    compiled = compile_pipeline_nodes(
        source, {"anonymize": "node_anonymize", "sentiment": "node_sentiment"}
    )
    previous_root = tmp_path / "transcribe"
    previous_spec = artifact()
    write_json(previous_root, previous_spec, {"transcript": "Alice is happy"})
    for node in compiled:
        folder = tmp_path / node.flow_id
        folder.mkdir()
        script = folder / "main.py"
        script.write_text(node.source)
        assert ".inlumen-inputs" not in node.source
        output_spec = artifact(
            name=node.flow_id,
            filename="anonymized.json"
            if node.flow_id == "anonymize"
            else "sentiment.json",
        )
        binding = dict(
            source_dir=str(previous_root),
            artifact=previous_spec,
            connection_id=node.flow_id,
        )
        contract = {"contract_id": CONTRACT_ID, "outputs": [output_spec], "inputs": []}
        # Execute the exported Argo runner, which launches the compiled package.
        output = folder / "output"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                _ARGO_PORT_RUNNER,
                json.dumps([sys.executable, str(script)]),
                json.dumps(["output"]),
                "[]",
                "[]",
                json.dumps(["output"]),
                json.dumps(contract),
                json.dumps([binding]),
            ],
            env={
                **os.environ,
                "PIPELINE_INPUT_DIR": str(folder / "input"),
                "PIPELINE_OUTPUT_DIR": str(output),
            },
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        previous_root = output / "output"
        previous_spec = output_spec
        assert list(previous_root.iterdir()) == [
            previous_root / output_spec["filename"]
        ]
    assert (
        json.loads((previous_root / "sentiment.json").read_text())["transcript"]
        == "[NAME] is happy"
    )


def test_old_argo_runner_contract_remains_supported(tmp_path):
    output = tmp_path / "output"
    code = "import os; from pathlib import Path; p=Path(os.environ['PIPELINE_OUTPUT_DIR']); p.mkdir(); (p/'legacy.txt').write_text('ok')"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _ARGO_PORT_RUNNER,
            json.dumps([sys.executable, "-c", code]),
            '["output"]',
            "[]",
            "[]",
        ],
        env={**os.environ, "PIPELINE_OUTPUT_DIR": str(output)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (output / "output" / "legacy.txt").read_text() == "ok"


def test_known_schema_conflict_is_rejected_before_execution():
    from artifact_runtime import validate_connection_contract

    expected = artifact()
    incompatible = artifact()
    incompatible["schema"]["properties"]["transcript"] = {"type": "integer"}
    with pytest.raises(ArtifactContractError, match="incompatible schema types"):
        validate_connection_contract(
            incompatible, expected, connection="producer->consumer"
        )


def test_retry_cannot_reuse_previous_attempt_output(tmp_path):
    output = tmp_path / "output"
    spec = artifact()
    contract = {"contract_id": CONTRACT_ID, "inputs": [], "outputs": [spec]}
    command = "import os; from pathlib import Path; (Path(os.environ['PIPELINE_OUTPUT_DIR'])/'transcript.json').write_text('{\"transcript\":\"valid\"}')"

    def execute(code):
        return subprocess.run(
            [
                sys.executable,
                "-c",
                _ARGO_PORT_RUNNER,
                json.dumps([sys.executable, "-c", code]),
                '["output"]',
                "[]",
                "[]",
                '["output"]',
                json.dumps(contract),
                "[]",
            ],
            env={
                **os.environ,
                "PIPELINE_INPUT_DIR": str(tmp_path / "input"),
                "PIPELINE_OUTPUT_DIR": str(output),
            },
            capture_output=True,
            text=True,
        )

    assert execute(command).returncode == 0
    # A successful process that forgot its result must fail, even after a previous success.
    result = execute("pass")
    assert result.returncode != 0
    assert "Artifact contract violation" in result.stderr
    assert not (output / "output" / "transcript.json").exists()


def test_publication_rollback_keeps_complete_previous_artifact(tmp_path, monkeypatch):
    import artifact_runtime

    staging, destination = tmp_path / "staging", tmp_path / "published"
    write_json(staging, artifact(), {"transcript": "new"})
    write_json(destination, artifact(), {"transcript": "old"})
    replace = artifact_runtime.os.replace

    def fail_publication(source, target):
        if Path(source) == staging:
            raise OSError("simulated publication failure")
        return replace(source, target)

    monkeypatch.setattr(artifact_runtime.os, "replace", fail_publication)
    with pytest.raises(OSError, match="simulated"):
        artifact_runtime.publish_artifact_directory(staging, destination)
    assert (
        json.loads((destination / "transcript.json").read_text())["transcript"] == "old"
    )


def test_publication_across_filesystems_stages_on_target_mount(tmp_path, monkeypatch):
    import artifact_runtime

    staging, destination = tmp_path / "work", tmp_path / "outputs" / "published"
    write_json(staging, artifact(), {"transcript": "complete"})
    original_stat = Path.stat

    def simulated_other_device(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if path == staging:
            values = list(result)
            values[2] += 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(Path, "stat", simulated_other_device)
    artifact_runtime.publish_artifact_directory(staging, destination)
    assert (
        json.loads((destination / "transcript.json").read_text())["transcript"]
        == "complete"
    )
    assert not staging.exists()
    assert list(destination.parent.iterdir()) == [destination]


def test_component_loader_numeric_ids_are_normalized_to_strings(tmp_path):
    from artifact_runtime import prepare_node_environment

    spec = artifact()
    source = tmp_path / "source"
    write_json(source, spec, {"transcript": "bound"})
    inputs = stage_bound_artifacts(
        [
            dict(
                source_dir=str(source),
                artifact=spec,
                source_node=2,
                connection_id=42,
                target_port=1,
            )
        ],
        tmp_path / "input",
    )
    assert (
        inputs[0]["source_node"],
        inputs[0]["connection_id"],
        inputs[0]["target_port"],
    ) == ("2", "42", "1")
    contract = {
        "contract_id": CONTRACT_ID,
        "inputs": [{**spec, "source_node": 2, "connection_id": 42}],
        "outputs": [spec],
    }
    env = prepare_node_environment(
        tmp_path / "input", tmp_path / "output", tmp_path / "work", inputs, contract
    )
    context = json.loads(Path(env["INLUMEN_CONTEXT_PATH"]).read_text())
    assert context["data_contract"]["inputs"][0]["connection_id"] == "42"


@pytest.mark.skipif(
    not os.getenv("INLUMEN_ARTIFACT_TEST_IMAGE"),
    reason="requires a built Dagster runtime image",
)
def test_real_dagster_loader_handoff_and_inactive_branch(tmp_path):
    """Run the same YAML component loading used by managed production execution."""
    from filesystem_runtime import filesystem_shell_component_source

    (tmp_path / "component.py").write_text(filesystem_shell_component_source())
    source = """import json
from pathlib import Path

def node_anonymize(inputs, output_dir, context):
    assert len(inputs) == 1 and inputs[0]['source_node'] == '2'
    assert inputs[0]['connection_id'] == '2:output->5:input'
    value = json.loads(Path(inputs[0]['path']).read_text())
    value['transcript'] = value['transcript'].replace('Alice', '[NAME]')
    path = Path(output_dir) / 'anonymized.json'
    path.write_text(json.dumps(value))
    return [{'filename': 'anonymized.json', 'path': str(path)}]

def node_sentiment(inputs, output_dir, context):
    assert len(inputs) == 1 and inputs[0]['source_node'] == '5'
    assert inputs[0]['connection_id'] == '5:output->3:input'
    value = json.loads(Path(inputs[0]['path']).read_text())
    assert value['transcript'] == '[NAME] is happy'
    path = Path(output_dir) / 'sentiment.json'
    path.write_text(json.dumps(value))
    return [{'filename': 'sentiment.json', 'path': str(path)}]
"""
    compiled = compile_pipeline_nodes(
        source, {"5": "node_anonymize", "3": "node_sentiment"}
    )
    spec = artifact()
    parent, parent_asset, parent_root = "2", "transcribe", "/fixture/published"
    for node in compiled:
        name = "anonymize" if node.flow_id == "5" else "sentiment"
        output_spec = artifact(
            name=name,
            filename="anonymized.json" if name == "anonymize" else "sentiment.json",
        )
        connection = f"{parent}:output->{node.flow_id}:input"
        (tmp_path / (name + ".py")).write_text(node.source)
        attrs = {
            "asset_key": name,
            "script_path": "/fixture/" + name + ".py",
            "upstream_assets": [parent_asset],
            "input_dir": "/fixture/" + name + "-input",
            "output_dir": "/fixture/" + name + "-output",
            "output_ports": ["output"],
            "data_contract": {
                "contract_id": CONTRACT_ID,
                "outputs": [output_spec],
                "inputs": [
                    {**spec, "source_node": parent, "connection_id": connection}
                ],
            },
            "artifact_bindings": [
                {
                    "source_dir": parent_root,
                    "source_port": "output",
                    "source_node": parent,
                    "connection_id": connection,
                    "artifact": spec,
                }
            ],
        }
        folder = tmp_path / "defs" / name
        folder.mkdir(parents=True)
        (folder / "defs.yaml").write_text(
            json.dumps(
                {
                    "type": "inlumen_dagster_project.components.shell_command.ShellCommand",
                    "attributes": attrs,
                }
            )
        )
        parent, parent_asset, parent_root, spec = (
            node.flow_id,
            name,
            attrs["output_dir"],
            output_spec,
        )
    (tmp_path / "defs" / "__init__.py").write_text("")
    (tmp_path / "check.py").write_text("""import json
from pathlib import Path
import dagster as dg

loaded = dg.load_from_defs_folder(project_root=Path('/workspace/dagster'))
@dg.asset(name='transcribe')
def transcribe(context):
    root = Path('/fixture/published') / context.run_id / 'output'
    root.mkdir(parents=True)
    (root/'transcript.json').write_text(json.dumps({'transcript': 'Alice is happy'}))
    (root/'.inlumen-inputs').mkdir()
    (root/'.inlumen-inputs'/'temporary.wav').write_bytes(b'temporary')
result = dg.materialize([transcribe, *loaded.assets])
assert result.success
value = json.loads((Path('/fixture/sentiment-output')/result.run_id/'output'/'sentiment.json').read_text())
assert value['transcript'] == '[NAME] is happy'
@dg.asset(name='transcribe', output_required=False)
def inactive():
    if False:
        yield dg.Output(None)
result = dg.materialize([inactive, *loaded.assets])
assert result.success
assert sum(e.event_type_value == 'STEP_SKIPPED' for e in result.all_events) == 2
""")
    command = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{tmp_path}:/fixture",
        "-v",
        f"{tmp_path}/component.py:/workspace/dagster/src/inlumen_dagster_project/components/shell_command.py:ro",
        "-v",
        f"{tmp_path}/defs:/workspace/dagster/src/inlumen_dagster_project/defs:ro",
        "-w",
        "/workspace/dagster",
        os.environ["INLUMEN_ARTIFACT_TEST_IMAGE"],
        "python",
        "/fixture/check.py",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
