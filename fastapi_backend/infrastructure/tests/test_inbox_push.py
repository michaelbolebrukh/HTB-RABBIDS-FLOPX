"""Web push: VAPID self-provisioning, subscription lifecycle, new-mail watcher."""

import types

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import platform.services.inbox_service.monitors as monitors
from infrastructure.database import Base
from platform.enums import Role
from platform.models import PlatformKey, PushSubscription, User
from platform.services.notification_service import push
from platform.services.inbox_service.ports import MailAddress, MessagePage, MessageSummary


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(User(name="Admin", email="admin@instance.io", role=Role.ADMIN))
        db.commit()
    return factory


def test_vapid_keys_generate_once_and_persist(session_factory):
    with session_factory() as db:
        pem_one, public_one = push.get_vapid_keys(db)
    with session_factory() as db:
        pem_two, public_two = push.get_vapid_keys(db)
        assert (pem_one, public_one) == (pem_two, public_two)
        assert db.query(PlatformKey).count() == 1
    assert pem_one.startswith("-----BEGIN")
    # applicationServerKey is base64url of the uncompressed P-256 point:
    assert len(public_one) == 87


def test_subscribe_upserts_by_endpoint(session_factory):
    with session_factory() as db:
        push.subscribe(db, 1, "https://push.example/abc", "p1", "a1")
        push.subscribe(db, 1, "https://push.example/abc", "p2", "a2")
        rows = db.query(PushSubscription).all()
        assert len(rows) == 1
        assert rows[0].p256dh == "p2"

        push.unsubscribe(db, "https://push.example/abc")
        assert db.query(PushSubscription).count() == 0


def test_notify_inbox_readers_delivers_and_prunes_gone_endpoints(session_factory, monkeypatch):
    with session_factory() as db:
        push.subscribe(db, 1, "https://push.example/live", "p", "a")
        push.subscribe(db, 1, "https://push.example/gone", "p", "a")

        sent = []

        def fake_webpush(subscription_info, **kwargs):
            if "gone" in subscription_info["endpoint"]:
                raise push.WebPushException(
                    "gone", response=types.SimpleNamespace(status_code=410),
                )
            sent.append(subscription_info["endpoint"])

        # Delivery + pruning live in the centralized notification engine; the
        # inbox ping (notify_inbox_readers) reaches the admin's devices (user 1 =
        # admin, an implicit inbox:read holder):
        monkeypatch.setattr(push, "webpush", fake_webpush)
        assert push.notify_inbox_readers(db, "Title", "Body") == 1
        assert sent == ["https://push.example/live"]
        # The 410 endpoint was pruned so the pool stays clean:
        endpoints = [row.endpoint for row in db.query(PushSubscription).all()]
        assert endpoints == ["https://push.example/live"]


def _summary(mid, date, subject="Hello"):
    return MessageSummary(id=mid, folder="INBOX", subject=subject,
                          sender=MailAddress(email="a@b.co", name="Ann"), date=date,
                          is_unread=True, is_flagged=False, has_attachments=False, snippet="")


class StubMailbox:
    def __init__(self):
        self.messages = []

    def list_messages(self, folder, page, search):
        return MessagePage(messages=self.messages, page=1, page_count=1)


def test_watcher_baselines_then_announces_only_new(session_factory, monkeypatch):
    mailbox = StubMailbox()
    mailbox.messages = [_summary("m1", "2026-07-12T10:00:00.000Z")]
    monkeypatch.setattr(monitors.svc, "get_mailbox", lambda: mailbox)

    announced = []
    monkeypatch.setattr(
        "platform.services.notification_service.push.notify_inbox_readers",
        lambda db, title, body="", url="/": announced.append((title, body)) or 1,
    )

    # First tick baselines silently (no notification flood on a fresh boot):
    assert monitors.check_new_mail_once(session_factory) == 0
    assert announced == []

    # Nothing new: still quiet.
    assert monitors.check_new_mail_once(session_factory) == 0

    # One arrival past the mark announces sender + subject:
    mailbox.messages = [
        _summary("m2", "2026-07-12T10:05:00.000Z", subject="Quarterly"),
        _summary("m1", "2026-07-12T10:00:00.000Z"),
    ]
    assert monitors.check_new_mail_once(session_factory) == 1
    assert announced == [("Ann", "Quarterly")]

    # The mark advanced, so a repeat tick stays quiet (exactly-once):
    assert monitors.check_new_mail_once(session_factory) == 0


def test_watcher_skips_canary_heartbeats(session_factory, monkeypatch):
    mailbox = StubMailbox()
    mailbox.messages = [_summary("m1", "2026-07-12T10:00:00.000Z")]
    monkeypatch.setattr(monitors.svc, "get_mailbox", lambda: mailbox)
    announced = []
    monkeypatch.setattr(
        "platform.services.notification_service.push.notify_inbox_readers",
        lambda db, title, body="", url="/": announced.append((title, body)) or 1,
    )
    monitors.check_new_mail_once(session_factory)

    mailbox.messages = [
        _summary("m2", "2026-07-12T10:06:00.000Z", subject="Inbox canary abc123"),
        _summary("m1", "2026-07-12T10:00:00.000Z"),
    ]
    assert monitors.check_new_mail_once(session_factory) == 0
    assert announced == []
