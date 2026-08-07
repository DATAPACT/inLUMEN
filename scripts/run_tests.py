#!/usr/bin/env python3
"""Run the inLUMEN regression suite from one cross-platform entry point."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Check:
    name: str
    command: tuple[str, ...]
    cwd: Path


def executable(name: str) -> str:
    candidate = f"{name}.cmd" if os.name == "nt" else name
    return shutil.which(candidate) or candidate


def checks_for(component: str) -> list[Check]:
    if component == "backend":
        return [
            Check(
                "backend unit and API tests",
                (
                    sys.executable,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tests",
                    "-p",
                    "test_*.py",
                    "-v",
                ),
                ROOT / "backend",
            )
        ]
    if component == "deployment-validation":
        return [
            Check(
                "deployment validation tests",
                (
                    sys.executable,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tests",
                    "-p",
                    "test_*.py",
                    "-v",
                ),
                ROOT / "deployment_validation",
            )
        ]
    if component == "frontend":
        npm = executable("npm")
        return [
            Check("frontend unit tests", (npm, "run", "test:run"), ROOT / "frontend"),
            Check("frontend lint", (npm, "run", "lint"), ROOT / "frontend"),
            Check("frontend type check", (npm, "run", "typecheck"), ROOT / "frontend"),
            Check("frontend production build", (npm, "run", "build"), ROOT / "frontend"),
        ]
    if component == "compose":
        docker = executable("docker")
        return [
            Check(
                "development Compose configuration",
                (docker, "compose", "config", "--quiet"),
                ROOT,
            ),
            Check(
                "production Compose configuration",
                (
                    docker,
                    "compose",
                    "-f",
                    "docker-compose-prod.yml",
                    "config",
                    "--quiet",
                ),
                ROOT,
            ),
        ]
    raise ValueError(f"Unknown test component: {component}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--component",
        action="append",
        choices=("backend", "deployment-validation", "frontend", "compose"),
        help="Run only this component. Repeat the option to select more than one.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    components = args.component or [
        "backend",
        "deployment-validation",
        "frontend",
        "compose",
    ]
    failures: list[str] = []

    for component in components:
        for check in checks_for(component):
            print(f"\n=== {check.name} ===", flush=True)
            try:
                result = subprocess.run(check.command, cwd=check.cwd, check=False)
            except FileNotFoundError as error:
                print(f"Unable to start {check.command[0]}: {error}", file=sys.stderr)
                failures.append(check.name)
                continue
            if result.returncode != 0:
                failures.append(check.name)

    if failures:
        print("\nFailed checks:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print("\nAll selected checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
