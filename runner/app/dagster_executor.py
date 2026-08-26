from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class DagsterExecutionServiceError(RuntimeError):
    pass


class CodegenDagsterExecutor:
    """Execute immutable Dagster bundle snapshots in the private codegen service."""

    def __init__(
        self,
        *,
        service_url: str | None = None,
        api_key: str | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        self.service_url = (
            service_url
            or os.getenv("INLUMEN_CODEGEN_SERVICE_URL", "http://codegen:8010")
        ).rstrip("/")
        self.api_key = (
            api_key
            if api_key is not None
            else os.getenv("INLUMEN_CODEGEN_SERVICE_API_KEY", "").strip()
        )
        self.timeout_seconds = timeout_seconds or int(
            os.getenv("RUNNER_DAGSTER_TIMEOUT_SECONDS", "1800")
        )

    @property
    def configured(self) -> bool:
        return bool(self.service_url and self.api_key)

    async def execute(
        self,
        run_id: str,
        files: list[dict[str, Any]],
        runtime_secrets: dict[str, str],
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._request,
            "POST",
            "/v1/validate/deployment-bundle",
            {
                "execution_id": run_id,
                "files": files,
                "targets": {"argo": False, "dagster": True},
                "mode": "validate",
                "validate_argo": False,
                "validate_dagster": True,
                "materialize": True,
                "timeout_seconds": self.timeout_seconds,
                "runtime_secrets": runtime_secrets,
            },
        )

    async def cancel(self, run_id: str) -> None:
        await asyncio.to_thread(
            self._request,
            "DELETE",
            f"/v1/validate/deployment-bundle/{quote(run_id, safe='')}",
            None,
        )

    async def progress(self, run_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._request,
            "GET",
            f"/v1/validate/deployment-bundle/{quote(run_id, safe='')}/progress",
            None,
            10,
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        request_timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        if not self.configured:
            raise DagsterExecutionServiceError(
                "Dagster execution service authentication is not configured."
            )
        encoded = (
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else None
        )
        request = Request(
            f"{self.service_url}{path}",
            data=encoded,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                **({"Content-Type": "application/json"} if encoded else {}),
            },
        )
        try:
            with urlopen(
                request,
                timeout=request_timeout_seconds or self.timeout_seconds + 30,
            ) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise DagsterExecutionServiceError(
                f"Dagster execution service rejected the run ({exc.code}): {detail}"
            ) from exc
        except URLError as exc:
            raise DagsterExecutionServiceError(
                f"Dagster execution service is unavailable at {self.service_url}: {exc.reason}"
            ) from exc
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError as exc:
            raise DagsterExecutionServiceError(
                "Dagster execution service returned invalid JSON."
            ) from exc
        if not isinstance(parsed, dict):
            raise DagsterExecutionServiceError(
                "Dagster execution service returned a non-object response."
            )
        return parsed
