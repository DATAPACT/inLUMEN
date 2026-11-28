"""The full design agent's prompt after it was shortened (issue #195).

These tests check the text that is sent, not how a model reacts to it: the
canvas is sent once, and the shortened tool descriptions and rules still name
every field and rule they named before.
"""
import inspect
import json
import re

import pytest

pytest.importorskip("autogen_agentchat", reason="needs the design extra")

from inlumen.design.context import _build_agent_task, _graph_for_agent_context  # noqa: E402
from inlumen.design.guardrails import _guardrail_repair_task  # noqa: E402
from inlumen.design.memory_store import InMemoryGraphStore  # noqa: E402
from inlumen.design.prompt import PIPELINE_EDITOR_SYSTEM_MESSAGE  # noqa: E402
from inlumen.design.tools import build_pipeline_editor_tools  # noqa: E402

CANVAS_HEADING = "CURRENT VISIBLE CANVAS SNAPSHOT"
BACKEND_HEADING = "CURRENT BACKEND GRAPH SNAPSHOT"


def _canvas() -> dict:
    labels = ["Scanned documents", "OCR", "Language detection", "Translation", "Summarizer", "Summary report"]
    types = ["source", "task", "task", "task", "task", "destination"]
    return {
        "updated_at": "2026-11-28T10:00:00Z",
        "nodes": [
            {
                "id": str(index + 1),
                "position": {"x": 150 + 300 * index, "y": 150},
                "data": {"type": types[index], "label": label, "description": f"About {label}"},
            }
            for index, label in enumerate(labels)
        ],
        "edges": [{"source": str(index), "target": str(index + 1)} for index in range(1, 6)],
    }


def _snapshot_text(graph: dict) -> str:
    return json.dumps(_graph_for_agent_context(graph), ensure_ascii=False)


def _descriptions() -> dict[str, str]:
    tools = build_pipeline_editor_tools(InMemoryGraphStore())
    return {tool.__name__: inspect.getdoc(tool) or "" for tool in tools}


# --- the canvas is sent once -------------------------------------------------


def test_the_canvas_is_sent_once_when_the_stored_graph_is_the_same():
    canvas = _canvas()
    task = _build_agent_task("delete language detection", canvas, canvas)

    assert task.startswith("delete language detection\n\n")
    assert task.count(_snapshot_text(canvas)) == 1
    assert task.count(CANVAS_HEADING) == 1
    assert BACKEND_HEADING not in task
    assert task.count('"label": "Language detection"') == 1
    # The agent is still told that the stored graph matches what it sees.
    assert "the backend graph in Neo4j holds the same steps and connections" in task


def test_a_different_save_time_or_position_does_not_bring_the_second_copy_back():
    canvas = _canvas()
    stored = json.loads(json.dumps(canvas))
    stored["updated_at"] = "2026-11-28T10:00:05Z"
    stored["nodes"][2]["position"] = {"x": 0, "y": 0}

    task = _build_agent_task("delete language detection", canvas, stored)

    assert BACKEND_HEADING not in task
    # The copy that is kept is the canvas, with the positions the user sees.
    assert task.count(_snapshot_text(canvas)) == 1


def test_both_snapshots_are_sent_when_the_stored_graph_differs():
    canvas = _canvas()
    stored = json.loads(json.dumps(canvas))
    stored["nodes"][2]["data"]["label"] = "Identify language"

    task = _build_agent_task("delete language detection", canvas, stored)

    assert task.count(_snapshot_text(canvas)) == 1
    assert task.count(_snapshot_text(stored)) == 1
    assert task.index(CANVAS_HEADING) < task.index(BACKEND_HEADING)


def test_both_snapshots_are_sent_when_no_canvas_came_with_the_request():
    stored = _canvas()

    task = _build_agent_task("what are the current steps?", None, stored)

    assert BACKEND_HEADING in task
    assert task.count(_snapshot_text(stored)) == 1


def test_the_repair_request_holds_the_canvas_once():
    canvas = _canvas()

    task = _guardrail_repair_task("delete language detection", canvas, canvas, ["a validation error"])

    assert task.count(_snapshot_text(canvas)) == 1
    assert "a validation error" in task


# --- the shortened tool descriptions keep every field -------------------------

