"""Connector flow: gating, audited grants, settings payload, Apple profile."""

import plistlib

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import infrastructure.dependencies as deps
from infrastructure.settings import settings as s
from platform.enums import Role
from platform.models import User
from platform.routers.inbox import router
from platform.services.inbox_service import connections


AUTH = {"Authorization": "Bearer test-token"}

WRITER = User(role=Role.USER, capabilities=["inbox:read", "inbox:write"])
READER = User(role=Role.USER, capabilities=["inbox:read"])
# Admin implicitly holds every capability; the password-exposing reveal + Apple
# profile endpoints are gated inbox:credentials — granted to nobody, so
# effectively admin-only (a plain inbox:write account cannot extract the
# mailbox password) — so those flows authenticate as ADMIN:
ADMIN = User(role=Role.ADMIN)


class FakeQuery:
    def options(self, *a):
        return self

    def order_by(self, *a):
        return self

    def group_by(self, *a):
        return self

    def limit(self, *a):
        return self

    def all(self):
        return []


class FakeDb:
    def __init__(self):
        self.added = []
        self.committed = False

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def query(self, *a):
        return FakeQuery()


def _client(monkeypatch, user: User, db: FakeDb) -> TestClient:
    # Patch the single auth-extraction point so the real capability gates each
    # run against this user: inbox:write for most endpoints, inbox:credentials
    # (admin-only in practice) for reveal + Apple profile.
    monkeypatch.setattr(deps, "get_current_verified_user", lambda db, token: user)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[deps.get_db] = lambda: db
    return TestClient(app)


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(s, "INBOX_IMAP_EMAIL", "hello@instance.io")
    monkeypatch.setattr(s, "INBOX_IMAP_PASSWORD", "mailbox-pass")
    monkeypatch.setattr(s, "SENDER_NAME", "Quant Soc")


def test_reveal_returns_material_and_records_grant(monkeypatch, caplog):
    db = FakeDb()
    ADMIN.id = 7
    client = _client(monkeypatch, ADMIN, db)

    with caplog.at_level("INFO"):
        response = client.post("/inbox/connections/reveal",
                               json={"connector": "outlook"}, headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "hello@instance.io"
    assert body["password"] == "mailbox-pass"
    assert body["imap_host"] == "imap.forwardemail.net" and body["imap_port"] == 993
    assert body["smtp_host"] == "smtp.forwardemail.net" and body["smtp_port"] == 465

    # Grant recorded and audit-logged (never the credential itself):
    assert db.committed and db.added[0].connector == "outlook"
    audit = [r.message for r in caplog.records if "credential granted" in r.message]
    assert audit and "mailbox-pass" not in " ".join(audit)


def test_unknown_connector_is_normalized(monkeypatch):
    db = FakeDb()
    client = _client(monkeypatch, ADMIN, db)
    assert client.post("/inbox/connections/reveal",
                       json={"connector": "weird-client"}, headers=AUTH).status_code == 200
    assert db.added[0].connector == "other"


def test_apple_profile_is_a_valid_mail_plist(monkeypatch):
    db = FakeDb()
    client = _client(monkeypatch, ADMIN, db)

    response = client.post("/inbox/connections/apple-profile", headers=AUTH)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-apple-aspen-config")
    profile = plistlib.loads(response.content)
    mail = profile["PayloadContent"][0]
    assert mail["PayloadType"] == "com.apple.mail.managed"
    assert mail["EmailAddress"] == "hello@instance.io"
    assert mail["IncomingMailServerHostName"] == "imap.forwardemail.net"
    assert mail["IncomingMailServerPortNumber"] == 993
    assert mail["IncomingPassword"] == "mailbox-pass"
    assert mail["OutgoingMailServerHostName"] == "smtp.forwardemail.net"
    assert mail["OutgoingPasswordSameAsIncomingPassword"] is True
    assert db.added[0].connector == "apple"


def test_audit_list_requires_inbox_write(monkeypatch):
    # The connector audit list carries no secret, so any inbox:write holder
    # reads it; a reader without the grant is refused.
    assert _client(monkeypatch, READER, FakeDb()).get(
        "/inbox/connections", headers=AUTH).status_code == 403
    assert _client(monkeypatch, WRITER, FakeDb()).get(
        "/inbox/connections", headers=AUTH).status_code == 200


def test_reveal_and_apple_profile_require_inbox_credentials(monkeypatch):
    # Both expose the mailbox PASSWORD, so they are gated inbox:credentials
    # (granted to nobody -> admin-only in practice): an inbox:write holder can
    # operate the mailbox in-app but cannot extract its password to configure
    # an external client. Neither a reader nor a plain writer passes.
    for user in (READER, WRITER):
        db = FakeDb()
        client = _client(monkeypatch, user, db)
        assert client.post("/inbox/connections/reveal",
                           json={"connector": "apple"}, headers=AUTH).status_code == 403
        assert client.post("/inbox/connections/apple-profile", headers=AUTH).status_code == 403
        assert db.added == []


def test_unconfigured_inbox_reveals_nothing(monkeypatch):
    monkeypatch.setattr(s, "INBOX_IMAP_PASSWORD", None)
    db = FakeDb()
    client = _client(monkeypatch, ADMIN, db)
    assert client.post("/inbox/connections/reveal",
                       json={"connector": "apple"}, headers=AUTH).status_code == 503
    assert db.added == []