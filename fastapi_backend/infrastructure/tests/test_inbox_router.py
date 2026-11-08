"""Inbox router auth matrix: every endpoint carries the right gate.

The session resolution is stubbed (token plumbing is covered elsewhere) while
the REAL require_capability closures run, so each endpoint's capability
requirement is exercised end to end through FastAPI.
"""
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import infrastructure.dependencies as deps
from platform.enums import Role
from platform.models import User
from platform.routers.inbox import router
from platform.services import inbox_service
from platform.services.inbox_service.ports import (
    AttachmentContent,
    MailAddress,
    MessagePage,
    MessageSummary,
)


AUTH = {"Authorization": "Bearer test-token"}


def _client(monkeypatch, user: User) -> TestClient:
    # The require_capability closure resolves get_current_verified_user by
    # module global at call time (the bearer is no service token here since
    # SERVICE_TOKENS is unset), so patching that single extraction point makes
    # the REAL capability check run on `user`:
    monkeypatch.setattr(deps, "get_current_verified_user", lambda db, token: user)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _configure(monkeypatch, configured=True, can_send=True):
    monkeypatch.setattr(inbox_service.service, "inbox_is_configured", lambda: configured)
    monkeypatch.setattr(inbox_service.service, "inbox_can_send", lambda: can_send)
    monkeypatch.setattr(inbox_service, "inbox_is_configured", lambda: configured)
    monkeypatch.setattr(inbox_service, "inbox_can_send", lambda: can_send)


READER = User(role=Role.USER, capabilities=["inbox:read"])
WRITER = User(role=Role.USER, capabilities=["inbox:read", "inbox:write"])
UNGRANTED = User(role=Role.USER, capabilities=[])
ADMIN = User(role=Role.ADMIN, capabilities=None)

SEND_BODY = {"to": [{"email": "bob@b.com"}], "subject": "Hi", "html": "<p>x</p>"}


def _stub_reads(monkeypatch):
    page = MessagePage(messages=[MessageSummary(
        id="m1", folder="INBOX", subject="s",
        sender=MailAddress(email="a@a.com"), date=None,
        is_unread=False, is_flagged=False, has_attachments=False, snippet="")], page=1, page_count=1)
    monkeypatch.setattr(inbox_service, "list_messages", lambda **kw: page)
    monkeypatch.setattr(inbox_service, "list_folders", lambda: ["INBOX"])
    monkeypatch.setattr(inbox_service, "get_attachment",
                        lambda mid, i: AttachmentContent("a.txt", "text/plain", b"x"))
    monkeypatch.setattr(inbox_service, "set_read", lambda mid, read: None)


def test_reader_can_read_but_not_send(monkeypatch):
    _stub_reads(monkeypatch)
    client = _client(monkeypatch, READER)

    assert client.get("/inbox/messages", headers=AUTH).status_code == 200
    assert client.get("/inbox/folders", headers=AUTH).status_code == 200
    assert client.get("/inbox/messages/m1/attachments/0", headers=AUTH).status_code == 200
    # Read-state is part of reading (shared mailbox state):
    assert client.patch("/inbox/messages/m1", json={"is_unread": True}, headers=AUTH).status_code == 204

    # Every send/mutation path is write-gated:
    assert client.post("/inbox/messages", json=SEND_BODY, headers=AUTH).status_code == 403
    assert client.post("/inbox/messages/m1/reply", json=SEND_BODY, headers=AUTH).status_code == 403
    assert client.post("/inbox/messages/m1/forward", json=SEND_BODY, headers=AUTH).status_code == 403
    assert client.post("/inbox/messages/m1/move", json={"folder": "Archive"}, headers=AUTH).status_code == 403
    assert client.delete("/inbox/messages/m1", headers=AUTH).status_code == 403


def test_ungranted_user_cannot_even_read(monkeypatch):
    _stub_reads(monkeypatch)
    client = _client(monkeypatch, UNGRANTED)

    assert client.get("/inbox/messages", headers=AUTH).status_code == 403
    # The config surface is part of reading the inbox, so it is gated too:
    assert client.get("/inbox/config", headers=AUTH).status_code == 403
    # A reader holds the grant and sees the config surface:
    reader_client = _client(monkeypatch, READER)
    assert reader_client.get("/inbox/config", headers=AUTH).status_code == 200


