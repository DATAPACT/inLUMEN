from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

SERVICE_API_KEY_ENV = "CODEGEN_SERVICE_API_KEY"
SERVICE_API_KEY_FILE_ENV = "CODEGEN_SERVICE_API_KEY_FILE"
AUTH_DISABLED_ENV = "CODEGEN_AUTH_DISABLED"

bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="CodegenServiceAPIKey",
    description="API key used to authorize requests to the code generation service.",
)


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def read_secret(env_name: str, file_env_name: str) -> str:
    """Read a secret directly or from a Docker/Kubernetes mounted secret file."""
    secret_file = os.getenv(file_env_name, "").strip()
    if secret_file:
        try:
            return Path(secret_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(
                f"Unable to read secret file configured by {file_env_name}"
            ) from exc
    return os.getenv(env_name, "").strip()


def service_auth_configuration_error() -> str | None:
    """Return a safe readiness error when service authentication cannot work."""
    if env_flag(AUTH_DISABLED_ENV):
        return None
    try:
        expected = read_secret(SERVICE_API_KEY_ENV, SERVICE_API_KEY_FILE_ENV)
    except RuntimeError:
        return "Service authentication secret file is unavailable"
    if not expected:
        return "Service authentication is not configured"
    return None


async def require_service_api_key(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> None:
    """Authenticate internal callers using only the codegen service credential."""
    if env_flag(AUTH_DISABLED_ENV):
        return

    configuration_error = service_auth_configuration_error()
    if configuration_error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=configuration_error,
        )
    expected = read_secret(SERVICE_API_KEY_ENV, SERVICE_API_KEY_FILE_ENV)
    supplied = credentials.credentials if credentials is not None else ""
    valid = (
        credentials is not None
        and credentials.scheme.lower() == "bearer"
        and hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing service credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response
