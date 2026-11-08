"""inlumen.codegen without the codegen service: generate() with a scripted model."""

import asyncio
import json

import pytest

pytest.importorskip("pydantic", reason="needs the codegen extra")
pytest.importorskip("docker", reason="needs the codegen extra")

from inlumen.codegen.context import directory_sampler, pipeline_context  # noqa: E402
from inlumen.codegen.generator import deterministic_pipeline_payload  # noqa: E402
from inlumen.codegen.model import ModelReply  # noqa: E402
from inlumen.codegen.run import generate  # noqa: E402
from inlumen.codegen.sandbox import (  # noqa: E402
    DockerSandbox,
    NoSandbox,
    docker_validation_enabled,
    thread_limit_environment,
    use_sandbox,
)


def _graph(files=()):
    def node(flow_id, kind, label, description, **extra):
        return {"id": flow_id, "data": {"type": kind, "label": label, "description": description, **extra}}

    return {
        "nodes": [
            node("1", "source", "Orders", "Order table", files=list(files)),
            node("2", "task", "Total", "Sum the amount column"),
            node("3", "destination", "Report", "CSV with the total"),
        ],
        "edges": [
            {"id": "e1", "source": "1", "target": "2"},
            {"id": "e2", "source": "2", "target": "3"},
        ],
        "pipeline": {"name": "Orders"},
    }


