"""Centralized web-push: full payload, audience scoping, pruning, fail-open."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from infrastructure.database import Base
import platform.models  # noqa: F401 — registers models
import product.models  # noqa: F401
from platform.enums import Role
from platform.models import PushSubscription, User
from platform.services.notification_service import push as push_service
from platform.services.notification_service.push import Audience, PushNotification


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def _stub_vapid(monkeypatch):
    # Avoid generating real EC keys / hitting a push service in unit tests:
    monkeypatch.setattr(push_service, "get_vapid_keys", lambda db: ("pem", "public"))
    monkeypatch.setattr(push_service, "_raw_private_key", lambda pem: "raw")


def _user(db, email, role=Role.USER, capabilities=None):
    u = User(name="X", email=email, role=role, capabilities=capabilities)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _sub(db, user, endpoint):
    s = PushSubscription(user_id=user.id, endpoint=endpoint, p256dh="p", auth="a")
    db.add(s)
    db.commit()
    return s


# -- payload -------------------------------------------------------------------

def test_payload_omits_unset_and_includes_set_fields():
    p = PushNotification(title="Hi", body="b", url="/x").to_payload()
    assert p == {"title": "Hi", "body": "b", "url": "/x"}


def test_payload_full_capability():
    p = PushNotification(
        title="New season live now", body="Season 3 is here", url="/learn",
        icon="/i.png", badge="/b.png", image="/hero.png", tag="season",
        renotify=True, require_interaction=True, silent=False, vibrate=[100, 50, 100],
        lang="en", dir="ltr",
        actions=[{"action": "open", "title": "Play", "url": "/learn/s3"}],
    ).to_payload()
    assert p["image"] == "/hero.png"
    assert p["requireInteraction"] is True
    assert p["renotify"] is True
    assert p["vibrate"] == [100, 50, 100]
    assert p["actions"] == [{"action": "open", "title": "Play"}]
    assert p["actionUrls"] == {"open": "/learn/s3"}


# -- audience scoping ----------------------------------------------------------

def test_broadcast_reaches_all_devices(db, monkeypatch):
    sent_to = []
    monkeypatch.setattr(push_service, "webpush", lambda **kw: sent_to.append(kw["subscription_info"]["endpoint"]))
    user, reader = _user(db, "u@x.com"), _user(db, "r@x.com", capabilities=["inbox:read"])
    _sub(db, user, "e-user"); _sub(db, reader, "e-reader")

    assert push_service.send_broadcast(db, PushNotification(title="Announcement")) == 2
    assert set(sent_to) == {"e-user", "e-reader"}


def test_broadcast_to_email_list_targets_only_those_users(db, monkeypatch):
    sent_to = []
    monkeypatch.setattr(push_service, "webpush", lambda **kw: sent_to.append(kw["subscription_info"]["endpoint"]))
    a, b, c = _user(db, "a@x.com"), _user(db, "b@x.com"), _user(db, "c@x.com")
    _sub(db, a, "e-a"); _sub(db, b, "e-b"); _sub(db, c, "e-c")

    # Only the addressed cohort is pushed (case-insensitive match):
    count = push_service.send_broadcast(db, PushNotification(title="Cohort"), emails=["A@x.com", "c@x.com"])
    assert count == 2
    assert set(sent_to) == {"e-a", "e-c"}


def test_broadcast_empty_email_list_sends_to_nobody(db, monkeypatch):
    monkeypatch.setattr(push_service, "webpush", lambda **kw: None)
    _sub(db, _user(db, "a@x.com"), "e-a")
    # An explicit empty cohort is not "everyone" — it is nobody:
    assert push_service.send_broadcast(db, PushNotification(title="X"), emails=[]) == 0


def test_inbox_ping_reaches_only_inbox_readers(db, monkeypatch):
    sent_to = []
    monkeypatch.setattr(push_service, "webpush", lambda **kw: sent_to.append(kw["subscription_info"]["endpoint"]))
    user = _user(db, "u@x.com")
    reader = _user(db, "r@x.com", capabilities=["inbox:read"])
    other_grant = _user(db, "o@x.com", capabilities=["marketing:manage"])
    admin = _user(db, "a@x.com", Role.ADMIN)
    _sub(db, user, "e-user"); _sub(db, reader, "e-reader")
    _sub(db, other_grant, "e-other"); _sub(db, admin, "e-admin")

    # Only inbox:read holders (explicit or implicit via admin) are pinged; the
    # end user and the unrelated-grant account are NOT pinged about inbox mail:
    assert push_service.notify_inbox_readers(db, "New mail") == 2
    assert set(sent_to) == {"e-reader", "e-admin"}


# -- delivery robustness -------------------------------------------------------

def test_gone_endpoint_is_pruned(db, monkeypatch):
    class Gone(push_service.WebPushException):
        def __init__(self):
            self.response = type("R", (), {"status_code": 410})()

    def _webpush(**kw):
        raise Gone()

    monkeypatch.setattr(push_service, "webpush", _webpush)
    user = _user(db, "u@x.com")
    _sub(db, user, "e-dead")

    assert push_service.send_broadcast(db, PushNotification(title="X")) == 0
    assert db.query(PushSubscription).count() == 0  # pruned


def test_transient_failure_is_fail_open(db, monkeypatch):
    def _webpush(**kw):
        raise RuntimeError("network blip")

    monkeypatch.setattr(push_service, "webpush", _webpush)
    user = _user(db, "u@x.com")
    _sub(db, user, "e-1")

    # Does not raise; subscription is kept (not a gone status):
    assert push_service.send_broadcast(db, PushNotification(title="X")) == 0
    assert db.query(PushSubscription).count() == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
