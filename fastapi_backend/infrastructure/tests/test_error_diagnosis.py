import hashlib
import hmac
import json

import pytest

from infrastructure.settings import settings
from platform.services.error_diagnosis import notifier
from platform.services.error_diagnosis import sentry_gateway as gw
from platform.services.error_diagnosis import service
from platform.services.error_diagnosis.snapshot import build_snapshot, summarize_event


# A representative Sentry event (backend), trimmed to the shape the code reads:
BACKEND_EVENT = {
    "title": "ZeroDivisionError: division by zero",
    "platform": "python",
    "tags": [
        {"key": "component", "value": "backend"},
        {"key": "environment", "value": "production"},
        {"key": "release", "value": "abc123"},
        {"key": "request_id", "value": "rid-1"},
    ],
    "user": {"id": "4242"},
    "entries": [
        {"type": "exception", "data": {"values": [{
            "type": "ZeroDivisionError", "value": "division by zero",
            "stacktrace": {"frames": [
                {"function": "_per_seat_cents", "filename": "diagnostics.py",
                 "lineNo": 30, "inApp": True, "vars": {"seats": "0", "row": "{...}"}},
            ]},
        }]}},
        {"type": "request", "data": {"method": "POST", "url": "https://api.example.com/x"}},
    ],
}

FRONTEND_EVENT = {
    "title": "Error: boom",
    "tags": [{"key": "component", "value": "frontend"}, {"key": "request_id", "value": "rid-1"}],
    "entries": [
        {"type": "breadcrumbs", "data": {"values": [
            {"category": "fetch", "level": "error", "message": None,
             "data": {"request_id": "rid-1", "status_code": 500}},
        ]}},
    ],
}


def _enable(monkeypatch):
    for k, v in {
        "SENTRY_WEBHOOK_SECRET": "shh",
        "SENTRY_API_TOKEN": "tok",
        "SENTRY_ORG_SLUG": "quantsoc",
        "SENTRY_PROJECT_SLUGS": "python-fastapi",
    }.items():
        monkeypatch.setattr(settings, k, v)


# --- gating -----------------------------------------------------------------

def test_dormant_when_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "SENTRY_WEBHOOK_SECRET", None)
    assert service.is_enabled() is False
    called = {"n": 0}
    monkeypatch.setattr(service.gw, "get_latest_event", lambda *_: called.update(n=called["n"] + 1))
    service.handle_sentry_webhook(b"{}", "sig")
    assert called["n"] == 0  # nothing ran


def test_enabled_when_all_set(monkeypatch):
    _enable(monkeypatch)
    assert service.is_enabled() is True


def test_enabled_with_two_secrets_only(monkeypatch):
    # The org + projects are discovered from the token, so only the two secrets
    # are required to arm the pipeline:
    monkeypatch.setattr(settings, "SENTRY_WEBHOOK_SECRET", "shh")
    monkeypatch.setattr(settings, "SENTRY_API_TOKEN", "tok")
    monkeypatch.setattr(settings, "SENTRY_ORG_SLUG", None)
    monkeypatch.setattr(settings, "SENTRY_PROJECT_SLUGS", None)
    assert service.is_enabled() is True


# --- org / project discovery (config-minimalism: identifiers come from the token) --

class _FakeResp:
    def __init__(self, data): self._data = data
    def raise_for_status(self): pass
    def json(self): return self._data


class _FakeClient:
    """Minimal httpx.Client stand-in routing by URL suffix."""
    def __init__(self, orgs=None, projects=None):
        self._orgs, self._projects = orgs, projects

    def __enter__(self): return self
    def __exit__(self, *a): return False

    def get(self, url, headers=None, params=None):
        if url.endswith("/projects/"):
            return _FakeResp(self._projects)
        if url.endswith("/organizations/"):
            return _FakeResp(self._orgs)
        raise AssertionError(f"unexpected url {url}")


def test_resolve_org_slug_override_wins(monkeypatch):
    # Override short-circuits discovery entirely (no network client patched):
    monkeypatch.setattr(settings, "SENTRY_ORG_SLUG", "explicit-org")
    gw.reset_discovery_cache()
    assert gw._resolve_org_slug() == "explicit-org"


def test_resolve_org_slug_discovers_single_org(monkeypatch):
    monkeypatch.setattr(settings, "SENTRY_ORG_SLUG", None)
    monkeypatch.setattr(settings, "SENTRY_API_TOKEN", "tok")
    gw.reset_discovery_cache()
    monkeypatch.setattr("httpx.Client", lambda timeout=None: _FakeClient(orgs=[{"slug": "blue-wisdom"}]))
    assert gw._resolve_org_slug() == "blue-wisdom"
    gw.reset_discovery_cache()


def test_resolve_org_slug_ambiguous_fails_loudly(monkeypatch):
    monkeypatch.setattr(settings, "SENTRY_ORG_SLUG", None)
    gw.reset_discovery_cache()
    monkeypatch.setattr("httpx.Client", lambda timeout=None: _FakeClient(orgs=[{"slug": "a"}, {"slug": "b"}]))
    with pytest.raises(RuntimeError):
        gw._resolve_org_slug()
    gw.reset_discovery_cache()


def test_resolve_project_slugs_discovers_all(monkeypatch):
    monkeypatch.setattr(settings, "SENTRY_ORG_SLUG", "blue-wisdom")  # skip org discovery
    monkeypatch.setattr(settings, "SENTRY_PROJECT_SLUGS", None)
    monkeypatch.setattr(settings, "SENTRY_API_TOKEN", "tok")
    gw.reset_discovery_cache()
    monkeypatch.setattr("httpx.Client",
                        lambda timeout=None: _FakeClient(projects=[{"slug": "python-fastapi"}, {"slug": "next-web"}]))
    assert gw._resolve_project_slugs() == ["python-fastapi", "next-web"]
    gw.reset_discovery_cache()


