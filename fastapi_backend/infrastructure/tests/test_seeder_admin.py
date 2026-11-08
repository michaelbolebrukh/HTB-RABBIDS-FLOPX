"""Admin seeding: platform + product emails, single-or-CSV, idempotent add-only."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from infrastructure.database import Base
from infrastructure.settings import settings as s
import platform.models  # noqa: F401 — registers User on Base.metadata
import product.models  # noqa: F401
from platform.models import User
from platform.enums import Role
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
def admin_env(monkeypatch):
    # Default both unset; each test sets what it needs on the settings singleton:
    monkeypatch.setattr(s, "ADMIN_EMAIL", None)
    monkeypatch.setattr(s, "PRODUCT_ADMIN_EMAIL", None)
    return monkeypatch


def _admins(db):
    # Filters through the is_admin hybrid expression (role == 'admin'):
    return {u.email for u in db.query(User).filter(User.is_admin).all()}


def test_no_config_seeds_nothing(db, admin_env):
    seeder.seed_admin_user_if_configured(db)
    assert db.query(User).count() == 0


def test_single_platform_email(db, admin_env):
    admin_env.setattr(s, "ADMIN_EMAIL", "Owner@Example.com")
    seeder.seed_admin_user_if_configured(db)
    admins = _admins(db)
    assert admins == {"owner@example.com"}  # normalized to lowercase
    user = db.query(User).filter(User.email == "owner@example.com").first()
    assert user.is_verified and user.password is None
    assert user.name == "Owner"  # derived from local-part


def test_csv_platform_and_product_union_deduped(db, admin_env):
    admin_env.setattr(s, "ADMIN_EMAIL", "a@x.io, b@x.io , a@x.io")   # dup within list
    admin_env.setattr(s, "PRODUCT_ADMIN_EMAIL", "b@x.io,c@x.io")     # b@ overlaps platform
    seeder.seed_admin_user_if_configured(db)
    assert _admins(db) == {"a@x.io", "b@x.io", "c@x.io"}
    # No duplicate rows despite the overlaps:
    assert db.query(User).count() == 3


def test_product_only(db, admin_env):
    admin_env.setattr(s, "PRODUCT_ADMIN_EMAIL", "prod@example.com")
    seeder.seed_admin_user_if_configured(db)
    assert _admins(db) == {"prod@example.com"}


def test_idempotent_rerun_no_duplicates(db, admin_env):
    admin_env.setattr(s, "ADMIN_EMAIL", "owner@example.com,second@example.com")
    seeder.seed_admin_user_if_configured(db)
    seeder.seed_admin_user_if_configured(db)  # second boot
    assert db.query(User).count() == 2
    assert _admins(db) == {"owner@example.com", "second@example.com"}


def test_existing_verified_user_promoted_add_only(db, admin_env):
    # A pre-existing VERIFIED non-admin user with a password proved mailbox
    # ownership, so promotion is safe: keeps the password but gains admin:
    existing = User(
        name="Existing", email="owner@example.com", password="hashed",
        is_verified=True, role=Role.USER, marketing_consent=True,
    )
    db.add(existing)
    db.commit()

    admin_env.setattr(s, "ADMIN_EMAIL", "owner@example.com")
    seeder.seed_admin_user_if_configured(db)

    refreshed = db.query(User).filter(User.email == "owner@example.com").first()
    assert refreshed.is_admin and refreshed.is_verified
    assert refreshed.password == "hashed"   # not clobbered
    assert refreshed.name == "Existing"     # not renamed
    assert db.query(User).count() == 1


def test_existing_passwordless_unverified_user_promoted(db, admin_env):
    # Passwordless rows carry no squatter risk (no login path), so an
    # unverified one is still promoted and verified:
    existing = User(
        name="Existing", email="owner@example.com", password=None,
        is_verified=False, role=Role.USER, marketing_consent=False,
    )
    db.add(existing)
    db.commit()

    admin_env.setattr(s, "ADMIN_EMAIL", "owner@example.com")
    seeder.seed_admin_user_if_configured(db)

    refreshed = db.query(User).filter(User.email == "owner@example.com").first()
    assert refreshed.is_admin and refreshed.is_verified


def test_unverified_password_row_is_not_promoted(db, admin_env, caplog):
    # An unverified password-bearing row was registered by someone who never
    # proved mailbox ownership: promotion is refused entirely — no role change,
    # no verification, and a loud ERROR for the operator:
    existing = User(
        name="Squatter", email="owner@example.com", password="hashed",
        is_verified=False, role=Role.USER, marketing_consent=False,
    )
    db.add(existing)
    db.commit()

    admin_env.setattr(s, "ADMIN_EMAIL", "owner@example.com")
    with caplog.at_level("ERROR"):
        seeder.seed_admin_user_if_configured(db)

    refreshed = db.query(User).filter(User.email == "owner@example.com").first()
    assert refreshed.role == Role.USER
    assert not refreshed.is_verified
    assert refreshed.password == "hashed"
    assert any("refusing to promote" in r.message for r in caplog.records)


def test_role_derives_admin():
    """is_admin derives from the single role identity column."""
    admin = User(role=Role.ADMIN)
    plain = User(role=Role.USER)

    assert admin.is_admin
    assert not plain.is_admin


def test_admin_passes_any_capability_check():
    """Admin is the catch-all identity: verify_capability admits it for any grant."""
    from infrastructure.dependencies import verify_capability
    from platform.exceptions import CapabilityException

    verify_capability(User(role=Role.ADMIN, capabilities=None), "anything:at-all")
    with pytest.raises(CapabilityException):
        verify_capability(User(role=Role.USER, capabilities=None), "anything:at-all")
