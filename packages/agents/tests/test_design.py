"""inlumen.design without any inLUMEN service: the in-memory store and design()."""

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("autogen_agentchat", reason="needs the design extra")

from autogen_core import FunctionCall  # noqa: E402
from autogen_core.models import CreateResult, RequestUsage  # noqa: E402
from autogen_ext.models.replay import ReplayChatCompletionClient  # noqa: E402

from inlumen.contracts.plugins import installed_plugin_manifest, use_plugin_source  # noqa: E402
from inlumen.contracts.project_ir import PROJECT_IR_SCHEMA_VERSION  # noqa: E402
from inlumen.design.catalog import Catalog  # noqa: E402
from inlumen.design.memory_store import InMemoryGraphStore  # noqa: E402
from inlumen.design.run import design  # noqa: E402
from inlumen.contracts.pipeline_graph_validation import validate_pipeline_graph  # noqa: E402
from inlumen.design.store_contract import SCENARIOS, observe, run_scenario  # noqa: E402
from inlumen.design.tools import build_pipeline_editor_tools  # noqa: E402

MODEL_INFO = {
    "vision": False, "function_calling": True, "json_output": True,
    "family": "unknown", "structured_output": False,
}


def _call(name: str, **arguments) -> CreateResult:
    return CreateResult(
        finish_reason="function_calls",
        content=[FunctionCall(id=f"call-{name}-{len(arguments)}", name=name, arguments=json.dumps(
            {"params": json.dumps(arguments)} if name != "overview" else {}))],
        usage=RequestUsage(prompt_tokens=100, completion_tokens=10),
        cached=False,
    )


def _reply(text: str) -> CreateResult:
    return CreateResult(
        finish_reason="stop", content=text,
        usage=RequestUsage(prompt_tokens=120, completion_tokens=20), cached=False,
    )


def _scripted(*results: CreateResult) -> ReplayChatCompletionClient:
    return ReplayChatCompletionClient(list(results), model_info=MODEL_INFO)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenarios_are_deterministic_in_memory(name):
    first = asyncio.run(observe(InMemoryGraphStore(), name))
    second = asyncio.run(observe(InMemoryGraphStore(), name))
    assert first == second


def test_branching_layout_and_rejections():
    outputs, graph = asyncio.run(run_scenario(InMemoryGraphStore(), "branching"))
    positions = {node["id"]: (node["position"]["x"], node["position"]["y"]) for node in graph["nodes"]}
    # Condition branches are stacked above (when_true) and below (when_false).
    assert positions["3"] == (600.0, -180.0)
    assert positions["4"] == (600.0, 180.0)
    assert "different downstream target" in outputs[7]["error"]
    assert "No connection matched" in outputs[10]["error"]
    handles = {(e["source"], e["sourceHandle"], e["target"]) for e in graph["edges"]}
    assert ("2", "when_false", "4") in handles and ("2", "when_true", "3") in handles


def test_insert_and_delete_rewire_and_shift_the_chain():
    outputs, _ = asyncio.run(run_scenario(InMemoryGraphStore(), "insert_delete"))
    inserted = outputs[3][0]["step"]
    assert (inserted["mode"], inserted["after_flow_id"], inserted["before_flow_id"]) == ("between", "1", "2")
    assert inserted["shifted_flow_ids"] == ["2", "3"]
    deleted = outputs[8][0]["deleted_step"]
    assert deleted["chain_reconnected"] is True
    assert outputs[-2][0]["pipeline"]["deleted_step_count"] == 3
    assert outputs[-1][0]["step"] is None  # an empty pipeline remains


def test_restore_graph_round_trips_the_canvas_graph():
    store = InMemoryGraphStore()
    outputs, graph = asyncio.run(run_scenario(store, "restore"))
    labels = sorted(node["data"]["label"] for node in graph["nodes"])
    assert labels == ["Escalate", "Queue", "Tickets", "Urgent?"]
    assert {(e["source"], e["target"]) for e in graph["edges"]} == {("1", "2"), ("2", "3"), ("2", "7")}
    tickets = next(node for node in graph["nodes"] if node["id"] == "1")
    assert tickets["data"]["files"] == ["tickets.csv"]


