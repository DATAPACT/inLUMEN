#!/usr/bin/env python3
"""Run the inLUMEN regression suite from one cross-platform entry point."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Check:
    name: str
    command: tuple[str, ...]
    cwd: Path
    environment: dict[str, str] | None = None


def executable(name: str) -> str:
    candidate = f"{name}.cmd" if os.name == "nt" else name
    return shutil.which(candidate) or candidate


def project_python(project: Path) -> str:
    """Prefer a component's managed virtualenv when it is available locally."""
    relative = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    candidate = project / ".venv" / relative
    return str(candidate) if candidate.is_file() else sys.executable


def checks_for(component: str) -> list[Check]:
    if component == "backend":
        return [
            Check(
                "backend unit and API tests",
                (
                    project_python(ROOT / "backend"),
                    "-m",
                    "pytest",
                    "tests",
                    "-q",
                ),
                ROOT / "backend",
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
    if component == "agents":
        agents_root = ROOT / "packages" / "agents"
        return [
            Check(
                "agents library tests",
                (project_python(agents_root), "-m", "pytest", "tests", "-q"),
                agents_root,
            )
        ]
    if component == "codegen":
        codegen_root = ROOT / "codegen"
        return [
            Check(
                "code generation service tests",
                (project_python(codegen_root), "-m", "pytest", "tests"),
                codegen_root,
            )
        ]
    if component == "runner":
        runner_root = ROOT / "runner"
        return [
            Check(
                "pipeline runner service tests",
                (project_python(runner_root), "-m", "pytest", "tests"),
                runner_root,
            )
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
                environment={
                    "INLUMEN_CODEGEN_SERVICE_API_KEY": "compose-validation-token",
                    "INLUMEN_RUNNER_SERVICE_API_KEY": "runner-validation-token",
                    "POSTGRES_PASSWORD": "compose-postgres-password",
                    "NEO4J_AUTH": "neo4j/compose-password",
                    "MINIO_ROOT_USER": "compose-minio",
                    "MINIO_ROOT_PASSWORD": "compose-minio-password",
                    "KEYCLOAK_JWKS_URL": "https://identity.example/realms/inlumen/certs",
                    "KEYCLOAK_ISSUER": "https://identity.example/realms/inlumen",
                    "KEYCLOAK_AUDIENCE": "inlumen-frontend",
                    "VITE_KEYCLOAK_URL": "https://identity.example",
                    "INLUMEN_PUBLIC_URL": "https://inlumen.example",
                    "INLUMEN_SECRET_ENCRYPTION_KEY": "test-only-compose-value",
                },
            ),
        ]
    raise ValueError(f"Unknown test component: {component}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--component",
        action="append",
        choices=("agents", "backend", "codegen", "runner", "frontend", "compose"),
        help="Run only this component. Repeat the option to select more than one.",
    )
    parser.add_argument(
        "--sequential",
        action="store_true",
        help="Run one check at a time and show its output as it is produced.",
    )
    return parser.parse_args()


def run_check(check: Check, *, capture: bool) -> tuple[bool, str, float]:
    """Run one check; returns whether it passed, its captured output and its seconds."""
    started = time.monotonic()
    environment = os.environ.copy()
    environment.update(check.environment or {})
    try:
        result = subprocess.run(
            check.command,
            cwd=check.cwd,
            check=False,
            env=environment,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None,
            text=True,
            errors="replace",
        )
    except FileNotFoundError as error:
        return False, f"Unable to start {check.command[0]}: {error}\n", time.monotonic() - started
    return result.returncode == 0, result.stdout or "", time.monotonic() - started


def main() -> int:
    args = parse_args()
    components = args.component or [
        "agents",
        "backend",
        "codegen",
        "runner",
        "frontend",
        "compose",
    ]
    checks = [check for component in components for check in checks_for(component)]
    failures: list[str] = []

    if args.sequential or len(checks) == 1:
        for check in checks:
            print(f"\n=== {check.name} ===", flush=True)
            passed, output, _seconds = run_check(check, capture=False)
            print(output, end="", file=sys.stderr)
            if not passed:
                failures.append(check.name)
    else:
        # The checks are independent, so they run side by side; each one's
        # output is printed as a block when it finishes.
        with ThreadPoolExecutor(max_workers=min(len(checks), os.cpu_count() or 2)) as pool:
            running = {pool.submit(run_check, check, capture=True): check for check in checks}
            for future in as_completed(running):
                check = running[future]
                passed, output, seconds = future.result()
                status = "passed" if passed else "FAILED"
                print(f"\n=== {check.name}: {status} in {seconds:.0f}s ===\n{output}", end="", flush=True)
                if not passed:
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
