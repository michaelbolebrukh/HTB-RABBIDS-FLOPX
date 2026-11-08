"""Non-ASCII credentials must refuse, never raise.

Starlette decodes headers and query strings as latin-1, so any byte >= 0x80 in
a bearer token, an unsubscribe token or a webhook signature reaches the
credential comparison as a non-ASCII `str`. `hmac.compare_digest` raises
`TypeError` on those, which turned a wrong credential into an unhandled 500 (and
a tracked error event) on five verification sites — a measurable difference from
the ordinary refusal, and on the fully public marketing endpoints an anonymous
error-quota drain.

These tests pin the behaviour at the helper and at every site that uses it.
"""

import pytest

from infrastructure.constant_time import constant_time_equals


# A latin-1-decoded high byte pair — exactly what Starlette hands the app:
NON_ASCII = "\xff\xfe"
UNICODE_TEXT = "café"


class TestHelper:
    """The single comparison primitive."""

    def test_matching_values_are_equal(self):
        assert constant_time_equals("abc123", "abc123") is True
        assert constant_time_equals(UNICODE_TEXT, UNICODE_TEXT) is True

    def test_non_ascii_input_refuses_instead_of_raising(self):
        assert constant_time_equals(NON_ASCII, "abc123") is False
        assert constant_time_equals("abc123", NON_ASCII) is False
        assert constant_time_equals(UNICODE_TEXT, "abc123") is False

    def test_missing_credential_never_matches(self):
        assert constant_time_equals(None, "abc123") is False
        assert constant_time_equals("abc123", None) is False
        assert constant_time_equals("", "") is True

    def test_bytes_and_text_forms_agree(self):
        assert constant_time_equals(b"abc123", "abc123") is True
        assert constant_time_equals(b"abc123", "abc124") is False


class TestServiceAuth:
    """`match_token` — the service-account bearer check."""

    def test_non_ascii_bearer_is_an_ordinary_miss(self, monkeypatch):
        from infrastructure import service_auth

        monkeypatch.setattr(service_auth.s, "SERVICE_TOKENS", "a-real-service-token-value")

        assert service_auth.match_token("a-real-service-token-value") is not None
        # Both wrong credentials refuse the same way — no exception, no signal:
        assert service_auth.match_token("wrong-but-ascii-token-value") is None
        assert service_auth.match_token(NON_ASCII) is None
        assert service_auth.match_token("Bearer " + NON_ASCII) is None


class TestMarketingTokens:
    """The two PUBLIC unsubscribe verifications."""

    def test_non_ascii_user_token_refuses(self):
        from platform.services import marketing_service as ms

        assert ms._verify_user_token(f"v1.1.{UNICODE_TEXT}") is None
        assert ms._verify_user_token(f"v1.1.{NON_ASCII}") is None

    def test_non_ascii_email_token_refuses(self):
        from platform.services import marketing_service as ms

        real = ms.mint_email_unsubscribe_token("someone@example.com")
        version, payload, _signature = real.split(".")
        assert ms._verify_email_token(f"{version}.{payload}.{UNICODE_TEXT}") is None
        assert ms._verify_email_token(f"{version}.{payload}.{NON_ASCII}") is None
        # The genuine token still verifies:
        assert ms._verify_email_token(real) == "someone@example.com"


class TestSentryWebhookSignature:
    """The inbound Sentry webhook HMAC."""

    def test_non_ascii_signature_refuses(self, monkeypatch):
        from platform.services.error_diagnosis import sentry_gateway as sg

        monkeypatch.setattr(sg.s, "SENTRY_WEBHOOK_SECRET", "webhook-secret")
        assert sg.verify_signature(b"{}", UNICODE_TEXT) is False
        assert sg.verify_signature(b"{}", NON_ASCII) is False

        import hashlib
        import hmac as _hmac
        good = _hmac.new(b"webhook-secret", b"{}", hashlib.sha256).hexdigest()
        assert sg.verify_signature(b"{}", good) is True


@pytest.mark.parametrize("credential", [b"\xff\xfe", "caf\xe9".encode("latin-1")])
def test_non_ascii_bearer_reaches_a_capability_gate_as_a_401(credential, monkeypatch):
    """
    End-to-end at the ASGI boundary: a non-ASCII bearer on a capability-gated
    route answers with the SAME status an ordinary bad token gets, never a 500:
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from infrastructure import service_auth
    from infrastructure.dependencies import require_capability
    from platform.capabilities import USERS_READ

    monkeypatch.setattr(service_auth.s, "SERVICE_TOKENS", "a-real-service-token-value")

    app = FastAPI()

    @app.get("/gated")
    def gated(user: require_capability(USERS_READ)):
        return {"ok": True}

    # The header goes in as RAW BYTES — an HTTP client cannot ASCII-encode
    # these, but a real socket can carry them and Starlette decodes latin-1:
    client = TestClient(app, raise_server_exceptions=False)
    ascii_status = client.get(
        "/gated", headers={b"Authorization": b"Bearer wrong-token-value"},
    ).status_code
    weird_status = client.get(
        "/gated", headers={b"Authorization": b"Bearer " + credential},
    ).status_code
    assert weird_status == ascii_status
    assert weird_status != 500
