from __future__ import annotations

import asyncio
import os
from typing import Any
from urllib.parse import quote
import httpx


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
        workspace_id: str = "local-workspace",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(limits=httpx.Limits(max_connections=100, max_keepalive_connections=50))
        self.workspace_id = workspace_id
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

    def for_workspace(self, workspace_id: str) -> "CodegenDagsterExecutor":
        return CodegenDagsterExecutor(service_url=self.service_url, api_key=self.api_key,
                                     timeout_seconds=self.timeout_seconds, workspace_id=workspace_id, client=self._client)

    @property
    def configured(self) -> bool:
        return bool(self.service_url and self.api_key)

    async def execute(
        self,
        run_id: str,
        files: list[dict[str, Any]],
        runtime_secrets: dict[str, str],
    ) -> dict[str, Any]:
        accepted = await self._request("POST", "/v1/validate/deployment-bundle?background=true", {
            "execution_id": run_id, "files": files,
            "targets": {"argo": False, "dagster": True}, "mode": "validate",
            "validate_argo": False, "validate_dagster": True, "materialize": True,
            "timeout_seconds": self.timeout_seconds, "runtime_secrets": runtime_secrets,
        }, 30)
        if accepted.get("status") != "accepted":
            return accepted  # Completed immutable receipt on a duplicate submission.
        deadline = asyncio.get_running_loop().time() + self.timeout_seconds + 30
        while asyncio.get_running_loop().time() < deadline:
            try:
                receipt = await self.result(run_id)
            except DagsterExecutionServiceError:
                # Observation is safe to retry. Submission is never retried: an
                # unknown POST outcome may already own a durable execution.
                await asyncio.sleep(2)
                continue
            if receipt.get("status") == "completed":
                return receipt["result"]
            if receipt.get("status") == "interrupted":
                raise DagsterExecutionServiceError("Execution was interrupted; it will not be replayed.")
            await asyncio.sleep(2)
        raise DagsterExecutionServiceError("Execution did not finish before its deadline.")

    async def aclose(self) -> None:
        await self._client.aclose()

    async def workload(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/execution-workload", None, 10)

    async def result(self, run_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/validate/deployment-bundle/{quote(run_id, safe='')}/result", None, 10)

    async def cancel(self, run_id: str) -> None:
        await self._request("DELETE", f"/v1/validate/deployment-bundle/{quote(run_id, safe='')}", None, 10)

    async def progress(self, run_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/validate/deployment-bundle/{quote(run_id, safe='')}/progress", None, 10)

    async def _request(self, method: str, path: str, payload: dict[str, Any] | None,
                       request_timeout_seconds: int | None = None) -> dict[str, Any]:
        if not self.configured:
            raise DagsterExecutionServiceError("Dagster execution service authentication is not configured.")
        try:
            response = await self._client.request(method, f"{self.service_url}{path}", json=payload,
                headers={"Accept": "application/json", "X-InLumen-Workspace-Id": self.workspace_id,
                         "Authorization": f"Bearer {self.api_key}"},
                timeout=request_timeout_seconds or self.timeout_seconds + 30)
            response.raise_for_status()
            parsed = response.json()
        except httpx.HTTPError as exc:
            # Do not echo response bodies: upstream errors can contain runtime secrets.
            raise DagsterExecutionServiceError("Execution service request failed. Check service health and run status.") from exc
        except ValueError as exc:
            raise DagsterExecutionServiceError("Execution service returned invalid JSON.") from exc
        if not isinstance(parsed, dict):
            raise DagsterExecutionServiceError("Execution service returned a non-object response.")
        return parsed