def test_design_builds_a_pipeline_and_exports_project_json():
    model = _scripted(
        _call("create_steps", type="source", label="Orders", description="Order table"),
        _call("create_steps", type="task", label="Price orders", description="Compute order totals"),
        _call("create_steps", type="destination", label="Priced orders", description="CSV with totals"),
        _reply("I created Orders -> Price orders -> Priced orders."),
    )
    events = []
    result = asyncio.run(design("Read the orders, price them and save the result", model=model,
                                name="Orders", events=events.append))

    assert result.accepted, result.sync
    assert [n["data"]["label"] for n in sorted(result.graph["nodes"], key=lambda n: n["id"])] == [
        "Orders", "Price orders", "Priced orders"]
    assert result.project["schema_version"] == PROJECT_IR_SCHEMA_VERSION
    assert [(c["from"]["port"], c["to"]["port"]) for c in result.project["pipeline"]["connections"]] == [
        ("data", "input"), ("output", "data")]
    assert result.usage == {"requests": 4, "prompt_tokens": 420, "completion_tokens": 50}
    assert events == result.requests and all(e["type"] == "model_request" for e in events)
    assert result.catalog_fingerprint == Catalog.default().fingerprint()


def test_design_rejects_an_invalid_result_and_keeps_the_starting_graph():
    # A Source with nothing after it is incomplete; the repair round does nothing.
    model = _scripted(
        _call("create_steps", type="source", label="Orders", description="Order table"),
        _reply("Added a source."),
        _reply("Nothing more to do."),
    )
    result = asyncio.run(design("Read the orders, price them and save the result", model=model))
    assert not result.accepted
    assert result.repaired
    assert result.graph["nodes"] == []


def test_a_catalog_scopes_its_plugins_to_the_design_run():
    example = Path(__file__).resolve().parents[3] / "examples" / "plugins" / "text-anonymizer.plugin.json"
    if not example.is_file():
        pytest.skip("runs from an inLUMEN checkout")
    manifest = json.loads(example.read_text())
    catalog = Catalog(plugins=(manifest,))
    tools = {tool.__name__: tool for tool in build_pipeline_editor_tools(InMemoryGraphStore())}

    with use_plugin_source(catalog.plugin_source()):
        listing = json.loads(asyncio.run(tools["list_pipeline_components"]("{}")))
    assert [plugin["template"] for plugin in listing["plugins"]] == [f"plugin:{manifest['id']}"]
    # Outside the run the plugin is not installed.
    assert installed_plugin_manifest(manifest["id"]) is None
    assert json.loads(asyncio.run(tools["list_pipeline_components"]("{}")))["plugins"] == []
    assert catalog.fingerprint() != Catalog.default().fingerprint()


def test_a_stream_read_to_its_result_is_not_a_failure():
    from inlumen.design.run import RecordingModelClient

    final = CreateResult(finish_reason="stop", content="ok", cached=False,
                         usage=RequestUsage(prompt_tokens=3, completion_tokens=1))
    events = []
    client = RecordingModelClient(_scripted(final), events.append)

    async def read_until_result():
        stream = client.create_stream([])
        async for chunk in stream:
            if isinstance(chunk, CreateResult):
                break
        await stream.aclose()

    asyncio.run(read_until_result())
    assert len(events) == 1 and events[0]["error"] is None and events[0]["prompt_tokens"] == 3
    assert set(events[0]) >= {"model", "cost_usd", "latency_s"}


def _tools(store):
    return {tool.__name__: tool for tool in build_pipeline_editor_tools(store)}


def _layout(graph):
    return {node["data"]["label"]: (node["position"]["x"], node["position"]["y"]) for node in graph["nodes"]}


def _edges(graph):
    return {(e["source"], e["target"], e.get("sourceHandle"), e.get("targetHandle")) for e in graph["edges"]}


BATCH_STEPS = [
    {"id": "customers", "type": "source", "label": "Customers", "description": "Customer table"},
    {"id": "orders", "type": "source", "label": "Orders", "description": "Order table"},
    {"id": "join", "type": "task", "label": "Join", "description": "Join orders to customers",
     "inputs": ["customers", "orders"], "after": "customers"},
    {"id": "check", "type": "flow", "template": "Condition", "label": "Valid?",
     "description": "Route valid rows", "expression": "value.is_valid == true", "after": "join"},
    {"id": "priced", "type": "destination", "label": "Priced", "description": "Valid rows",
     "after": "check", "source_port": "when_true"},
    {"id": "rejected", "type": "destination", "label": "Rejected", "description": "Invalid rows",
     "after": "check", "source_port": "when_false"},
]