class Scripted:
    """Answers each request with the next reply; a callable gets the parsed prompt."""

    name = "scripted"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.operations = []

    async def complete(self, *, system_prompt, user_prompt, json_output, max_tokens):
        prompt = json.loads(user_prompt)
        self.operations.append(prompt["operation"])
        reply = self.replies.pop(0)
        content = reply(prompt) if callable(reply) else reply
        return ModelReply(content=json.dumps(content), usage={"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.01})


def _valid(prompt):
    return deterministic_pipeline_payload(prompt["pipeline_plan"])


def test_generate_returns_one_task_package_per_step(tmp_path):
    model = Scripted(_valid)
    events = []
    result = asyncio.run(generate(_graph(), model=model, events=events.append))

    assert result.accepted, result.validation
    assert model.operations == ["generate_canonical_pipeline"]
    assert sorted(result.packages) == ["1", "2", "3"]
    assert "main.py" in result.packages["2"] and "inlumen.task.json" in result.packages["2"]
    assert result.usage == {"requests": 1, "prompt_tokens": 100, "completion_tokens": 20, "cost_usd": 0.01}
    assert events == result.requests and events[0]["model"] == "scripted"
    written = result.write_packages(tmp_path)
    assert (tmp_path / "2" / "main.py") in written


def test_runtime_constraints_reach_the_generator(monkeypatch):
    import inlumen.codegen.run as run_module

    seen = []
    real = run_module.generate_pipeline_script_bundles

    async def capture(request, **kwargs):
        seen.append(request.context.runtime_constraints)
        return await real(request, **kwargs)

    monkeypatch.setattr(run_module, "generate_pipeline_script_bundles", capture)
    runtime = {"allowed_packages": ["pandas"], "network_allowed": False}
    result = asyncio.run(generate(_graph(), model=Scripted(_valid), runtime=runtime))
    assert result.accepted, result.validation
    assert seen[0].allowed_packages == ["pandas"] and seen[0].network_allowed is False


def test_an_invalid_draft_is_repaired():
    model = Scripted({"nodes": "not a list"}, _valid)
    result = asyncio.run(generate(_graph(), model=model))
    assert result.accepted, result.validation
    assert model.operations == ["generate_canonical_pipeline", "repair_canonical_pipeline"]


def test_no_deterministic_fallback_unless_asked():
    def broken(_prompt):
        return {"nodes": "not a list"}

    result = asyncio.run(generate(_graph(), model=Scripted(broken, broken, broken), options={"repair_attempts": 2}))
    assert not result.accepted
    assert not any("deterministic fallback" in w for w in result.validation["warnings"])


def test_directory_sampler_describes_attached_files(tmp_path):
    (tmp_path / "1").mkdir()
    (tmp_path / "1" / "orders.csv").write_text("id,amount\n1,9.5\n2,3\n", encoding="utf-8")
    context = pipeline_context(_graph(["orders.csv"]), sample=directory_sampler(tmp_path))
    entry = context["graph"]["nodes"][0]["files"][0]
    assert entry["bucket"] == "files-step-id-1"
    assert entry["columns"] == ["id", "amount"]
    assert entry["sample"]["rows"][1] == {"id": "2", "amount": "3"}


def test_the_sandbox_is_chosen_per_run(monkeypatch):
    monkeypatch.setenv("CODEGEN_DOCKER_VALIDATION_ENABLED", "true")
    with use_sandbox(NoSandbox()):
        assert not docker_validation_enabled()
    assert docker_validation_enabled()
    monkeypatch.delenv("CODEGEN_DOCKER_VALIDATION_ENABLED")
    with use_sandbox(DockerSandbox()):
        assert docker_validation_enabled()


def test_options_objects_keep_the_app_defaults(monkeypatch):
    import inlumen.codegen.run as run_module
    from inlumen.codegen.schemas import GenerationOptions

    seen = []
    real = run_module.generate_pipeline_script_bundles

    async def capture(request, **kwargs):
        seen.append(request.options)
        return await real(request, **kwargs)

    monkeypatch.setattr(run_module, "generate_pipeline_script_bundles", capture)
    monkeypatch.setenv("CODEGEN_ALLOW_DETERMINISTIC_FALLBACK", "true")
    asyncio.run(generate(_graph(), model=Scripted(_valid), options=GenerationOptions(repair_attempts=3)))
    options = seen[0]
    assert options.repair_attempts == 3
    assert options.validation_mode == "pipeline_sample" and options.allow_deterministic_fallback is False


def test_an_http_model_without_a_key_fails_clearly():
    from inlumen.codegen.llm import LLMGenerationError
    from inlumen.codegen.run import http_model

    with pytest.raises(LLMGenerationError, match="API key is missing"):
        asyncio.run(generate(_graph(), model=http_model("m", base_url="http://127.0.0.1:9/v1", api_key="")))


def test_step_folders_are_found_whatever_their_case(tmp_path):
    (tmp_path / "LoadOrders").mkdir()
    (tmp_path / "LoadOrders" / "orders.csv").write_text("id\n1\n", encoding="utf-8")
    graph = {"nodes": [{"id": "LoadOrders", "data": {"type": "source", "label": "Orders", "files": ["orders.csv"]}}],
             "edges": []}
    entry = pipeline_context(graph, sample=directory_sampler(tmp_path))["graph"]["nodes"][0]["files"][0]
    assert entry["columns"] == ["id"]


def test_generation_refuses_an_interpreter_that_breaks_3_11_code(monkeypatch):
    import inlumen.codegen.run as run_module

    assert run_module.unparse_is_portable()  # this test interpreter is fine
    monkeypatch.setattr(run_module, "unparse_is_portable", lambda: False)
    with pytest.raises(RuntimeError, match="Python 3.11 runtime cannot parse"):
        asyncio.run(generate(_graph(), model=Scripted(_valid)))


def test_sandbox_caps_numeric_thread_pools_at_its_cpus():
    # numpy's OpenBLAS sizes its pool from the host's CPUs (64 threads on a 72-CPU host),
    # which exceeds the sandbox's process limit unless capped.
    env = thread_limit_environment(2)
    assert env["OPENBLAS_NUM_THREADS"] == "2"
    assert env["OMP_NUM_THREADS"] == env["MKL_NUM_THREADS"] == env["NUMEXPR_NUM_THREADS"] == "2"
    assert thread_limit_environment(0.5)["OPENBLAS_NUM_THREADS"] == "1"
