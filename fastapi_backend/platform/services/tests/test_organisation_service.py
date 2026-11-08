"""Organisation membership as a source of capability grants.

Covers the contract organisation_service owns: idempotent provisioning, the typo
guard on every capability string written, conferral onto members, the union
across several organisations, and — the property that makes the computed design
work — revocation that removes a conferred grant while a DIRECTLY held one
survives untouched.

The thing to notice throughout: `users.capabilities` never changes. It means
grants held directly by the account, nothing here writes to it, so there is no
materialised copy to strip, repair or drift. `effective_capabilities` is the one
definition, and every assertion goes through it or through has_capability.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from infrastructure.database import Base
import platform.models  # noqa: F401 — registers User on Base.metadata
from platform.capabilities import EARLY_ACCESS, FEATURE_PREVIEW, INBOX_READ, USERS_READ
from platform.enums import Role
from platform.exceptions import UnknownCapabilityException
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


def _user(db, email, *, role=Role.USER, capabilities=None) -> User:
    user = User(name="X", email=email, role=role, capabilities=capabilities, is_verified=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _sponsor(db, slug="hudson-river-trading", caps=(FEATURE_PREVIEW, EARLY_ACCESS)) -> Organisation:
    return orgs.create_organisation(db, slug, "Hudson River Trading", granted_capabilities=caps)


def _effective(user) -> list[str]:
    return sorted(orgs.effective_capabilities(user))


# -- provisioning --------------------------------------------------------------

def test_create_organisation_stores_the_declared_grants(db):
    org = _sponsor(db)
    assert org.slug == "hudson-river-trading"
    assert org.is_active is True
    assert sorted(org.granted_capabilities) == sorted([FEATURE_PREVIEW, EARLY_ACCESS])


def test_create_organisation_is_idempotent_on_slug(db):
    first = _sponsor(db)
    second = orgs.create_organisation(db, "hudson-river-trading", "HRT again")

    assert second.id == first.id
    assert db.query(Organisation).count() == 1
    # The existing row is returned untouched — set_granted_capabilities is the
    # mutator, so a replayed provisioning call cannot silently strip grants:
    assert sorted(second.granted_capabilities) == sorted([FEATURE_PREVIEW, EARLY_ACCESS])


def test_create_organisation_rejects_an_unknown_capability(db):
    with pytest.raises(UnknownCapabilityException):
        orgs.create_organisation(db, "typo-co", "Typo Co", granted_capabilities=["feature:pre-view"])

    # Nothing is written when validation fails:
    assert db.query(Organisation).count() == 0


def test_set_granted_capabilities_rejects_an_unknown_capability(db):
    org = _sponsor(db)
    with pytest.raises(UnknownCapabilityException):
        orgs.set_granted_capabilities(db, org, [FEATURE_PREVIEW, "inbox:reed"])


# -- conferral -----------------------------------------------------------------

def test_add_member_confers_the_organisation_grants(db):
    org = _sponsor(db)
    user = _user(db, "sarah@x.com")

    orgs.add_member(db, org, user, title="University Relations")

    assert _effective(user) == sorted([FEATURE_PREVIEW, EARLY_ACCESS])
    assert user.has_capability(FEATURE_PREVIEW)
    # Conferred, never materialised: the account's own column is still empty,
    # which is exactly what makes revocation a single row delete:
    assert not user.capabilities


def test_add_member_is_idempotent_on_the_pair(db):
    org = _sponsor(db)
    user = _user(db, "sarah@x.com")

    first = orgs.add_member(db, org, user, title="University Relations")
    second = orgs.add_member(db, org, user)

    assert second.id == first.id
    assert db.query(OrganisationMember).count() == 1
    assert _effective(user) == sorted([FEATURE_PREVIEW, EARLY_ACCESS])


def test_membership_of_two_organisations_unions_their_grants(db):
    sponsor = _sponsor(db, caps=(FEATURE_PREVIEW,))
    society = orgs.create_organisation(db, "quant-soc", "Quant Soc", granted_capabilities=[EARLY_ACCESS])
    user = _user(db, "both@x.com")

    orgs.add_member(db, sponsor, user)
    orgs.add_member(db, society, user)

    assert _effective(user) == sorted([FEATURE_PREVIEW, EARLY_ACCESS])


def test_direct_and_conferred_grants_union(db):
    user = _user(db, "ops@x.com", capabilities=[INBOX_READ])
    org = _sponsor(db, caps=(FEATURE_PREVIEW,))
    orgs.add_member(db, org, user)

    assert _effective(user) == sorted([INBOX_READ, FEATURE_PREVIEW])


def test_an_inactive_organisation_confers_nothing(db):
    org = _sponsor(db)
    user = _user(db, "lapsed@x.com")
    orgs.add_member(db, org, user)
    assert _effective(user) == sorted([FEATURE_PREVIEW, EARLY_ACCESS])

    # A lapsed sponsorship is deactivated, not deleted: conferral stops at once
    # while the membership rows survive, so reactivating restores it:
    orgs.set_active(db, org, False)
    assert _effective(user) == []
    assert not user.has_capability(FEATURE_PREVIEW)
    assert db.query(OrganisationMember).count() == 1

    orgs.set_active(db, org, True)
    assert _effective(user) == sorted([FEATURE_PREVIEW, EARLY_ACCESS])


def test_set_granted_capabilities_takes_effect_with_no_sync_step(db):
    org = _sponsor(db, caps=(FEATURE_PREVIEW,))
    one = _user(db, "one@x.com")
    two = _user(db, "two@x.com")
    orgs.add_member(db, org, one)
    orgs.add_member(db, org, two)

    # No re-sync is called anywhere below — conferral is computed, so changing
    # the organisation changes every member's effective set immediately:
    orgs.set_granted_capabilities(db, org, [FEATURE_PREVIEW, EARLY_ACCESS])
    assert _effective(one) == sorted([FEATURE_PREVIEW, EARLY_ACCESS])
    assert _effective(two) == sorted([FEATURE_PREVIEW, EARLY_ACCESS])

    # Removing a grant from the organisation revokes it from every member:
    orgs.set_granted_capabilities(db, org, [EARLY_ACCESS])
    assert _effective(one) == [EARLY_ACCESS]
    assert _effective(two) == [EARLY_ACCESS]
    assert not one.has_capability(FEATURE_PREVIEW)


# -- revocation ----------------------------------------------------------------

def test_remove_member_drops_conferred_grants_but_keeps_direct_ones(db):
    # One grant held directly, one conferred by the organisation:
    user = _user(db, "ops@x.com", capabilities=[INBOX_READ])
    org = _sponsor(db, caps=(FEATURE_PREVIEW,))

    orgs.add_member(db, org, user)
    assert _effective(user) == sorted([INBOX_READ, FEATURE_PREVIEW])

    orgs.remove_member(db, org, user)

    # Only the conferred one falls away; the direct grant is untouched, because
    # nothing in the service ever wrote to that column:
    assert _effective(user) == [INBOX_READ]
    assert user.capabilities == [INBOX_READ]
    assert db.query(OrganisationMember).count() == 0


def test_direct_grant_survives_membership_churn(db):
    user = _user(db, "churn@x.com", capabilities=[USERS_READ])
    org = _sponsor(db, caps=(FEATURE_PREVIEW,))

    for _ in range(3):
        orgs.add_member(db, org, user)
        assert user.has_capability(USERS_READ)
        assert user.has_capability(FEATURE_PREVIEW)
        orgs.remove_member(db, org, user)
        assert _effective(user) == [USERS_READ]


def test_a_grant_held_both_directly_and_by_conferral_survives_revocation(db):
    # The overlap case a materialised design gets wrong: leaving the
    # organisation must not strip a grant the account holds in its own right:
    user = _user(db, "both-ways@x.com", capabilities=[FEATURE_PREVIEW])
    org = _sponsor(db, caps=(FEATURE_PREVIEW,))

    orgs.add_member(db, org, user)
    orgs.remove_member(db, org, user)

    assert user.has_capability(FEATURE_PREVIEW)
    assert user.capabilities == [FEATURE_PREVIEW]


def test_leaving_one_of_two_organisations_keeps_the_others_grants(db):
    sponsor = _sponsor(db, caps=(FEATURE_PREVIEW,))
    society = orgs.create_organisation(db, "quant-soc", "Quant Soc", granted_capabilities=[EARLY_ACCESS])
    user = _user(db, "both@x.com")
    orgs.add_member(db, sponsor, user)
    orgs.add_member(db, society, user)

    orgs.remove_member(db, sponsor, user)

    assert _effective(user) == [EARLY_ACCESS]


def test_remove_member_is_a_no_op_when_there_is_no_membership(db):
    org = _sponsor(db)
    user = _user(db, "stranger@x.com", capabilities=[INBOX_READ])

    orgs.remove_member(db, org, user)

    assert _effective(user) == [INBOX_READ]


def test_deleting_the_organisation_removes_conferral(db):
    org = _sponsor(db, caps=(FEATURE_PREVIEW,))
    user = _user(db, "gone@x.com", capabilities=[INBOX_READ])
    orgs.add_member(db, org, user)

    db.delete(org)
    db.commit()
    db.refresh(user)

    assert _effective(user) == [INBOX_READ]


# -- membership as a queryable relation ---------------------------------------

def test_members_of_lists_the_organisations_people(db):
    # The query capabilities cannot answer, and a reason organisations are a
    # relation rather than a grant string:
    org = _sponsor(db)
    one = _user(db, "one@x.com")
    two = _user(db, "two@x.com")
    _user(db, "outsider@x.com")
    orgs.add_member(db, org, one)
    orgs.add_member(db, org, two)

    assert sorted(u.email for u in orgs.members_of(db, org)) == ["one@x.com", "two@x.com"]


# -- admin ---------------------------------------------------------------------

def test_admin_is_unaffected_by_conferral(db):
    admin = _user(db, "admin@x.com", role=Role.ADMIN)
    org = _sponsor(db, caps=(FEATURE_PREVIEW,))

    # Admin holds everything implicitly, with or without a membership — and
    # effective_capabilities deliberately does not fold admin in, since admin is
    # an identity that passes checks, not a set of grants:
    assert admin.has_capability(INBOX_READ)
    assert orgs.effective_capabilities(admin) == set()

    orgs.add_member(db, org, admin)
    assert admin.role == Role.ADMIN
    assert admin.has_capability(INBOX_READ)

    orgs.remove_member(db, org, admin)
    assert admin.role == Role.ADMIN
    assert admin.has_capability(INBOX_READ)


def test_a_user_with_no_memberships_is_unchanged(db):
    user = _user(db, "plain@x.com", capabilities=[INBOX_READ])
    assert _effective(user) == [INBOX_READ]

    bare = _user(db, "bare@x.com")
    assert orgs.effective_capabilities(bare) == set()
    assert not bare.has_capability(FEATURE_PREVIEW)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
