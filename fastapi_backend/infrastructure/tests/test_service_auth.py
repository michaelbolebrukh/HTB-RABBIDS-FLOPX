"""Service auth: constant-time token matching, fingerprint attribution,
revocation, and the persisted service account (seeded idempotently, resolved
fail-closed at request time)."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from infrastructure import seeder
from infrastructure import service_auth as sa
from infrastructure.database import Base
from infrastructure.settings import settings as s
import platform.models  # noqa: F401 — registers User on Base.metadata
import product.models  # noqa: F401
from platform.capabilities import NOTIFICATIONS_BROADCAST, PARTNERSHIPS_MANAGE
from platform.models import User


TOKEN_A = "svc_" + "a" * 40
TOKEN_B = "svc_" + "b" * 40


@pytest.fixture
def tokens(monkeypatch):
    monkeypatch.setattr(s, "SERVICE_TOKENS", f"{TOKEN_A}, {TOKEN_B}")


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()


# -- token matching ------------------------------------------------------------

def test_valid_token_yields_fingerprint(tokens):
    fingerprint = sa.match_token(TOKEN_A)
    assert fingerprint and TOKEN_A not in fingerprint  # attribution handle, never the secret


def test_wrong_and_empty_tokens_reject(tokens):
    assert sa.match_token("svc_" + "z" * 40) is None
    assert sa.match_token("") is None
    assert sa.match_token(None) is None


def test_each_token_has_distinct_fingerprint(tokens):
    assert sa.match_token(TOKEN_A) != sa.match_token(TOKEN_B)


def test_revocation_by_removing_from_list(monkeypatch):
    monkeypatch.setattr(s, "SERVICE_TOKENS", TOKEN_B)  # A revoked
    assert sa.match_token(TOKEN_A) is None
    assert sa.match_token(TOKEN_B) is not None


def test_short_tokens_are_ignored(monkeypatch):
    monkeypatch.setattr(s, "SERVICE_TOKENS", "short")
    assert sa.match_token("short") is None  # below min length, never live


def test_unset_disables_service_auth(monkeypatch):
    monkeypatch.setattr(s, "SERVICE_TOKENS", None)
    assert sa.configured_tokens() == []
    assert sa.match_token(TOKEN_A) is None


# -- bearer parsing ------------------------------------------------------------

def test_bearer_parsing():
    assert sa.bearer_from_authorization("Bearer abc123") == "abc123"
    assert sa.bearer_from_authorization("bearer abc123") == "abc123"
    assert sa.bearer_from_authorization("Basic abc") is None
    assert sa.bearer_from_authorization(None) is None
    assert sa.bearer_from_authorization("Bearer ") is None


# -- the service account -------------------------------------------------------

def test_service_account_email_derives_from_app_domain(monkeypatch):
    monkeypatch.setattr(s, "APP_DOMAIN", "Quant-Soc.com")
    assert sa.service_account_email() == "service@quant-soc.com"  # normalized


def test_get_service_account_loads_seeded_row(db, tokens):
    seeder.seed_service_account_if_configured(db)
    account = sa.get_service_account(db)
    assert account is not None and account.email == sa.service_account_email()


def test_get_service_account_missing_row_fails_closed(db, caplog):
    # SERVICE_TOKENS set but the seeder never ran: None + a loud log, never an
    # auto-created identity at request time:
    with caplog.at_level("ERROR"):
        assert sa.get_service_account(db) is None
    assert any("not seeded" in record.message for record in caplog.records)


# -- seeding -------------------------------------------------------------------

def test_seeding_skipped_without_tokens(db, monkeypatch):
    monkeypatch.setattr(s, "SERVICE_TOKENS", None)
    seeder.seed_service_account_if_configured(db)
    assert db.query(User).count() == 0


def test_seeded_account_shape(db, tokens):
    seeder.seed_service_account_if_configured(db)
    row = db.query(User).one()
    assert row.email == sa.service_account_email()
    assert row.is_verified and row.password is None  # headless: login paths dead
    assert not row.is_admin and row.marketing_consent is False
    assert NOTIFICATIONS_BROADCAST in row.capabilities
    # The agent-operated write path for partnership state (see the seeder):
    assert PARTNERSHIPS_MANAGE in row.capabilities
    # No implicit admin: the account passes only the grants it actually holds:
    assert row.has_capability(NOTIFICATIONS_BROADCAST)
    assert not row.has_capability("inbox:read")


def test_seeding_idempotent_and_add_only(db, tokens):
    seeder.seed_service_account_if_configured(db)
    row = db.query(User).one()
    row.capabilities = ["custom:grant"]
    db.commit()

    seeder.seed_service_account_if_configured(db)  # second boot
    row = db.query(User).one()  # still one row
    # The seeded grants union back in; the manual grant is never removed:
    assert set(row.capabilities) == {"custom:grant", *seeder.SERVICE_ACCOUNT_CAPABILITIES}


def test_existing_passwordless_row_adopts(db, tokens):
    # A passwordless pre-existing row at the reserved address is safe to adopt
    # (no login path exists), gaining the grants and the verified flag:
    db.add(User(name="Pre", email=sa.service_account_email(), password=None,
                is_verified=False, capabilities=None, marketing_consent=False))
    db.commit()

    seeder.seed_service_account_if_configured(db)

    row = db.query(User).one()
    assert row.is_verified
    assert NOTIFICATIONS_BROADCAST in row.capabilities


def test_password_bearing_row_refuses_adoption(db, tokens, caplog):
    # A password at the reserved address means someone else registered it:
    # adoption is refused — no grants, no verification, password untouched,
    # and a loud ERROR for the operator:
    db.add(User(name="Squatter", email=sa.service_account_email(), password="hashed",
                is_verified=False, capabilities=None, marketing_consent=False))
    db.commit()

    with caplog.at_level("ERROR"):
        seeder.seed_service_account_if_configured(db)

    row = db.query(User).one()
    assert row.password == "hashed"
    assert not row.is_verified
    assert not (row.capabilities or [])  # gains NO grants
    assert any("refusing to adopt" in r.message for r in caplog.records)


# -- reserved-address registration guard --------------------------------------

def test_reserved_email_registration_rejected(db, monkeypatch):
    # Registering the reserved service-account address is refused with the
    # SAME error a taken address raises (no enumeration signal), before any
    # row is created:
    from fastapi import BackgroundTasks

    from platform.exceptions import UserExistsException
    from platform.schemas import CreateUserRequest
    from platform.services import user_service

    # A routable domain: the schema's email validator rejects the test-env
    # default APP_DOMAIN (.local is a special-use TLD):
    monkeypatch.setattr(s, "APP_DOMAIN", "instance-under-test.com")
    request = CreateUserRequest(
        name="Someone",
        email=sa.service_account_email().upper(),  # normalization still catches it
        password="Str0ng!Passw0rd#77",
    )
    with pytest.raises(UserExistsException):
        user_service.create_user(db, request, BackgroundTasks())
    assert db.query(User).count() == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
