#!/usr/bin/env python3
"""Send edits that are not valid alone to the design agent and count model calls.

A lone disconnect, a Flow behavior switch, or deleting the only destination
cannot pass validation (issue #194). The agent should leave the canvas
unchanged and explain, in at most two model calls; the report counts both. The compound requests in
the suite contain such an edit as one step and must still change the canvas.

Calls a real model, so it needs an API key. Runs on an in-memory canvas.

    OPENAI_API_KEY=... python evals/design-edits/scripts/run_edits_not_valid_alone.py \\
        --base-url https://openrouter.ai/api/v1 --model openai/gpt-oss-120b
"""
import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

from inlumen.design.run import design, openai_model_client

ROOT = Path(__file__).resolve().parents[1]


def labels(graph: dict) -> list[str]:
    return sorted(node["data"]["label"] for node in graph["nodes"])


def links(graph: dict) -> list[tuple]:
    return sorted(
        (edge["source"], edge.get("sourceHandle"), edge["target"], edge.get("targetHandle"))
        for edge in graph["edges"]
    )


def behaviors(graph: dict) -> list[tuple]:
    return sorted((node["id"], node["data"].get("template_label")) for node in graph["nodes"])


def check(case: dict, canvas: dict, start: dict, result, seconds: float) -> dict:
    before, after = set(labels(canvas)), set(labels(result.graph))
    unchanged = (
        labels(result.graph) == labels(start)
        and links(result.graph) == links(start)
        and behaviors(result.graph) == behaviors(start)
    )
    if case["expect"] == "declined":
        # Leaving the canvas as it was, with an explanation, is the expected
        # answer. A different edit that passes validation is also acceptable.
        passed = unchanged or bool(result.accepted)
    else:
        passed = (
            bool(result.accepted)
            and not unchanged
            and before - after == set(case["removed"])
            and len(after - before) == case["added"]
        )
    return {
        "id": case["id"],
        "kind": case["kind"],
        "expect": case["expect"],
        "request": case["request"],
        "passed": passed,
        "accepted": bool(result.accepted),
        "unchanged": unchanged,
        "declined": bool(result.sync.get("declined_edit")),
        "repaired": bool(result.repaired),
        "model_calls": result.usage["requests"],
        "prompt_tokens": result.usage["prompt_tokens"],
        "seconds": round(seconds, 1),
        "reply": result.message[:300],
    }


async def run(args: argparse.Namespace) -> list[dict]:
    suite = json.loads(args.suite.read_text())
    rows = []
    for repetition in range(args.repeat):
        for case in suite["requests"]:
            if args.kind and case["kind"] != args.kind:
                continue
            model = openai_model_client(
                args.model, base_url=args.base_url, api_key=os.environ["OPENAI_API_KEY"],
                **({"reasoning_effort": args.reasoning_effort} if args.reasoning_effort else {}),
            )
            started = time.monotonic()
            result = await design(case["request"], model=model, graph=suite["canvas"])
            row = check(case, suite["canvas"], _loaded(suite["canvas"]), result, time.monotonic() - started)
            rows.append({"repetition": repetition + 1, **row})
            print(
                f"{'ok  ' if row['passed'] else 'FAIL'} {case['id']:<14} calls: {row['model_calls']:>2}"
                f"  repaired: {str(row['repaired']):<5}  {row['seconds']:>5}s"
            )
    return rows


def _loaded(canvas: dict) -> dict:
    """The canvas as the in-memory store returns it (default handles filled in)."""
    from inlumen.design.memory_store import InMemoryGraphStore

    return InMemoryGraphStore(json.loads(json.dumps(canvas))).graph()


def summary(rows: list[dict]) -> dict:
    out = {}
    for kind in sorted({row["kind"] for row in rows}):
        group = [row for row in rows if row["kind"] == kind]
        calls = [row["model_calls"] for row in group]
        out[kind] = {
            "requests": len(group),
            "passed": sum(row["passed"] for row in group),
            "unchanged": sum(row["unchanged"] for row in group),
            "within_two_calls": sum(call <= 2 for call in calls),
            "median_calls": statistics.median(calls),
            "max_calls": max(calls),
            "repair_rounds": sum(row["repaired"] for row in group),
            "median_seconds": statistics.median(row["seconds"] for row in group),
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default="openai/gpt-oss-120b")
    parser.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", "https://openrouter.ai/api/v1"))
    parser.add_argument("--reasoning-effort", default="low", help="empty string to leave it unset")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--kind", default="", help="only run requests of this kind (disconnect, flow, delete, compound)")
    parser.add_argument("--suite", type=Path, default=ROOT / "inputs" / "edits-not-valid-alone.json")
    parser.add_argument("--output", type=Path, default=ROOT / "results" / "edits-not-valid-alone.json")
    args = parser.parse_args()
    if not os.environ.get("OPENAI_API_KEY"):
        print("Set OPENAI_API_KEY to the key of the provider behind --base-url.", file=sys.stderr)
        return 2
    rows = asyncio.run(run(args))
    report = {"model": args.model, "summary": summary(rows), "rows": rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["summary"], indent=2))
    print(f"report: {args.output}")
    return 0 if all(row["passed"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
