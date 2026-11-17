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


def test_a_failed_run_reports_the_end_of_its_log():
    from inlumen.codegen.sandbox import log_tail

    logs = ("pip output\n" * 500 + "KeyError: 'amount'").encode()
    excerpt = log_tail(logs, 200)
    assert excerpt.endswith("KeyError: 'amount'") and excerpt.startswith("[earlier output omitted]")
    assert log_tail(b"short", 200) == "short"


class Raw:
    """Answers each request with the next (content, finish_reason) pair."""

    name = "raw"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    async def complete(self, *, system_prompt, user_prompt, json_output, max_tokens):
        self.requests.append({"system_prompt": system_prompt, "max_tokens": max_tokens})
        content, finish_reason = self.replies.pop(0)
        return ModelReply(content=content, finish_reason=finish_reason)


def _ask(model):
    from inlumen.codegen.llm import generate_json
    from inlumen.codegen.model import use_model

    async def run():
        with use_model(model):
            return await generate_json(None, system_prompt="JSON", user_prompt="{}")

    return asyncio.run(run())


def test_a_cut_off_reply_is_requested_again_with_a_higher_limit():
    model = Raw(('{"pipeline_py": "def a(', "length"), ('{"ok": true}', "stop"))
    assert _ask(model) == {"ok": True}
    assert [item["max_tokens"] for item in model.requests] == [16384, 32768]


def test_a_reply_that_is_not_json_is_requested_again_with_the_error():
    model = Raw(("Sure! Here is the code", "stop"), ('{"ok": true}', "stop"))
    assert _ask(model) == {"ok": True}
    assert "invalid JSON" in model.requests[1]["system_prompt"]
    assert model.requests[0]["system_prompt"] == "JSON"


def test_a_reply_that_stays_unusable_fails_after_one_retry():
    from inlumen.codegen.llm import LLMGenerationError

    model = Raw(("{", "length"), ("{", "length"))
    with pytest.raises(LLMGenerationError, match="cut off"):
        _ask(model)
    assert len(model.requests) == 2


def _task_function(source, name):
    import ast

    tree = ast.parse(source)
    node = next(item for item in tree.body if getattr(item, "name", "") == name)
    return ast.get_source_segment(source, node)


def _break_task(prompt):
    """A valid draft whose task function (step 2) has the wrong signature."""
    payload = _valid(prompt)
    name = next(n["function_name"] for n in prompt["pipeline_plan"]["nodes"] if n["flow_id"] == "2")
    good = _task_function(payload["pipeline_py"], name)
    bad = good.replace("(inputs, output_dir, context)", "(inputs, output_dir)", 1)
    assert bad != good
    return {**payload, "pipeline_py": payload["pipeline_py"].replace(good, bad)}, name, good


def test_only_the_failing_step_is_repaired():
    seen = {}

    def repair(prompt):
        seen.update(prompt)
        name = prompt["repair_functions"][0]
        _payload, _name, good = _break_task({"pipeline_plan": prompt["pipeline_plan"]})
        return {"functions": {name: good}, "helpers_py": "", "requirements": [], "notes": []}

    model = Scripted(lambda prompt: _break_task(prompt)[0], repair)
    result = asyncio.run(generate(_graph(), model=model))
    assert result.accepted, result.validation
    assert model.operations == ["generate_canonical_pipeline", "repair_pipeline_nodes"]
    assert seen["repair_flow_ids"] == ["2"] and len(seen["repair_functions"]) == 1


def test_an_unusable_step_repair_falls_back_to_the_whole_pipeline():
    model = Scripted(lambda prompt: _break_task(prompt)[0], {"functions": {}}, _valid)
    result = asyncio.run(generate(_graph(), model=model))
    assert result.accepted, result.validation
    assert model.operations == [
        "generate_canonical_pipeline", "repair_pipeline_nodes", "repair_canonical_pipeline",
    ]


def test_replacing_a_function_keeps_the_rest_and_rejects_name_clashes():
    from inlumen.codegen.pipeline_compiler import PipelineCompilerError, replace_node_functions

    source = "import os\n\nLIMIT = 3\n\n\ndef a(x):\n    return 1\n\n\ndef b(x):\n    return 2  # kept\n"
    out = replace_node_functions(source, {"a": "def a(x):\n    return helper(x)"}, "def helper(x):\n    return x")
    assert "return 2  # kept" in out and out.index("def helper") < out.index("def a(x)")
    compile(out, "pipeline.py", "exec")
    with pytest.raises(PipelineCompilerError, match="redefines"):
        replace_node_functions(source, {"a": "def a(x):\n    return 1"}, "LIMIT = 9")
    with pytest.raises(PipelineCompilerError, match="exactly one definition"):
        replace_node_functions(source, {"a": "def c(x):\n    return 1"})


class Text:
    """Like Scripted, but each reply is the reply text itself."""

    name = "text"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []
        self.requests = []

    async def complete(self, *, system_prompt, user_prompt, json_output, max_tokens):
        prompt = json.loads(user_prompt)
        self.prompts.append(prompt)
        self.requests.append({"system_prompt": system_prompt, "json_output": json_output})
        reply = self.replies.pop(0)
        return ModelReply(content=reply(prompt) if callable(reply) else reply)


