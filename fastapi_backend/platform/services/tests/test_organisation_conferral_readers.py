"""The two readers that had to learn about conferral.

Computing the effective set instead of materialising it costs exactly two
Python readers and one SQL-level candidate filter. `User.has_capability` and
`verify_capability` are covered by the gate test; the remaining two surfaces
are pinned here, because both would fail SILENTLY rather than loudly:

- `UserResponse.capabilities` must serialise the EFFECTIVE set, or the frontend
  contract (and its hasCapability) would disagree with the backend gate.
- `push.py`'s coarse candidate filter must include organisation members, or a
  member whose inbox:read is org-conferred would simply stop being notified —
  no error, just missing pings.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from infrastructure.database import Base
import platform.models  # noqa: F401 — registers User on Base.metadata
from platform.capabilities import EARLY_ACCESS, FEATURE_PREVIEW, INBOX_READ, USERS_READ
from platform.exceptions import UnknownCapabilityException
from platform.enums import Role
from platform.models import PushSubscription, User
from platform.schemas import UserResponse
from platform.services import organisation_service as orgs
from platform.services.notification_service import push


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


def _subscribe(db, user: User) -> PushSubscription:
    row = PushSubscription(user_id=user.id, endpoint=f"https://push.example/{user.id}",
                           p256dh="k", auth="a")
    db.add(row)
    db.commit()
    return row


# -- UserResponse.organisations ------------------------------------------------
#
# Capabilities say WHAT an account may do; they cannot say WHICH organisation it
# belongs to, because a grant is not parameterised. Personalization needs the
# latter (the sponsor proposal resolves the firm from the slug), so the contract
# carries organisation IDENTITY alongside the effective grants. These pin that
# it stays identity-only and stays in step with conferral.

def test_user_response_exposes_the_organisations_the_account_belongs_to(db):
    user = _user(db, "sarah@x.com")
    org = orgs.create_organisation(db, "hudson-river-trading", "Hudson River Trading",
                                   logo_url="/sponsors/hrt.svg",
                                   granted_capabilities=[FEATURE_PREVIEW])
    orgs.add_member(db, org, user)

    payload = UserResponse.model_validate(user)

    assert [o.slug for o in payload.organisations] == ["hudson-river-trading"]
    assert payload.organisations[0].name == "Hudson River Trading"
    assert payload.organisations[0].logo_url == "/sponsors/hrt.svg"


def test_user_response_organisations_is_empty_without_membership(db):
    # The shape every ordinary account has - and the shape the frontend must
    # deny on, rather than treating "no organisations" as "any organisation":
    assert UserResponse.model_validate(_user(db, "plain@x.com")).organisations == []


def test_user_response_hides_an_inactive_organisation(db):
    # Active-only, matching conferral exactly: an inactive organisation confers
    # nothing, so personalising for it would honour a lapsed relation.
    user = _user(db, "lapsed@x.com")
    org = orgs.create_organisation(db, "sponsor", "Sponsor", granted_capabilities=[FEATURE_PREVIEW])
    orgs.add_member(db, org, user)
    orgs.set_active(db, org, False)

    assert UserResponse.model_validate(user).organisations == []


def test_user_response_drops_the_organisation_on_revocation(db):
    user = _user(db, "sarah@x.com")
    org = orgs.create_organisation(db, "sponsor", "Sponsor", granted_capabilities=[FEATURE_PREVIEW])
    orgs.add_member(db, org, user)
    orgs.remove_member(db, org, user)

    assert UserResponse.model_validate(user).organisations == []


def test_user_response_organisations_carries_no_capabilities(db):
    # Identity, never authorization: nothing in the exposed shape may let a
    # client infer or assert a grant from a membership.
    user = _user(db, "sarah@x.com")
    org = orgs.create_organisation(db, "sponsor", "Sponsor",
                                   granted_capabilities=[FEATURE_PREVIEW, EARLY_ACCESS])
    orgs.add_member(db, org, user)

    exposed = UserResponse.model_validate(user).organisations[0].model_dump()

    assert set(exposed) == {"slug", "name", "logo_url"}


# -- UserResponse --------------------------------------------------------------

def test_user_response_exposes_the_effective_set(db):
    user = _user(db, "sarah@x.com", capabilities=[USERS_READ])
    org = orgs.create_organisation(db, "hudson-river-trading", "Hudson River Trading",
                                   granted_capabilities=[FEATURE_PREVIEW, EARLY_ACCESS])
    orgs.add_member(db, org, user)

    payload = UserResponse.model_validate(user)

    # Direct and conferred are indistinguishable to the client, which is what
    # keeps the frontend contract and hasCapability unchanged:
    assert sorted(payload.capabilities) == sorted([USERS_READ, FEATURE_PREVIEW, EARLY_ACCESS])


def test_user_response_drops_conferred_grants_on_revocation(db):
    user = _user(db, "sarah@x.com", capabilities=[USERS_READ])
    org = orgs.create_organisation(db, "sponsor", "Sponsor", granted_capabilities=[FEATURE_PREVIEW])
    orgs.add_member(db, org, user)
    orgs.remove_member(db, org, user)

    assert UserResponse.model_validate(user).capabilities == [USERS_READ]


def test_user_response_is_unchanged_for_an_account_with_no_memberships(db):
    user = _user(db, "plain@x.com", capabilities=[USERS_READ])
    assert UserResponse.model_validate(user).capabilities == [USERS_READ]

    bare = _user(db, "bare@x.com")
    assert UserResponse.model_validate(bare).capabilities == []


def test_user_response_hides_an_inactive_organisations_grants(db):
    user = _user(db, "lapsed@x.com")
    org = orgs.create_organisation(db, "sponsor", "Sponsor", granted_capabilities=[FEATURE_PREVIEW])
    orgs.add_member(db, org, user)
    orgs.set_active(db, org, False)

    assert UserResponse.model_validate(user).capabilities == []


# -- push targeting ------------------------------------------------------------

def test_an_organisation_cannot_confer_an_operator_capability(db):
    """Conferral is restricted to CONFERRABLE_CAPABILITIES.

    A direct grant is one reviewed decision per account; a conferred one applies
    to an unbounded, later-growing membership. Operator capabilities therefore
    stay one-account-at-a-time:
    """
    with pytest.raises(UnknownCapabilityException):
        orgs.create_organisation(db, "ops-co", "Ops Co",
                                 granted_capabilities=[INBOX_READ])


def test_push_arm_finds_conferred_readers_if_the_policy_ever_widens(db, monkeypatch):
    """The push candidate filter has an organisation arm even though no
    conferrable capability grants inbox access today.

    It is defensive, not dead: widening CONFERRABLE_CAPABILITIES must not
    silently drop conferred readers from inbox pings. Widening it here proves the
    arm works, so the guarantee is pinned rather than assumed:
    """
    monkeypatch.setattr(orgs, "CONFERRABLE_CAPABILITIES", frozenset({INBOX_READ}))
    # The account holds NOTHING directly, so the candidate filter's admin and
    # direct-grant arms would both skip it:
    member = _user(db, "ops@x.com")
    org = orgs.create_organisation(db, "ops-co", "Ops Co", granted_capabilities=[INBOX_READ])
    orgs.add_member(db, org, member)
    _subscribe(db, member)

    subscriptions = push._subscriptions_for(db, push.Audience.INBOX_READERS)
    assert [s.user.email for s in subscriptions] == ["ops@x.com"]

    # ...and ends the moment the membership does:
    orgs.remove_member(db, org, member)
    assert push._subscriptions_for(db, push.Audience.INBOX_READERS) == []


def test_push_targeting_still_excludes_ordinary_users(db):
    # Inbox pings must not reach end users who merely opted into announcements,
    # including members of an organisation that confers something unrelated:
    plain = _user(db, "plain@x.com")
    _subscribe(db, plain)

    sponsored = _user(db, "sponsored@x.com")
    sponsor = orgs.create_organisation(db, "sponsor", "Sponsor",
                                       granted_capabilities=[FEATURE_PREVIEW])
    orgs.add_member(db, sponsor, sponsored)
    _subscribe(db, sponsored)

    assert push._subscriptions_for(db, push.Audience.INBOX_READERS) == []


def test_push_targeting_keeps_admins_and_direct_grant_holders(db):
    admin = _user(db, "admin@x.com", role=Role.ADMIN)
    direct = _user(db, "direct@x.com", capabilities=[INBOX_READ])
    _subscribe(db, admin)
    _subscribe(db, direct)

    emails = {s.user.email for s in push._subscriptions_for(db, push.Audience.INBOX_READERS)}
    assert emails == {"admin@x.com", "direct@x.com"}
