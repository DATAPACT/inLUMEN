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
        _call("create_step", type="source", label="Orders", description="Order table"),
        _call("create_step", type="task", label="Price orders", description="Compute order totals"),
        _call("create_step", type="destination", label="Priced orders", description="CSV with totals"),
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
        _call("create_step", type="source", label="Orders", description="Order table"),
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
