#!/usr/bin/env python3
"""Send each delete request to the design agent and check that the step is gone.

Calls a real model, so it needs an API key and costs a little (about 4 model
calls per request). Runs on an in-memory canvas; no workspace is touched.

    OPENAI_API_KEY=... python evals/design-edits/scripts/run_delete_requests.py \\
        --base-url https://openrouter.ai/api/v1 --model openai/gpt-oss-120b
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from inlumen.design.run import design, openai_model_client

ROOT = Path(__file__).resolve().parents[1]


def check(case: dict, canvas: dict, result) -> dict:
    """One row of the report: was exactly the named step removed?"""
    before = {node["data"]["label"] for node in canvas["nodes"]}
    after = {node["data"]["label"] for node in result.graph["nodes"]}
    return {
        "id": case["id"],
        "request": case["request"],
        "passed": bool(result.accepted) and before - after == {case["deleted_label"]} and after <= before,
        "accepted": bool(result.accepted),
        "removed": sorted(before - after),
        "added": sorted(after - before),
        "model_calls": result.usage["requests"],
        "reply": result.message[:200],
    }


async def run(args: argparse.Namespace) -> list[dict]:
    suite = json.loads(args.suite.read_text())
    rows = []
    for repetition in range(args.repeat):
        for case in suite["requests"]:
            model = openai_model_client(
                args.model, base_url=args.base_url, api_key=os.environ["OPENAI_API_KEY"],
                **({"reasoning_effort": args.reasoning_effort} if args.reasoning_effort else {}),
            )
            result = await design(case["request"], model=model, graph=suite["canvas"])
            rows.append({"repetition": repetition + 1, **check(case, suite["canvas"], result)})
            print(f"{'ok  ' if rows[-1]['passed'] else 'FAIL'} {case['id']} (calls: {rows[-1]['model_calls']})")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default="openai/gpt-oss-120b")
    parser.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", "https://openrouter.ai/api/v1"))
    parser.add_argument("--reasoning-effort", default="low", help="empty string to leave it unset")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--suite", type=Path, default=ROOT / "inputs" / "delete-requests.json")
    parser.add_argument("--output", type=Path, default=ROOT / "results" / "delete-requests.json")
    args = parser.parse_args()
    if not os.environ.get("OPENAI_API_KEY"):
        print("Set OPENAI_API_KEY to the key of the provider behind --base-url.", file=sys.stderr)
        return 2
    rows = asyncio.run(run(args))
    passed = sum(row["passed"] for row in rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"model": args.model, "passed": passed, "total": len(rows), "rows": rows}, indent=2) + "\n")
    print(f"{passed} of {len(rows)} passed; report: {args.output}")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
