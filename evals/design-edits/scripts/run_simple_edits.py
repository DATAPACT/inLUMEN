#!/usr/bin/env python3
"""Count model calls and prompt tokens for simple edits (issue #195).

Sends plain delete, clear and connect requests to the design agent on an
in-memory canvas, plus a few other requests that the quick path must hand to
the full agent. Run it twice to compare: once as it is, once with
INLUMEN_DESIGN_FAST_EDITS=0, which switches the quick path off.

Calls a real model, so it needs an API key.

    OPENAI_API_KEY=... python evals/design-edits/scripts/run_simple_edits.py \\
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


def label_of(graph: dict) -> dict[str, str]:
    return {node["id"]: node["data"]["label"] for node in graph["nodes"]}


def check(case: dict, canvas: dict, result, seconds: float) -> dict:
    before, after = set(label_of(canvas).values()), set(label_of(result.graph).values())
    names = label_of(result.graph)
    links = {(names.get(edge["source"]), names.get(edge["target"])) for edge in result.graph["edges"]}
    if case.get("cleared"):
        passed = bool(result.accepted) and not result.graph["nodes"]
    elif case.get("unchanged"):
        passed = before == after and not result.sync.get("graph_changed")
    else:
        passed = (
            bool(result.accepted)
            and before - after == set(case["removed"])
            and len(after - before) == case["added"]
            and (not case.get("connected") or tuple(case["connected"]) in links)
        )
    return {
        "id": case["id"],
        "kind": case["kind"],
        "request": case["request"],
        "passed": passed,
        "accepted": bool(result.accepted),
        "quick": result.sync.get("fast_edit") or "",
        "repaired": bool(result.repaired),
        "model_calls": result.usage["requests"],
        "prompt_tokens": result.usage["prompt_tokens"],
        "completion_tokens": result.usage["completion_tokens"],
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
            row = check(case, suite["canvas"], result, time.monotonic() - started)
            rows.append({"repetition": repetition + 1, **row})
            print(
                f"{'ok  ' if row['passed'] else 'FAIL'} {case['id']:<10} calls: {row['model_calls']:>2}"
                f"  prompt tokens: {row['prompt_tokens']:>6}  {row['seconds']:>5}s  {row['quick']}"
            )
    return rows


def summary(rows: list[dict]) -> dict:
    out = {}
    for kind in sorted({row["kind"] for row in rows}):
        group = [row for row in rows if row["kind"] == kind]
        out[kind] = {
            "requests": len(group),
            "passed": sum(row["passed"] for row in group),
            "quick": sum(bool(row["quick"]) for row in group),
            "median_calls": statistics.median(row["model_calls"] for row in group),
            "median_prompt_tokens": statistics.median(row["prompt_tokens"] for row in group),
            "total_prompt_tokens": sum(row["prompt_tokens"] for row in group),
            "median_seconds": statistics.median(row["seconds"] for row in group),
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default="openai/gpt-oss-120b")
    parser.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", "https://openrouter.ai/api/v1"))
    parser.add_argument("--reasoning-effort", default="low", help="empty string to leave it unset")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--kind", default="", help="only run requests of this kind (delete, clear, connect, other)")
    parser.add_argument("--suite", type=Path, default=ROOT / "inputs" / "simple-edits.json")
    parser.add_argument("--output", type=Path, default=ROOT / "results" / "simple-edits.json")
    args = parser.parse_args()
    if not os.environ.get("OPENAI_API_KEY"):
        print("Set OPENAI_API_KEY to the key of the provider behind --base-url.", file=sys.stderr)
        return 2
    rows = asyncio.run(run(args))
    quick_path = os.environ.get("INLUMEN_DESIGN_FAST_EDITS", "1")
    report = {"model": args.model, "quick_path": quick_path, "summary": summary(rows), "rows": rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["summary"], indent=2))
    print(f"report: {args.output}")
    return 0 if all(row["passed"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
