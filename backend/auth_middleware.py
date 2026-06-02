import os
import functools
from dataclasses import dataclass
from typing import Any, Optional
from flask import request, jsonify
import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError

_jwks_client: Optional[PyJWKClient] = None
_jwks_client_url: str = ""


@dataclass(frozen=True)
class AuthValidationError:
    status_code: int
    error: str
    detail: str
    details: dict[str, Any] | None = None


def is_auth_enabled() -> bool:
    return os.getenv("AUTH_ENABLED", "false").lower() == "true"


def _keycloak_jwks_url() -> str:
    return os.getenv("KEYCLOAK_JWKS_URL", "").strip()


def _keycloak_issuer() -> str:
    return os.getenv("KEYCLOAK_ISSUER", "").strip()


def _keycloak_audience() -> str:
    return os.getenv("KEYCLOAK_AUDIENCE", "").strip()


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
        return None, AuthValidationError(401, "Unauthorized", "Token expired")
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
        if not is_auth_enabled() or request.method == "OPTIONS":
            return f(*args, **kwargs)

        _claims, error = validate_keycloak_bearer_token()
        if error is not None:
            payload = {"error": error.error, "detail": error.detail}
            if error.details is not None:
                payload.update(error.details)
            return jsonify(payload), error.status_code

        return f(*args, **kwargs)

    return decorated