def test_admin_holds_every_gate(monkeypatch):
    _stub_reads(monkeypatch)
    captured = {}
    monkeypatch.setattr(inbox_service, "send_message", lambda **kw: captured.update(kw))
    monkeypatch.setattr(inbox_service, "delete_message", lambda mid: None)
    client = _client(monkeypatch, ADMIN)

    assert client.get("/inbox/messages", headers=AUTH).status_code == 200
    assert client.post("/inbox/messages", json=SEND_BODY, headers=AUTH).status_code == 201
    assert client.delete("/inbox/messages/m1", headers=AUTH).status_code == 204
    assert captured["subject"] == "Hi"


def test_unconfigured_inbox_returns_503_and_config_shows_it(monkeypatch):
    _configure(monkeypatch, configured=False, can_send=False)
    client = _client(monkeypatch, ADMIN)

    response = client.get("/inbox/config", headers=AUTH)
    body = response.json()
    assert body["configured"] is False and body["can_send"] is False
    assert client.get("/inbox/messages", headers=AUTH).status_code == 503


def test_attachment_budget_is_enforced(monkeypatch):
    client = _client(monkeypatch, WRITER)
    oversized = {**SEND_BODY, "attachments": [
        {"filename": "big.bin", "content_base64": "A" * 11_000_001}
    ]}
    assert client.post("/inbox/messages", json=oversized, headers=AUTH).status_code == 422


def test_send_requires_at_least_one_recipient(monkeypatch):
    client = _client(monkeypatch, WRITER)
    assert client.post("/inbox/messages", json={**SEND_BODY, "to": []}, headers=AUTH).status_code == 422


def test_half_configured_send_returns_503(monkeypatch):
    # Mailbox wired, sender not - reads work, sends fail closed and loudly:
    monkeypatch.setattr(inbox_service.service, "inbox_is_configured", lambda: True)
    monkeypatch.setattr(inbox_service.service, "inbox_can_send", lambda: False)
    client = _client(monkeypatch, WRITER)

    monkeypatch.setattr(
        inbox_service.service.get_mailbox(), "get_message",
        lambda mid: (_ for _ in ()).throw(AssertionError("send gate must fire before fetch")),
        raising=False,
    )
    response = client.post("/inbox/messages", json=SEND_BODY, headers=AUTH)
    assert response.status_code == 503


@pytest.fixture(autouse=True)
def _default_configuration(monkeypatch):
    _configure(monkeypatch, configured=True, can_send=True)
    # Undo-send off by default so the direct-send tests exercise the
    # synchronous path; the undo test opts back in explicitly:
    from infrastructure.settings import settings as app_settings
    monkeypatch.setattr(app_settings, "INBOX_UNDO_SEND_SECONDS", 0)
    yield


def test_immediate_send_files_into_undo_outbox(monkeypatch):
    """With the undo window on, a plain send becomes a cancellable outbox row"""
    from datetime import datetime, timedelta, timezone

    from infrastructure.settings import settings as app_settings
    from platform.routers import inbox as inbox_router

    monkeypatch.setattr(app_settings, "INBOX_UNDO_SEND_SECONDS", 20)
    created = {}

    class Row:
        id = 77

    def fake_create(db, user_id, payload, send_at):
        created.update(payload=payload, send_at=send_at)
        return Row()

    monkeypatch.setattr(inbox_router.inbox_scheduled, "create_scheduled", fake_create)
    monkeypatch.setattr(
        inbox_service, "send_message",
        lambda **kw: (_ for _ in ()).throw(AssertionError("undo path must not send synchronously")),
    )
    client = _client(monkeypatch, WRITER)

    response = client.post("/inbox/messages", json=SEND_BODY, headers=AUTH)
    assert response.status_code == 201
    body = response.json()
    assert body == {"scheduled": False, "undo_id": 77, "undo_seconds": 20}
    assert created["payload"]["kind"] == "new"
    # The outbox instant honors the window (about now + 20s):
    delta = created["send_at"] - datetime.now(timezone.utc)
    assert timedelta(seconds=10) < delta <= timedelta(seconds=25)


def test_package_surface_exports_every_symbol_the_router_calls():
    # Router calls go through the package surface (inbox_service.<fn>); a
    # service function that is not re-exported fails only at request time,
    # so pin the export list to the router's actual usage:
    import re
    from pathlib import Path

    import platform.services.inbox_service as pkg

    router_src = Path(__file__).resolve().parents[2] / "platform" / "routers" / "inbox.py"
    used = set(re.findall(r"inbox_service\.([a-z_]+)\(", router_src.read_text()))
    missing = sorted(name for name in used if not hasattr(pkg, name))
    assert not missing, f"inbox_service package must export: {missing}"
