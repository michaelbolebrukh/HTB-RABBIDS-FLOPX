"""Requesting a verification email when there is nothing left to verify.

The endpoint takes `unverified_user_dependency`, whose name reads like a guard
and is not one: it means "verification is not required to CALL this". A fully
verified session therefore reaches the service exactly as a pending one does,
and without a check the service mints a fresh code and emails it every time.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from infrastructure.database import Base
# Platform tables only. A `product.models` import here would be a real
# boundary violation (platform must never reach into product) and
# test_architecture_boundaries.py scans this package too, which is exactly
# how it caught the first draft of this file.
import platform.models  # noqa: F401  (registers tables)
from platform.models import User
from platform.services import user_service as us


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _user(db, *, verified: bool) -> User:
    user = User(email="ada@example.com", name="Ada", is_verified=verified)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_a_verified_account_is_not_sent_another_verification_code(db, monkeypatch):
    """The defect: a stream of "verify your account" mail to somebody who
    verified months ago. Nothing is compromised, since the code only reaches
    their own mailbox, but it costs send volume and it teaches a recipient to
    treat our verification mail as noise, which is the one message we most
    need them to trust."""
    sent = []
    monkeypatch.setattr(
        us, "generate_and_send_code",
        lambda *args, **kwargs: sent.append(kwargs.get("email", True)),
    )

    result = us.request_user_verification(db, _user(db, verified=True), None)

    assert sent == [], "a verified account was emailed another verification code"
    # Answered rather than raised, so a caller that retries is not broken:
    assert result.cooldown_remaining_seconds == 0


def test_an_unverified_account_still_gets_its_code(db, monkeypatch):
    """The other side of the guard. A check that also silenced the accounts
    that genuinely need the email would be a far worse bug than the one it
    fixes, and it would look identical in a green suite."""
    sent = []
    monkeypatch.setattr(
        us, "generate_and_send_code",
        lambda *args, **kwargs: sent.append(kwargs.get("email", True)),
    )

    result = us.request_user_verification(db, _user(db, verified=False), None)

    assert len(sent) == 1, "an unverified account was not sent its verification code"
    assert result.cooldown_remaining_seconds > 0
