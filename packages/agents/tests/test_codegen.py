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