def test_create_steps_places_a_batch_exactly_like_one_call_per_step():
    batch_store = InMemoryGraphStore()
    result = json.loads(asyncio.run(_tools(batch_store)["create_steps"](json.dumps({
        "steps": BATCH_STEPS,
        "connections": [{"source": "orders", "target": "join", "target_port": "orders"}],
    }))))
    assert [(step["id"], step["flow_id"]) for step in result["steps"]] == [
        ("customers", "1"), ("orders", "2"), ("join", "3"), ("check", "4"), ("priced", "5"), ("rejected", "6")]
    assert len(result["connections"]) == 1

    serial_store = InMemoryGraphStore()
    serial = _tools(serial_store)
    flow_ids = {}
    for step in BATCH_STEPS:
        params = {key: value for key, value in step.items() if key not in {"id", "after"}}
        if "after" in step:
            params["after_flow_id"] = flow_ids[step["after"]]
        row = json.loads(asyncio.run(serial["create_steps"](json.dumps(params))))  # one step, no list
        flow_ids[step["id"]] = row["steps"][0]["flow_id"]
    asyncio.run(serial["connect_steps"](json.dumps(
        {"source_flow_id": "2", "target_flow_id": "3", "target_port": "orders"})))

    batch_graph = asyncio.run(batch_store.get_graph())
    serial_graph = asyncio.run(serial_store.get_graph())
    assert _layout(batch_graph) == _layout(serial_graph)
    assert _edges(batch_graph) == _edges(serial_graph)
    assert _layout(batch_graph) == {
        "Customers": (0.0, 0.0), "Orders": (0.0, 220.0), "Join": (300.0, 0.0), "Valid?": (600.0, 0.0),
        "Priced": (900.0, -180.0), "Rejected": (900.0, 180.0)}


def test_create_steps_rejects_a_step_listed_before_its_predecessor_without_writing():
    store = InMemoryGraphStore()
    with pytest.raises(RuntimeError, match="not the id of an earlier step"):
        asyncio.run(_tools(store)["create_steps"](json.dumps({"steps": [
            {"id": "price", "type": "task", "label": "Price", "description": "x", "after": "orders"},
            {"id": "orders", "type": "source", "label": "Orders", "description": "x"},
        ]})))
    assert asyncio.run(store.get_graph())["nodes"] == []


def test_create_steps_puts_the_graph_back_when_a_later_entry_is_rejected():
    store = InMemoryGraphStore()
    tools = _tools(store)
    asyncio.run(tools["create_steps"](json.dumps({"type": "source", "label": "Orders", "description": "x"})))
    before = asyncio.run(store.get_graph())
    with pytest.raises(RuntimeError, match=r"steps\[2\] \(Again\).*Nothing was created"):
        asyncio.run(tools["create_steps"](json.dumps({"steps": [
            {"id": "price", "type": "task", "label": "Price", "description": "x", "after_flow_id": "1"},
            {"id": "out", "type": "destination", "label": "Out", "description": "x", "after": "price"},
            # A Task may not fan out without allow_fan_out.
            {"type": "task", "label": "Again", "description": "x", "after": "price"},
        ]})))
    after = asyncio.run(store.get_graph())
    assert _layout(after) == _layout(before) and _edges(after) == _edges(before)


def test_create_steps_accepts_an_existing_flow_id_as_after():
    store = InMemoryGraphStore()
    tools = _tools(store)
    asyncio.run(tools["create_steps"](json.dumps({"type": "source", "label": "Orders", "description": "x"})))
    result = json.loads(asyncio.run(tools["create_steps"](json.dumps({"steps": [
        {"id": "price", "type": "task", "label": "Price", "description": "x", "after": "1"},
        {"type": "destination", "label": "Out", "description": "x", "after": "price"},
    ]}))))
    assert [step["flow_id"] for step in result["steps"]] == ["2", "3"]
    assert _layout(asyncio.run(store.get_graph())) == {
        "Orders": (0.0, 0.0), "Price": (300.0, 0.0), "Out": (600.0, 0.0)}


