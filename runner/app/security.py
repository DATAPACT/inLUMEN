from __future__ import annotations

import hmac
import os
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

bearer = HTTPBearer(auto_error=False, scheme_name="RunnerServiceAPIKey")


def configured_key() -> str:
    return os.getenv("RUNNER_SERVICE_API_KEY", "").strip()


async def require_service_api_key(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> None:
    expected = configured_key()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Runner service authentication is not configured.",
        )
    supplied = credentials.credentials if credentials is not None else ""
    valid = (
        credentials is not None
        and credentials.scheme.lower() == "bearer"
        and hmac.compare_digest(supplied.encode(), expected.encode())
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing service credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def workspace_context(
    workspace_id: Annotated[str | None, Header(alias="X-InLumen-Workspace-Id")] = None,
) -> str:
    resolved = str(workspace_id or "").strip()
    if resolved:
        return resolved
    if os.getenv("APP_ENV", "development").strip().lower() == "production":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-InLumen-Workspace-Id is required.",
        )
    return "local-workspace"
