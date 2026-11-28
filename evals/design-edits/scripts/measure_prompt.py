#!/usr/bin/env python3
"""Report the size of one request of the full design agent, by part (issue #195).

Builds the agent exactly as a design turn does, on the six-step canvas of
``inputs/delete-requests.json``, and measures what would be sent to the model:
the rules, the component palette, each tool and the request with the canvas.
No model is called and no API key is needed.

    python evals/design-edits/scripts/measure_prompt.py

Sizes are in tokens of tiktoken's ``o200k_base`` encoding when tiktoken is
installed, otherwise in characters (``--chars`` forces characters). The
provider's own count differs a little: it adds a few tokens per message and
per tool, and it may use another tokenizer. The sum here is therefore an
estimate of one call, good for comparing two versions of the prompt.
"""
import argparse
import json
from pathlib import Path

from autogen_ext.models.replay import ReplayChatCompletionClient

from inlumen.design.catalog import Catalog
from inlumen.design.context import _build_agent_task, _graph_for_agent_context
from inlumen.design.memory_store import InMemoryGraphStore
from inlumen.design.prompt import PIPELINE_EDITOR_SYSTEM_MESSAGE
from inlumen.design.team import build_design_team

ROOT = Path(__file__).resolve().parents[1]
MODEL_INFO = {
    "vision": False,
    "function_calling": True,
    "json_output": True,
    "family": "unknown",
    "structured_output": False,
}


def counter(force_chars: bool):
    """Return (unit, function that gives the size of a text)."""
    if not force_chars:
        try:
            import tiktoken

            encoding = tiktoken.get_encoding("o200k_base")
            return "tokens (o200k_base)", lambda text: len(encoding.encode(text))
        except Exception:  # not installed, or its vocabulary file is not available
            pass
    return "characters", len


def measure(suite: dict, request: str, size) -> dict:
    catalog = Catalog.default()
    store = InMemoryGraphStore(json.loads(json.dumps(suite["canvas"])), reusable_pipelines=catalog.reusable_pipelines)
    canvas = store.graph()
    # A stand-in client: it is never asked for a reply.
    client = ReplayChatCompletionClient([], model_info=MODEL_INFO)
    team = build_design_team(client, store, node_definitions=catalog.node_definitions)
    editor = team._participants[0]
    system_message = editor._system_messages[0].content
    palette = system_message[len(PIPELINE_EDITOR_SYSTEM_MESSAGE):]

    tools = {}
    for tool in editor._tools:
        schema = dict(tool.schema)
        tools[schema["name"]] = {
            "description": size(schema.get("description", "")),
            # The whole entry of the tool in the request: name, description, argument schema.
            "sent": size(json.dumps(schema, ensure_ascii=False)),
        }

    # The same arguments as inlumen.design.run.design gives to a turn.
    task = _build_agent_task(request, canvas, canvas)
    snapshot = json.dumps(_graph_for_agent_context(canvas), ensure_ascii=False)
    copies = task.count(snapshot)
    parts = {
        "rules": size(PIPELINE_EDITOR_SYSTEM_MESSAGE),
        "palette": size(palette),
        "tools": sum(tool["sent"] for tool in tools.values()),
        "request_and_canvas": size(task),
    }
    return {
        "request": request,
        "steps_on_canvas": len(canvas["nodes"]),
        "parts": parts,
        "total": sum(parts.values()),
        "canvas_copies_in_request": copies,
        "one_canvas_copy": size(snapshot),
        "tools": tools,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--suite", type=Path, default=ROOT / "inputs" / "delete-requests.json")
    parser.add_argument("--request", default="", help="default: the first request of the suite")
    parser.add_argument("--chars", action="store_true", help="count characters, not tokens")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args()

    suite = json.loads(args.suite.read_text())
    unit, size = counter(args.chars)
    report = {"unit": unit, **measure(suite, args.request or suite["requests"][0]["request"], size)}
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print(f"Unit: {unit}. Request: {report['request']!r} on {report['steps_on_canvas']} steps.\n")
    for name, value in report["parts"].items():
        print(f"{name:<28}{value:>7}")
    print(f"{'total':<28}{report['total']:>7}")
    print(
        f"\nThe request holds {report['canvas_copies_in_request']} copy/copies of the canvas "
        f"({report['one_canvas_copy']} each).\n"
    )
    print(f"{'tool':<28}{'sent':>7}{'description':>13}")
    for name, tool in sorted(report["tools"].items(), key=lambda item: -item[1]["sent"]):
        print(f"{name:<28}{tool['sent']:>7}{tool['description']:>13}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
