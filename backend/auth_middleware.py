import os
import functools
from dataclasses import dataclass
from typing import Any, Optional
from flask import g, has_request_context, jsonify, request
import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError

from workspace_store import (
    Principal,
    WorkspaceAccessDenied,
    WorkspaceStoreError,
    WORKSPACE_HEADER,
    local_principal,
    resolve_principal,
    validate_auth_mode_continuity,
)

_jwks_client: Optional[PyJWKClient] = None
_jwks_client_url: str = ""


@dataclass(frozen=True)
class AuthValidationError:
    status_code: int
    error: str
    detail: str
    details: dict[str, Any] | None = None
    code: str | None = None


def is_auth_enabled() -> bool:
    value = os.getenv("AUTH_ENABLED", "false").strip().lower()
    if value not in {"true", "false"}:
        raise RuntimeError("AUTH_ENABLED must be exactly true or false; refusing to select an identity mode.")
    return value == "true"


def validate_production_auth_configuration() -> None:
    is_auth_enabled()  # Validate configuration in development as well as production.
    _keycloak_clock_skew_seconds()
    if os.getenv("APP_ENV", "development").strip().lower() != "production":
        return
    if not is_auth_enabled():
        raise RuntimeError("AUTH_ENABLED must be true when APP_ENV=production.")
    missing = [
        name
        for name, value in (
            ("KEYCLOAK_JWKS_URL", _keycloak_jwks_url()),
            ("KEYCLOAK_ISSUER", _keycloak_issuer()),
            ("KEYCLOAK_AUDIENCE", _keycloak_audience()),
            ("DATABASE_URL", os.getenv("DATABASE_URL", "").strip()),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Production authentication is missing: " + ", ".join(missing)
        )


def validate_auth_mode_configuration() -> None:
    """Apply the environment-specific identity-mode transition policy."""
    validate_auth_mode_continuity(is_auth_enabled())


def current_principal() -> Principal:
    if not has_request_context():
        return local_principal()
    principal = getattr(g, "inlumen_principal", None)
    if principal is None:
        principal = local_principal()
        g.inlumen_principal = principal
    return principal


def current_workspace_id() -> str:
    return current_principal().workspace_id


def current_user_id() -> str:
    return current_principal().user_id


def is_application_admin() -> bool:
    """Set only by require_auth after validating identity; never by workspace role."""
    return has_request_context() and getattr(g, "inlumen_is_application_admin", False) is True


def _keycloak_jwks_url() -> str:
    return os.getenv("KEYCLOAK_JWKS_URL", "").strip()


def _keycloak_issuer() -> str:
    return os.getenv("KEYCLOAK_ISSUER", "").strip()


def _keycloak_audience() -> str:
    return os.getenv("KEYCLOAK_AUDIENCE", "").strip()


def _keycloak_clock_skew_seconds() -> int:
    """Bounded tolerance for synchronized hosts, not a substitute for NTP."""
    value = os.getenv("KEYCLOAK_CLOCK_SKEW_SECONDS", "5").strip()
    try:
        seconds = int(value)
    except ValueError:
        raise RuntimeError("KEYCLOAK_CLOCK_SKEW_SECONDS must be an integer from 0 to 60") from None
    if not 0 <= seconds <= 60:
        raise RuntimeError("KEYCLOAK_CLOCK_SKEW_SECONDS must be an integer from 0 to 60")
    return seconds


def _claim_values(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {item for item in value if isinstance(item, str)}
    return set()


def _configured_audiences() -> set[str]:
    return {item.strip() for item in _keycloak_audience().split(",") if item.strip()}


def _token_matches_expected_audience(claims: dict, expected: set[str]) -> bool:
    if not expected:
        return True

    token_audiences = _claim_values(claims.get("aud"))
    token_authorized_parties = _claim_values(claims.get("azp")) | _claim_values(claims.get("client_id"))

    return bool(expected & (token_audiences | token_authorized_parties))


def _get_jwks_client() -> Optional[PyJWKClient]:
    global _jwks_client, _jwks_client_url
    jwks_url = _keycloak_jwks_url()
    if not jwks_url:
        return None
    if _jwks_client is None or _jwks_client_url != jwks_url:
        _jwks_client = PyJWKClient(
            jwks_url,
            cache_keys=True,
            headers={
                "Accept": "application/json",
                "User-Agent": "inLUMEN-auth/1.0",
            },
            timeout=10,
        )
        _jwks_client_url = jwks_url
    return _jwks_client


def validate_keycloak_bearer_token(auth_header: str | None = None) -> tuple[dict[str, Any] | None, AuthValidationError | None]:
    header = auth_header if auth_header is not None else request.headers.get("Authorization", "")
    scheme, separator, token = header.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        return None, AuthValidationError(401, "Unauthorized", "Missing Bearer token")

    token = token.strip()
    client = _get_jwks_client()
    if client is None:
        return None, AuthValidationError(500, "Auth misconfigured", "KEYCLOAK_JWKS_URL not set")

    try:
        signing_key = client.get_signing_key_from_jwt(token)
        expected_audiences = _configured_audiences()
        decode_kwargs: dict = {
            "algorithms": ["RS256"],
            "options": {"verify_exp": True, "verify_aud": False},
            "leeway": _keycloak_clock_skew_seconds(),
        }
        issuer = _keycloak_issuer()
        if issuer:
            decode_kwargs["issuer"] = issuer
        claims = jwt.decode(token, signing_key.key, **decode_kwargs)
        if not _token_matches_expected_audience(claims, expected_audiences):
            return None, AuthValidationError(
                401,
                "Unauthorized",
                "Audience doesn't match",
                {
                    "expected": sorted(expected_audiences),
                    "token_aud": sorted(_claim_values(claims.get("aud"))),
                    "token_azp": claims.get("azp"),
                    "token_client_id": claims.get("client_id"),
                },
            )
        return claims, None
    except jwt.ExpiredSignatureError:
        return None, AuthValidationError(401, "Unauthorized", "Token expired", code="token_expired")
    except jwt.ImmatureSignatureError:
        return None, AuthValidationError(
            401, "Unauthorized", "Token is not yet valid. Check server clock synchronization.",
            code="token_not_yet_valid",
        )
    except PyJWKClientConnectionError as e:
        return None, AuthValidationError(503, "Auth unavailable", str(e))
    except PyJWKClientError as e:
        return None, AuthValidationError(401, "Unauthorized", str(e))
    except jwt.InvalidTokenError as e:
        return None, AuthValidationError(401, "Unauthorized", str(e))


def require_auth(f):
    """
    Flask route decorator that validates Keycloak JWTs.

    Behaviour:
    - AUTH_ENABLED=false (default): no-op, request passes through.
    - AUTH_ENABLED=true: requires a valid Bearer token in the Authorization header.
    - OPTIONS requests are always allowed (CORS preflight).
    """
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if request.method == "OPTIONS":
            return f(*args, **kwargs)

        if not is_auth_enabled():
            if request.headers.get("Authorization"):
                return jsonify({
                    "error": "Authentication configuration mismatch",
                    "detail": "The browser sent an authenticated request but the server is in local mode. Recreate backend and frontend with the same root AUTH_ENABLED setting, then reload.",
                }), 409
            g.inlumen_principal = local_principal()
            g.inlumen_is_application_admin = os.getenv("APP_ENV", "development").strip().lower() != "production"
            return f(*args, **kwargs)

        claims, error = validate_keycloak_bearer_token()
        if error is not None:
            payload = {"error": error.error, "detail": error.detail}
            if error.code is not None:
                payload["code"] = error.code
            if error.details is not None:
                payload.update(error.details)
            return jsonify(payload), error.status_code

        try:
            g.inlumen_principal = resolve_principal(
                claims or {}, request.headers.get(WORKSPACE_HEADER)
            )
        except WorkspaceAccessDenied:
            return jsonify({"error": "Not found", "detail": "Workspace was not found"}), 404
        except WorkspaceStoreError as exc:
            return jsonify({"error": "Workspace unavailable", "detail": str(exc)}), 503
        except Exception:
            return jsonify({
                "error": "Workspace unavailable",
                "detail": "The workspace database could not be queried.",
            }), 503

        realm_access = (claims or {}).get("realm_access")
        roles = realm_access.get("roles") if isinstance(realm_access, dict) else None
        g.inlumen_is_application_admin = isinstance(roles, list) and "inlumen-admin" in roles
        if request.path == "/api/admin/application-llm":
            if not is_application_admin():
                return jsonify({"error": "Forbidden", "code": "application_admin_required"}), 403
            return f(*args, **kwargs)

        from permissions import permits
        if not permits(g.inlumen_principal.workspace_role, request.method, request.path):
            return jsonify({"error": "Forbidden", "code": "insufficient_permissions"}), 403
        return f(*args, **kwargs)

    return decorated