def _three_unconnected_branches(store):
    tools = _tools(store)
    asyncio.run(tools["create_steps"](json.dumps({"steps": [
        {"id": "a", "type": "source", "label": "A", "description": "x"},
        {"id": "b", "type": "source", "label": "B", "description": "x"},
        {"id": "c", "type": "source", "label": "C", "description": "x"},
        {"id": "join", "type": "task", "label": "Join", "description": "x",
         "inputs": ["a", "b", "c"], "after": "a"},
        {"type": "destination", "label": "Out", "description": "x", "after": "join"},
    ]})))
    return tools


def test_connect_steps_makes_several_connections_in_one_call():
    store = InMemoryGraphStore()
    tools = _three_unconnected_branches(store)
    result = json.loads(asyncio.run(tools["connect_steps"](json.dumps({"connections": [
        {"source_flow_id": "2", "target_flow_id": "4", "target_port": "b"},
        {"source": "3", "target": "4", "target_port": "c"},
    ]}))))
    assert [(c["source_flow_id"], c["target_port"]) for c in result["connections"]] == [("2", "b"), ("3", "c")]
    assert {("2", "4", "data", "b"), ("3", "4", "data", "c")} <= _edges(asyncio.run(store.get_graph()))


def test_connect_steps_puts_the_graph_back_when_a_later_connection_is_rejected():
    store = InMemoryGraphStore()
    tools = _three_unconnected_branches(store)
    before = _edges(asyncio.run(store.get_graph()))
    with pytest.raises(RuntimeError, match=r"connections\[1\].*Nothing was connected"):
        asyncio.run(tools["connect_steps"](json.dumps({"connections": [
            {"source_flow_id": "2", "target_flow_id": "4", "target_port": "b"},
            {"source_flow_id": "3", "target_flow_id": "4", "target_port": "nope"},
        ]})))
    assert _edges(asyncio.run(store.get_graph())) == before


def test_create_steps_designs_a_whole_pipeline_in_one_call():
    store = InMemoryGraphStore()
    tools = _tools(store)
    assert "create_step" not in tools
    result = json.loads(asyncio.run(tools["create_steps"](json.dumps({
        "pipeline": {"name": "Order checks", "description": "Route valid orders"},
        "steps": [
            {"id": "orders", "type": "source", "label": "Orders", "description": "x"},
            {"id": "check", "type": "flow", "template": "Condition", "label": "Valid?",
             "description": "x", "expression": "value.is_valid == true", "after": "orders"},
            {"type": "destination", "label": "Valid", "description": "x",
             "after": "check", "source_port": "when_true"},
            {"type": "destination", "label": "Rejected", "description": "x",
             "after": "check", "source_port": "when_false"},
        ],
    }))))
    graph = asyncio.run(store.get_graph())
    assert graph["pipeline"]["name"] == "Order checks"
    condition = next(node for node in graph["nodes"] if node["data"]["label"] == "Valid?")
    assert condition["data"]["param"] == {"expression": "value.is_valid == true"}
    assert validate_pipeline_graph(graph)["valid"]
    # The result carries the graph, so the agent needs no overview call to verify it.
    assert result["overview"] == json.loads(asyncio.run(store.overview()))


def test_create_steps_rejects_an_expression_outside_a_condition():
    with pytest.raises(RuntimeError, match="expression is only for a Flow step"):
        asyncio.run(_tools(InMemoryGraphStore())["create_steps"](json.dumps(
            {"type": "task", "label": "Price", "description": "x", "expression": "value.a == 1"})))


def test_design_tools_accept_params_as_a_string_an_object_or_the_bare_object():
    from autogen_core import CancellationToken

    from inlumen.design.team import TolerantFunctionTool

    step = {"type": "source", "label": "Orders", "description": "x"}
    for arguments in ({"params": json.dumps(step)}, {"params": step}, step):
        store = InMemoryGraphStore()
        tools = {tool.__name__: TolerantFunctionTool(tool) for tool in build_pipeline_editor_tools(store)}
        asyncio.run(tools["create_steps"].run_json(arguments, CancellationToken()))
        assert [node["data"]["label"] for node in asyncio.run(store.get_graph())["nodes"]] == ["Orders"]
        # Tools without a payload still run when a model sends nothing or an empty params.
        asyncio.run(tools["list_reusable_pipelines"].run_json({}, CancellationToken()))
        asyncio.run(tools["overview"].run_json({"params": {}}, CancellationToken()))


