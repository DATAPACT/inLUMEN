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
    if component == "frontend":
        npm = executable("npm")
        return [
            Check("frontend unit tests", (npm, "run", "test:run"), ROOT / "frontend"),
            Check("frontend lint", (npm, "run", "lint"), ROOT / "frontend"),
            Check("frontend type check", (npm, "run", "typecheck"), ROOT / "frontend"),
            Check("frontend production build", (npm, "run", "build"), ROOT / "frontend"),
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
                    "CLOUDFLARE_TUNNEL_TOKEN": "compose-tunnel-token",
                },
            ),
        ]
    raise ValueError(f"Unknown test component: {component}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--component",
        action="append",
        choices=("backend", "codegen", "runner", "frontend", "compose"),
        help="Run only this component. Repeat the option to select more than one.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    components = args.component or [
        "backend",
        "codegen",
        "runner",
        "frontend",
        "compose",
    ]
    failures: list[str] = []

    for component in components:
        for check in checks_for(component):
            print(f"\n=== {check.name} ===", flush=True)
            try:
                environment = os.environ.copy()
                environment.update(check.environment or {})
                result = subprocess.run(
                    check.command,
                    cwd=check.cwd,
                    check=False,
                    env=environment,
                )
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
