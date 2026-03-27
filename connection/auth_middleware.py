import os
import functools
from typing import Optional
from flask import request, jsonify
import jwt
from jwt import PyJWKClient

AUTH_ENABLED: bool = os.getenv("AUTH_ENABLED", "false").lower() == "true"
KEYCLOAK_JWKS_URL: str = os.getenv("KEYCLOAK_JWKS_URL", "")
KEYCLOAK_ISSUER: str = os.getenv("KEYCLOAK_ISSUER", "")
KEYCLOAK_AUDIENCE: str = os.getenv("KEYCLOAK_AUDIENCE", "")

_jwks_client: Optional[PyJWKClient] = None


def _get_jwks_client() -> Optional[PyJWKClient]:
    global _jwks_client
    if _jwks_client is None and KEYCLOAK_JWKS_URL:
        _jwks_client = PyJWKClient(KEYCLOAK_JWKS_URL, cache_keys=True)
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
            decode_kwargs: dict = {
                "algorithms": ["RS256"],
                "options": {"verify_exp": True},
            }
            if KEYCLOAK_ISSUER:
                decode_kwargs["issuer"] = KEYCLOAK_ISSUER
            if KEYCLOAK_AUDIENCE:
                decode_kwargs["audience"] = KEYCLOAK_AUDIENCE
            else:
                decode_kwargs["options"]["verify_aud"] = False
            jwt.decode(token, signing_key.key, **decode_kwargs)
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Unauthorized", "detail": "Token expired"}), 401
        except jwt.InvalidTokenError as e:
            return jsonify({"error": "Unauthorized", "detail": str(e)}), 401

        return f(*args, **kwargs)

    return decorated