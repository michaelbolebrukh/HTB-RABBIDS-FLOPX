"""Test-user seeding: refreshes a synthetic QA account every boot, never a human admin."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from infrastructure.database import Base
from infrastructure.settings import settings as s
import platform.models  # noqa: F401 — registers User on Base.metadata
import product.models  # noqa: F401
from platform.capabilities import LINKS_MANAGE
from platform.models import User
from platform.enums import Role
from security import bcrypt_context
from infrastructure import seeder


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def seed_env(monkeypatch):
    # Default everything unset; each test sets what it needs on the settings singleton:
    monkeypatch.setattr(s, "ADMIN_EMAIL", None)
    monkeypatch.setattr(s, "PRODUCT_ADMIN_EMAIL", None)
    monkeypatch.setattr(s, "TEST_USER_EMAIL", None)
    monkeypatch.setattr(s, "TEST_USER_PASSWORD", None)
    return monkeypatch


def test_no_config_seeds_nothing(db, seed_env):
    seeder.seed_test_user_if_configured(db)
    assert db.query(User).count() == 0


def test_fresh_qa_account_is_verified_non_admin_with_links_grant(db, seed_env):
    seed_env.setattr(s, "TEST_USER_EMAIL", "QA@Example.com")
    seed_env.setattr(s, "TEST_USER_PASSWORD", "first-secret")
    seeder.seed_test_user_if_configured(db)

    user = db.query(User).filter(User.email == "qa@example.com").first()
    assert user is not None and user.is_verified
    assert user.role == Role.USER
    assert LINKS_MANAGE in (user.capabilities or [])
    assert bcrypt_context.verify("first-secret", user.password)


def test_rerun_refreshes_password_from_env_without_duplicating(db, seed_env):
    seed_env.setattr(s, "TEST_USER_EMAIL", "qa@example.com")
    seed_env.setattr(s, "TEST_USER_PASSWORD", "first-secret")
    seeder.seed_test_user_if_configured(db)

    seed_env.setattr(s, "TEST_USER_PASSWORD", "rotated-secret")
    seeder.seed_test_user_if_configured(db)  # second boot

    assert db.query(User).count() == 1
    user = db.query(User).filter(User.email == "qa@example.com").first()
    assert bcrypt_context.verify("rotated-secret", user.password)
    assert not bcrypt_context.verify("first-secret", user.password)


@pytest.mark.parametrize("admin_var", ["ADMIN_EMAIL", "PRODUCT_ADMIN_EMAIL"])
def test_configured_admin_address_is_refused_as_test_user(db, seed_env, caplog, admin_var):
    # The exact misconfiguration the Learn preview shipped with: the human admin's
    # own address in the QA slot. Nothing is created and the operator is told why:
    seed_env.setattr(s, admin_var, "Owner@Example.com")
    seed_env.setattr(s, "TEST_USER_EMAIL", "owner@example.com")
    seed_env.setattr(s, "TEST_USER_PASSWORD", "generated-qa-secret")

    with caplog.at_level("ERROR"):
        seeder.seed_test_user_if_configured(db)

    assert db.query(User).count() == 0
    assert any("configured admin address" in r.message for r in caplog.records)


def test_seeded_admin_password_survives_boot_when_also_named_as_test_user(db, seed_env, caplog):
    # Admin seeding runs first and creates the passwordless admin; a later reset
    # gives them a password of their own. The test-user seeder must not touch it:
    seed_env.setattr(s, "ADMIN_EMAIL", "owner@example.com")
    seeder.seed_admin_user_if_configured(db)
    admin = db.query(User).filter(User.email == "owner@example.com").first()
    admin.password = bcrypt_context.hash("chosen-by-the-human")
    db.commit()

    seed_env.setattr(s, "TEST_USER_EMAIL", "owner@example.com")
    seed_env.setattr(s, "TEST_USER_PASSWORD", "generated-qa-secret")
    with caplog.at_level("ERROR"):
        seeder.seed_test_user_if_configured(db)

    refreshed = db.query(User).filter(User.email == "owner@example.com").first()
    assert refreshed.is_admin
    assert bcrypt_context.verify("chosen-by-the-human", refreshed.password)
    assert LINKS_MANAGE not in (refreshed.capabilities or [])


def test_existing_admin_row_is_refused_even_when_not_in_the_configured_lists(db, seed_env, caplog):
    # An admin promoted by hand is not in ADMIN_EMAIL/PRODUCT_ADMIN_EMAIL; the row's
    # own role still protects it:
    existing = User(
        name="Promoted", email="promoted@example.com",
        password=bcrypt_context.hash("their-own"), is_verified=True,
        role=Role.ADMIN, marketing_consent=False,
    )
    db.add(existing)
    db.commit()

    seed_env.setattr(s, "TEST_USER_EMAIL", "promoted@example.com")
    seed_env.setattr(s, "TEST_USER_PASSWORD", "generated-qa-secret")
    with caplog.at_level("ERROR"):
        seeder.seed_test_user_if_configured(db)

    refreshed = db.query(User).filter(User.email == "promoted@example.com").first()
    assert bcrypt_context.verify("their-own", refreshed.password)
    assert any("existing admin row" in r.message for r in caplog.records)


def test_existing_non_admin_row_is_refreshed_and_gains_links_grant(db, seed_env):
    existing = User(
        name="Member", email="qa@example.com",
        password=bcrypt_context.hash("stale"), is_verified=False,
        role=Role.USER, marketing_consent=False,
    )
    db.add(existing)
    db.commit()

    seed_env.setattr(s, "TEST_USER_EMAIL", "qa@example.com")
    seed_env.setattr(s, "TEST_USER_PASSWORD", "fresh")
    seeder.seed_test_user_if_configured(db)

    refreshed = db.query(User).filter(User.email == "qa@example.com").first()
    assert refreshed.is_verified
    assert refreshed.role == Role.USER
    assert bcrypt_context.verify("fresh", refreshed.password)
    assert LINKS_MANAGE in refreshed.capabilities
    assert refreshed.name == "Member"  # not renamed