def test_resolve_project_slugs_override_wins(monkeypatch):
    monkeypatch.setattr(settings, "SENTRY_PROJECT_SLUGS", "only-this")
    gw.reset_discovery_cache()
    assert gw._resolve_project_slugs() == ["only-this"]


# --- signature verification -------------------------------------------------

def test_verify_signature_valid_and_invalid(monkeypatch):
    monkeypatch.setattr(settings, "SENTRY_WEBHOOK_SECRET", "shh")
    body = b'{"hello":"world"}'
    good = hmac.new(b"shh", body, hashlib.sha256).hexdigest()
    assert gw.verify_signature(body, good) is True
    assert gw.verify_signature(body, "deadbeef") is False
    assert gw.verify_signature(body, None) is False


def test_verify_signature_requires_secret(monkeypatch):
    monkeypatch.setattr(settings, "SENTRY_WEBHOOK_SECRET", None)
    assert gw.verify_signature(b"x", "anything") is False


# --- payload parsing --------------------------------------------------------

def test_parse_webhook_extracts_issue_and_request_id():
    payload = {"data": {"event": {
        "issue_id": "123", "event_id": "e1", "title": "ZeroDivisionError",
        "tags": [["request_id", "rid-1"], ["component", "backend"]],
    }}}
    ref = gw.parse_webhook(payload)
    assert ref["issue_id"] == "123"
    assert ref["request_id"] == "rid-1"
    assert ref["component"] == "backend"


def test_parse_webhook_returns_none_without_issue():
    assert gw.parse_webhook({"data": {}}) is None


# --- snapshot ---------------------------------------------------------------

def test_summarize_event_pulls_exception_and_locals():
    s = summarize_event(BACKEND_EVENT)
    assert s["tier"] == "backend"
    assert s["request_id"] == "rid-1"
    assert s["exception"]["type"] == "ZeroDivisionError"
    assert s["exception"]["frames"][-1]["vars"]["seats"] == "0"  # frame locals survive


def test_build_snapshot_lists_both_tiers():
    snap = build_snapshot(BACKEND_EVENT, [FRONTEND_EVENT], "rid-1")
    assert snap["request_id"] == "rid-1"
    assert set(snap["tiers_present"]) == {"backend", "frontend"}
    assert len(snap["correlated"]) == 1


# --- orchestration ----------------------------------------------------------

def _payload():
    return json.dumps({"data": {"event": {
        "issue_id": "123", "event_id": "e1", "title": "ZeroDivisionError",
        "tags": [["request_id", "rid-1"], ["component", "backend"]],
    }}}).encode()


def test_happy_path_emits_correlated_snapshot(monkeypatch):
    _enable(monkeypatch)
    delivered = {}
    monkeypatch.setattr(service.gw, "verify_signature", lambda b, sig: True)
    monkeypatch.setattr(service.gw, "get_latest_event", lambda iid: BACKEND_EVENT)
    monkeypatch.setattr(service.gw, "find_events_by_request_id", lambda rid, ex: [FRONTEND_EVENT])
    monkeypatch.setattr(service, "notify_admin", lambda snap, ref: delivered.update(snap=snap, ref=ref))

    service.handle_sentry_webhook(_payload(), "sig")
    snap = delivered["snap"]
    assert delivered["ref"]["issue_id"] == "123"
    assert snap["request_id"] == "rid-1"                     # correlation id carried through
    assert set(snap["tiers_present"]) == {"backend", "frontend"}  # both tiers correlated
    assert len(snap["correlated"]) == 1


def test_invalid_signature_stops(monkeypatch):
    _enable(monkeypatch)
    called = {"n": 0}
    monkeypatch.setattr(service.gw, "verify_signature", lambda b, sig: False)
    monkeypatch.setattr(service.gw, "get_latest_event", lambda *_: called.update(n=called["n"] + 1))
    service.handle_sentry_webhook(_payload(), "bad")
    assert called["n"] == 0


def test_fail_open_on_downstream_error(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(service.gw, "verify_signature", lambda b, sig: True)
    def boom(_):
        raise RuntimeError("sentry api down")
    monkeypatch.setattr(service.gw, "get_latest_event", boom)
    # Must not raise — collection is best-effort:
    service.handle_sentry_webhook(_payload(), "sig")


# --- admin notification (interim sink) --------------------------------------

def test_notify_admin_emails_the_snapshot(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_EMAIL", "admin@example.com")
    sent = {}
    monkeypatch.setattr(notifier, "send_email",
                        lambda **kw: sent.update(kw))
    snap = build_snapshot(BACKEND_EVENT, [FRONTEND_EVENT], "rid-1")
    notifier.notify_admin(snap, {"issue_id": "123", "title": "ZeroDivisionError"})
    assert sent["recipient"] == "admin@example.com"
    assert "rid-1" in sent["text_body"]              # the correlation id
    assert "ZeroDivisionError" in sent["text_body"]  # the exact payload is included
    assert "seats" in sent["text_body"]              # frame locals present in the payload


def test_notify_admin_noop_without_recipient(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_EMAIL", None)
    monkeypatch.setattr(settings, "CONTACT_EMAIL", None)
    called = {"n": 0}
    monkeypatch.setattr(notifier, "send_email", lambda **kw: called.update(n=called["n"] + 1))
    notifier.notify_admin(build_snapshot(BACKEND_EVENT, [], "rid-1"), {"issue_id": "1"})
    assert called["n"] == 0  # nothing sent, no crash