def test_create_steps_requires_a_condition_expression():
    with pytest.raises(RuntimeError, match='A Condition requires "expression"'):
        asyncio.run(_tools(InMemoryGraphStore())["create_steps"](json.dumps(
            {"type": "flow", "template": "Condition", "label": "Valid?", "description": "x"})))


def test_create_steps_keeps_the_pipeline_when_one_step_is_sent_without_the_list():
    store = InMemoryGraphStore()
    asyncio.run(_tools(store)["create_steps"](json.dumps({
        "type": "source", "label": "Orders", "description": "x",
        "pipeline": {"name": "Named", "description": "Reads orders"}})))
    graph = asyncio.run(store.get_graph())
    assert graph["pipeline"]["name"] == "Named"
    assert [node["data"]["label"] for node in graph["nodes"]] == ["Orders"]


FAILING_BATCH = [
    {"id": "orders", "type": "source", "label": "Orders", "description": "x"},
    {"id": "price", "type": "task", "label": "Price", "description": "x", "after": "orders"},
    {"id": "out", "type": "destination", "label": "Out", "description": "x", "after": "price"},
    # A Task may not fan out without allow_fan_out.
    {"type": "destination", "label": "Again", "description": "x", "after": "price"},
]


def test_a_failed_batch_puts_the_pipeline_name_back_and_reports_the_rollback():
    store = InMemoryGraphStore()
    rolled_back = []

    async def tool_call_rolled_back():
        rolled_back.append(True)

    store.tool_call_rolled_back = tool_call_rolled_back
    tools = _tools(store)
    asyncio.run(tools["create_pipeline"](json.dumps({"name": "Original", "description": "First"})))
    with pytest.raises(RuntimeError, match="Nothing was created") as raised:
        asyncio.run(tools["create_steps"](json.dumps({
            "pipeline": {"name": "Renamed", "description": "Second"}, "steps": FAILING_BATCH})))
    assert "were kept" not in str(raised.value)
    graph = asyncio.run(store.get_graph())
    assert (graph["pipeline"]["name"], graph["pipeline"]["description"]) == ("Original", "First")
    assert graph["nodes"] == []
    assert rolled_back == [True]


def test_a_failed_batch_says_so_when_the_new_pipeline_name_stays():
    store = InMemoryGraphStore()
    with pytest.raises(RuntimeError, match='The pipeline name and description from "pipeline" were kept'):
        asyncio.run(_tools(store)["create_steps"](json.dumps({
            "pipeline": {"name": "Renamed", "description": "Second"}, "steps": FAILING_BATCH})))
    assert asyncio.run(store.get_graph())["nodes"] == []


def test_every_tool_call_is_reported_to_a_store_that_keeps_provenance():
    from contextlib import contextmanager

    from autogen_core import CancellationToken

    from inlumen.design.team import TolerantFunctionTool

    store = InMemoryGraphStore()
    scopes, finished = [], []

    @contextmanager
    def tool_call(name):
        scopes.append(name)
        yield

    async def tool_call_finished(name, arguments, error):
        finished.append((name, bool(error)))

    store.tool_call, store.tool_call_finished = tool_call, tool_call_finished
    tools = {tool.__name__: TolerantFunctionTool(tool, store) for tool in build_pipeline_editor_tools(store)}
    step = {"type": "source", "label": "Orders", "description": "x"}
    asyncio.run(tools["overview"].run_json({}, CancellationToken()))
    asyncio.run(tools["overview"].run_json({}, CancellationToken()))  # a repeat is its own call
    asyncio.run(tools["create_steps"].run_json(step, CancellationToken()))
    with pytest.raises(RuntimeError):
        asyncio.run(tools["create_steps"].run_json({**step, "after_flow_id": "1"}, CancellationToken()))
    assert finished == [("overview", False), ("overview", False), ("create_steps", False), ("create_steps", True)]
    assert scopes[:2] == ["overview", "overview"]