# Every field and port id that the descriptions named before they were shortened.
CREATE_STEPS_FIELDS = [
    "pipeline", "name", "description", "steps", "connections",
    "type", "label", "template", "expression", "id", "after", "after_flow_id",
    "source_port", "target_port", "inputs", "allow_fan_out",
    "reusable_pipeline_name", "reusable_pipeline_uid", "source", "target", "overview",
]
CREATE_STEPS_VALUES = [
    "source|task|destination|flow|subpipeline", "Condition", "Parallel Map", "Collect",
    "plugin:<id>", "when_true", "when_false", "value.is_valid == true",
]
CONNECT_STEPS_FIELDS = [
    "source_flow_id", "target_flow_id", "source_port", "target_port", "allow_fan_out", "connections",
]
PORT_IDS = ["data", "input", "output", "value", "when_true", "when_false", "items", "item"]


def _names(text: str, word: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(word)}(?![A-Za-z0-9_])", text) is not None


def test_create_steps_still_names_every_field():
    text = _descriptions()["create_steps"]

    assert [field for field in CREATE_STEPS_FIELDS if not _names(text, field)] == []
    assert [value for value in CREATE_STEPS_VALUES if value not in text] == []


def test_connect_steps_still_names_every_field_and_port_id():
    text = _descriptions()["connect_steps"]

    assert [field for field in CONNECT_STEPS_FIELDS if not _names(text, field)] == []
    assert [port for port in PORT_IDS if f"`{port}`" not in text] == []


@pytest.mark.parametrize(
    "tool, rule",
    [
        ("create_steps", "This is the only tool that creates steps"),
        ("create_steps", "in ONE call, never one call per step"),
        ("create_steps", "(required)"),
        ("create_steps", "required for a Condition"),
        ("create_steps", "an EARLIER step in this list"),
        ("create_steps", "required when it has several"),
        ("create_steps", "each input receives exactly one connection"),
        ("create_steps", "true only when a non-Flow predecessor must feed"),
        ("create_steps", "required for a Subpipeline"),
        ("create_steps", "A Source never follows another step"),
        ("create_steps", "appended to the graph's single tail"),
        ("create_steps", "never create configuration nodes"),
        ("create_steps", "ORDER MATTERS"),
        ("create_steps", "when_true target before its when_false target"),
        ("create_steps", "a joining Task after the branches it joins"),
        ("create_steps", 'Do not repeat an edge already created through "after"'),
        ("create_steps", "all-or-nothing"),
        ("create_steps", "no separate overview call is needed"),
        ("connect_steps", "in ONE call instead of one call per connection"),
        ("connect_steps", "applied in list order and the call is all-or-nothing"),
        ("connect_steps", "never include a component type or label prefix"),
        ("connect_steps", "A side with exactly one port may omit"),
        ("connect_steps", "the named inputs of a joining Task"),
        ("connect_steps", "public port ids returned by create_steps"),
        ("connect_steps", "updates that connection's handles"),
        ("connect_steps", "Use it to add every non-linear branch"),
        ("connect_steps", "only gain a second downstream target when allow_fan_out is explicitly true"),
        ("delete_step", "Branch points and merges are not bridged automatically"),
    ],
)
def test_the_rules_of_the_tool_descriptions_are_kept(tool, rule):
    # Compared without regard to line breaks and indentation.
    assert rule in " ".join(_descriptions()[tool].split())


def test_rules_cut_from_the_system_prompt_are_stated_by_a_tool():
    """Each rule removed from prompt.py is still sent, in a tool description."""
    rules = PIPELINE_EDITOR_SYSTEM_MESSAGE
    tools = _descriptions()

    # Removed from the rules because these tools say the same.
    assert "create_steps is the only tool that creates steps" not in rules
    assert "Connection port ids are local ids" not in rules
    assert "After delete_step on a branch point" not in rules
    assert "only tool that creates steps" in tools["create_steps"]
    assert "Port ids are local ids" in tools["connect_steps"]
    assert "use connect_steps explicitly after deleting" in tools["delete_step"]
    # Not stated by a tool, so they stay in the rules.
    assert "only when the user explicitly requests" in rules
    assert "Do not add shortcuts or bypass edges" in rules
    assert "send ONE create_steps call" in rules


def test_the_descriptions_do_not_grow_back():
    """A loose ceiling in characters; the measured sizes are in evals/design-edits."""
    tools = _descriptions()

    assert len(tools["create_steps"]) < 3500
    assert len(tools["connect_steps"]) < 1750
    assert len(PIPELINE_EDITOR_SYSTEM_MESSAGE) < 12800
