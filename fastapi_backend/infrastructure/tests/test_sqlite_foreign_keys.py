"""SQLite must enforce foreign keys, like PostgreSQL always has.

SQLite ships with `PRAGMA foreign_keys` OFF, so `ondelete="CASCADE"` is silently
ignored — and relationships use `passive_deletes=True`, which deliberately leaves
the delete to the database. Orphaned children used to be mere junk. Now that
organisation membership CONFERS capabilities they are a privilege-escalation
vector: SQLite recycles row ids, so an orphaned membership can hand a departed
member's grants to whoever next takes that id.
"""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from infrastructure.database import Base
import platform.models  # noqa: F401 — registers the models on Base.metadata
import product.models  # noqa: F401
from platform.capabilities import FEATURE_PREVIEW
from platform.enums import Role
from platform.models import Organisation, OrganisationMember, User
from platform.services import organisation_service as orgs


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()


def test_the_pragma_is_on(db):
    assert db.execute(text("PRAGMA foreign_keys")).scalar() == 1, (
        "foreign keys must be enforced, or ON DELETE CASCADE is a no-op"
    )


def _member(db, email="member@example.com"):
    user = User(name="Member", email=email, is_verified=True, role=Role.USER,
                marketing_consent=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    org = orgs.create_organisation(db, "sponsor", "Sponsor",
                                   granted_capabilities=[FEATURE_PREVIEW])
    orgs.add_member(db, org, user)
    return user, org


def test_deleting_a_user_removes_their_memberships(db):
    user, _org = _member(db)
    assert db.query(OrganisationMember).count() == 1

    db.delete(user)
    db.commit()

    assert db.query(OrganisationMember).count() == 0, (
        "an orphaned membership would confer this user's grants to a recycled id"
    )


def test_deleting_an_organisation_removes_its_memberships(db):
    _user, org = _member(db)
    assert db.query(OrganisationMember).count() == 1

    db.delete(org)
    db.commit()

    assert db.query(OrganisationMember).count() == 0


def test_a_recycled_user_id_inherits_nothing(db):
    """The concrete escalation: SQLite reuses row ids, so a stale membership
    would silently attach a departed member's grants to a new account."""
    user, _org = _member(db)
    departed_id = user.id

    db.delete(user)
    db.commit()

    newcomer = User(name="Newcomer", email="new@example.com", is_verified=True,
                    role=Role.USER, marketing_consent=False)
    db.add(newcomer)
    db.commit()
    db.refresh(newcomer)

    assert newcomer.id == departed_id, "SQLite recycles the id — the premise of the risk"
    assert orgs.effective_capabilities(newcomer) == set()
    assert newcomer.has_capability(FEATURE_PREVIEW) is False
