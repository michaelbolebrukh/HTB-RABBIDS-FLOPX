"""Email-keyed marketing suppression + shared unsubscribe tokens.

Covers the generalization that lets bulk/announcement mail to *any* address
(not just registered users) carry a working unsubscribe, with this table as the
address-level source of truth mirrored to registered users' stored consent.
"""


import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from infrastructure.database import Base
import platform.models  # noqa: F401 — registers models on Base.metadata
import product.models  # noqa: F401
from platform.enums import Role
from platform.exceptions import InvalidUnsubscribeTokenException
from platform.models import MarketingSuppression, User
from platform.services import marketing_service as ms


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _user(db, email, consent=True):
    u = User(name="X", email=email, role=Role.USER, marketing_consent=consent)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


# -- email token round-trip ----------------------------------------------------

def test_email_token_round_trips_and_normalizes():
    token = ms.mint_email_unsubscribe_token("  Jane@Example.COM ")
    assert ms._verify_email_token(token) == "jane@example.com"


def test_email_token_rejects_tamper():
    token = ms.mint_email_unsubscribe_token("jane@example.com")
    head, body, sig = token.split(".")
    assert ms._verify_email_token(f"{head}.{body}.{'0' * len(sig)}") is None
    assert ms._verify_email_token("v1.1.deadbeef") is None  # wrong flavour/version


def test_user_and_email_tokens_are_not_interchangeable():
    # A user token must not verify as an email token and vice versa:
    assert ms._verify_email_token(ms.mint_unsubscribe_token(1)) is None
    assert ms._verify_user_token(ms.mint_email_unsubscribe_token("a@b.com")) is None


# -- suppression store + user mirroring ---------------------------------------

def test_suppress_non_user_address(db):
    ms.suppress_email(db, "guest@external.com", reason="unsubscribe")
    assert ms.is_suppressed(db, "GUEST@external.com") is True
    assert db.query(MarketingSuppression).count() == 1


def test_suppress_mirrors_to_registered_user_consent(db):
    user = _user(db, "member@site.com", consent=True)
    ms.suppress_email(db, "member@site.com")
    db.refresh(user)
    assert user.marketing_consent is False
    assert ms.is_suppressed(db, "member@site.com") is True


def test_unsuppress_restores_consent_and_removes_row(db):
    user = _user(db, "member@site.com", consent=False)
    ms.suppress_email(db, "member@site.com")
    ms.unsuppress_email(db, "member@site.com")
    db.refresh(user)
    assert user.marketing_consent is True
    assert ms.is_suppressed(db, "member@site.com") is False


def test_suppress_is_idempotent(db):
    ms.suppress_email(db, "dup@x.com")
    ms.suppress_email(db, "dup@x.com")
    assert db.query(MarketingSuppression).count() == 1


def test_filter_suppressed_splits_batch(db):
    ms.suppress_email(db, "no@x.com")
    allowed, suppressed = ms.filter_suppressed(db, ["Yes@x.com", "no@x.com", "yes2@x.com"])
    assert allowed == ["yes2@x.com", "yes@x.com"]
    assert suppressed == ["no@x.com"]


# -- token-driven unsubscribe (public endpoint path) ---------------------------

def test_apply_email_token_suppresses(db):
    token = ms.mint_email_unsubscribe_token("blast@list.com")
    masked = ms.apply_unsubscribe_token(db, token, suppressed=True)
    assert masked == "b***@list.com"
    assert ms.is_suppressed(db, "blast@list.com") is True


def test_apply_user_token_suppresses_and_mirrors(db):
    user = _user(db, "u@site.com", consent=True)
    token = ms.mint_unsubscribe_token(user.id)
    ms.apply_unsubscribe_token(db, token, suppressed=True)
    db.refresh(user)
    assert user.marketing_consent is False
    assert ms.is_suppressed(db, "u@site.com") is True


def test_apply_invalid_token_raises(db):
    with pytest.raises(InvalidUnsubscribeTokenException):
        ms.apply_unsubscribe_token(db, "e1.bogus.bad", suppressed=True)