def _blocks(meta, code):
    return f"Here it is.\n```json\n{json.dumps(meta)}\n```\n\n```python\n{code}\n```\n"


def _valid_blocks(prompt):
    payload = _valid(prompt)
    return _blocks({"requirements": payload["requirements"], "notes": []}, payload["pipeline_py"])


def test_code_can_be_returned_outside_json():
    model = Text(_valid_blocks)
    result = asyncio.run(generate(_graph(), model=model, options={"reply_format": "code_block"}))
    assert result.accepted, result.validation
    assert model.requests[0]["json_output"] is False
    assert "```python block" in model.requests[0]["system_prompt"]
    assert "exactly one JSON object" not in model.requests[0]["system_prompt"]


def test_a_code_block_step_repair_returns_only_the_function():
    def broken(prompt):
        payload, _name, _good = _break_task(prompt)
        return _blocks({"requirements": []}, payload["pipeline_py"])

    def repair(prompt):
        _payload, _name, good = _break_task({"pipeline_plan": prompt["pipeline_plan"]})
        return _blocks({"requirements": []}, "import json\n\n\n" + good)

    model = Text(broken, repair)
    result = asyncio.run(generate(_graph(), model=model, options={"reply_format": "code_block"}))
    assert result.accepted, result.validation
    assert [p["operation"] for p in model.prompts] == ["generate_canonical_pipeline", "repair_pipeline_nodes"]
    # The system prompt is the same for the draft and the repair.
    assert model.requests[0]["system_prompt"] == model.requests[1]["system_prompt"]


def test_a_repair_is_told_about_earlier_attempts_after_the_stable_prefix():
    def broken(_prompt):
        return {"nodes": "not a list"}

    model = Text(*[lambda prompt: json.dumps(broken(prompt))] * 3)
    asyncio.run(generate(_graph(), model=model, options={"repair_attempts": 2}))
    first, second, third = model.prompts
    assert all(list(prompt)[:2] == ["pipeline_plan", "user_instruction"] for prompt in model.prompts)
    assert first["pipeline_plan"] == third["pipeline_plan"]
    assert "earlier_attempts" not in second
    assert [item["attempt"] for item in third["earlier_attempts"]] == [1]
    assert third["earlier_attempts"][0]["validation_errors"]


def _node_draft(context):
    """A valid draft for one node; a destination declares no output."""
    from inlumen.codegen.generator import fallback_script_payload
    from inlumen.codegen.schemas import GenerateNodeScriptRequest

    request = GenerateNodeScriptRequest.model_validate({"context": context})
    payload = fallback_script_payload(request)
    if not request.context.expected_outputs:
        payload["outputs"] = []
    return request.context.target_node.flow_id, payload


def _node_drafts(prompt, skip=()):
    drafts = {}
    for node in prompt["nodes"]:
        context = dict(node)
        context["graph"] = {**prompt["graph"], "upstream_nodes": context.pop("upstream_nodes"),
                            "downstream_nodes": context.pop("downstream_nodes")}
        context["pipeline"] = {**prompt["pipeline"], **context["pipeline"]}
        flow_id, payload = _node_draft(context)
        if flow_id not in skip:
            drafts[flow_id] = payload
    return {"nodes": drafts}


def _node_first(model, **options):
    options = {"generation_strategy": "node_first", "validation_mode": "static", **options}
    return asyncio.run(generate(_graph(), model=model, options=options))


def test_node_first_drafts_every_step_in_one_request():
    model = Scripted(_node_drafts)
    result = _node_first(model)
    assert result.accepted, result.validation
    assert model.operations == ["generate_node_runtimes"]
    assert sorted(result.packages) == ["1", "2", "3"]


def test_a_step_missing_from_the_batch_is_generated_on_its_own():
    def one(prompt):
        return _node_draft(prompt["context"])[1]

    model = Scripted(lambda prompt: _node_drafts(prompt, skip={"2"}), one)
    result = _node_first(model)
    assert result.accepted, result.validation
    assert model.operations == ["generate_node_runtimes", "generate_node_runtime"]


def test_node_first_can_still_ask_once_per_step():
    def one(prompt):
        return _node_draft(prompt["context"])[1]

    model = Scripted(one, one, one)
    result = _node_first(model, batch_node_drafts=False)
    assert result.accepted, result.validation
    assert model.operations == ["generate_node_runtime"] * 3


def test_a_batch_of_code_blocks_is_split_by_node_heading():
    from inlumen.codegen.llm import _node_batch_from_code_reply

    reply = ('```json\n{"nodes": {"2": {"requirements": ["pandas"]}}}\n```\n'
             "## node 2\n```python\nprint('two')\n```\n\n### node: 3\n```py\nprint('3')\n```\n")
    nodes = _node_batch_from_code_reply(reply)["nodes"]
    assert nodes == {"2": {"requirements": ["pandas"], "main_py": "print('two')\n"},
                     "3": {"main_py": "print('3')\n"}}
