import os
import functools
from typing import Any, Optional
from flask import request, jsonify
import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError

AUTH_ENABLED: bool = os.getenv("AUTH_ENABLED", "false").lower() == "true"
KEYCLOAK_JWKS_URL: str = os.getenv("KEYCLOAK_JWKS_URL", "")
KEYCLOAK_ISSUER: str = os.getenv("KEYCLOAK_ISSUER", "")
KEYCLOAK_AUDIENCE: str = os.getenv("KEYCLOAK_AUDIENCE", "")

_jwks_client: Optional[PyJWKClient] = None


def _claim_values(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {item for item in value if isinstance(item, str)}
    return set()


def _configured_audiences() -> set[str]:
    return {item.strip() for item in KEYCLOAK_AUDIENCE.split(",") if item.strip()}


def _token_matches_expected_audience(claims: dict, expected: set[str]) -> bool:
    if not expected:
        return True

    token_audiences = _claim_values(claims.get("aud"))
    token_authorized_parties = _claim_values(claims.get("azp")) | _claim_values(claims.get("client_id"))

    return bool(expected & (token_audiences | token_authorized_parties))


def _get_jwks_client() -> Optional[PyJWKClient]:
    global _jwks_client
    if _jwks_client is None and KEYCLOAK_JWKS_URL:
        _jwks_client = PyJWKClient(
            KEYCLOAK_JWKS_URL,
            cache_keys=True,
            headers={
                "Accept": "application/json",
                "User-Agent": "inLUMEN-auth/1.0",
            },
            timeout=10,
        )
    return _jwks_client


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
        if not AUTH_ENABLED or request.method == "OPTIONS":
            return f(*args, **kwargs)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Unauthorized", "detail": "Missing Bearer token"}), 401

        token = auth_header[len("Bearer "):]
        client = _get_jwks_client()
        if client is None:
            return jsonify({"error": "Auth misconfigured", "detail": "KEYCLOAK_JWKS_URL not set"}), 500

        try:
            signing_key = client.get_signing_key_from_jwt(token)
            expected_audiences = _configured_audiences()
            decode_kwargs: dict = {
                "algorithms": ["RS256"],
                "options": {"verify_exp": True, "verify_aud": False},
            }
            if KEYCLOAK_ISSUER:
                decode_kwargs["issuer"] = KEYCLOAK_ISSUER
            claims = jwt.decode(token, signing_key.key, **decode_kwargs)
            if not _token_matches_expected_audience(claims, expected_audiences):
                return jsonify({
                    "error": "Unauthorized",
                    "detail": "Audience doesn't match",
                    "expected": sorted(expected_audiences),
                    "token_aud": sorted(_claim_values(claims.get("aud"))),
                    "token_azp": claims.get("azp"),
                    "token_client_id": claims.get("client_id"),
                }), 401
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Unauthorized", "detail": "Token expired"}), 401
        except PyJWKClientConnectionError as e:
            return jsonify({"error": "Auth unavailable", "detail": str(e)}), 503
        except PyJWKClientError as e:
            return jsonify({"error": "Unauthorized", "detail": str(e)}), 401
        except jwt.InvalidTokenError as e:
            return jsonify({"error": "Unauthorized", "detail": str(e)}), 401

        return f(*args, **kwargs)

    return decorated
