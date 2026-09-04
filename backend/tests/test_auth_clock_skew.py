import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from auth_middleware import (
    _keycloak_clock_skew_seconds,
    validate_keycloak_bearer_token,
    validate_production_auth_configuration,
)


class AuthClockSkewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def validate(self, changes=None, skew="5", signing_key=None):
        now = 1800000000
        claims = dict(iss="https://issuer/realms/test", sub="user", azp="frontend",
                      iat=now, nbf=now, exp=now + 300)
        claims.update(changes or {})
        token = jwt.encode(claims, signing_key or self.key, algorithm="RS256")
        with patch.dict("os.environ", {
            "KEYCLOAK_ISSUER": "https://issuer/realms/test",
            "KEYCLOAK_AUDIENCE": "frontend", "KEYCLOAK_CLOCK_SKEW_SECONDS": skew,
        }), patch("auth_middleware._get_jwks_client") as client, patch("jwt.api_jwt.datetime") as clock:
            client.return_value.get_signing_key_from_jwt.return_value = SimpleNamespace(key=self.key.public_key())
            clock.now.return_value = datetime.fromtimestamp(now, timezone.utc)
            return validate_keycloak_bearer_token("Bearer " + token)

    def test_small_future_issue_and_not_before_times_are_accepted(self):
        claims, error = self.validate({"iat": 1800000002, "nbf": 1800000002})
        self.assertIsNone(error)
        self.assertEqual(claims["sub"], "user")

    def test_future_times_beyond_tolerance_are_rejected(self):
        for claim in ("iat", "nbf"):
            with self.subTest(claim=claim):
                _, error = self.validate({claim: 1800000006})
                self.assertEqual(error.code, "token_not_yet_valid")
                self.assertEqual(error.status_code, 401)

    def test_expiry_has_only_the_configured_grace_period(self):
        self.assertIsNone(self.validate({"exp": 1799999998})[1])
        self.assertEqual(self.validate({"exp": 1799999995})[1].code, "token_expired")

    def test_zero_tolerance_is_supported(self):
        self.assertEqual(self.validate({"iat": 1800000001}, skew="0")[1].code, "token_not_yet_valid")

    def test_signature_issuer_and_audience_remain_enforced(self):
        for changes in ({"iss": "https://wrong-issuer"}, {"azp": "other-client"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.validate(changes)[1].status_code, 401)
        wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.assertEqual(self.validate(signing_key=wrong_key)[1].status_code, 401)

    def test_configuration_is_bounded_and_fails_at_startup(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(_keycloak_clock_skew_seconds(), 5)
        for value in ("-1", "61", "nan", "1.5", ""):
            with self.subTest(value=value), patch.dict("os.environ", {"KEYCLOAK_CLOCK_SKEW_SECONDS": value}):
                with self.assertRaises(RuntimeError):
                    validate_production_auth_configuration()


if __name__ == "__main__":
    unittest.main()
