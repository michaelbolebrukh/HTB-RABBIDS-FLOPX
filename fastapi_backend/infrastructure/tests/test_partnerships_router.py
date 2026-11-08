"""Partnership router auth matrix: two audiences, one resource, one primitive.

The member-facing read is the data-scoped case — capabilities are not
parameterised, so "HRT's own people" cannot be a grant string and has to be a
check over the membership rows — while every cross-organisation and every write
surface gates on require_capability(PARTNERSHIPS_MANAGE). What this file pins is
that BOTH doors admit admin implicitly and neither ever asks about is_admin: the
member-facing check runs through has_capability, the same rule the dependency
applies.

The member door is also narrower than the manage door in three ways this file
pins, all of them about what ends up in a response body rather than what a UI
chooses to render: refusal is indistinguishable from not-found (no oracle over
which firms are in our pipeline), draft rows are withheld, and an inactive
organisation stops opening the door at all.

Real database, real router, real capability closures; only the session
resolution is stubbed (token plumbing is covered by test_capability_dependency).
"""

from datetime import date

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

import infrastructure.dependencies as deps
from infrastructure.database import Base
import platform.models  # noqa: F401 — registers the models on Base.metadata
from platform.capabilities import INBOX_READ, PARTNERSHIPS_MANAGE
from platform.enums import Role
from platform.models import User
from platform.routers.partnerships import router
from platform.services import organisation_service as orgs
from platform.services import partnership_service as ps


AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _client(monkeypatch, db, user: User) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[deps.get_db] = lambda: db
    # The member-facing endpoint resolves its user through the dependency alias
    # (overridden here); the require_capability closure resolves the same
    # function by module global at call time, so both paths land on `user` while
    # the REAL capability checks still run:
    app.dependency_overrides[deps.get_current_verified_user] = lambda: user
    monkeypatch.setattr(deps, "get_current_verified_user", lambda db_, token: user)
    return TestClient(app)


def _user(db, email, *, role=Role.USER, capabilities=None) -> User:
    user = User(name="X", email=email, role=role, capabilities=capabilities, is_verified=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def sponsor(db):
    """An organisation with one signed partnership for the current season."""
    organisation = orgs.create_organisation(db, "hudson-river-trading", "Hudson River Trading")
    ps.create_partnership(
        db, organisation, season="2026/27", status="signed",
        offering_label="Talent Partner", snapshot_key="hudson-river-trading-2026-27",
        signed_on=date(2026, 7, 28), starts_on=date(2026, 9, 1), ends_on=date(2027, 8, 31),
    )
    return organisation


# -- GET /partnerships/for-org/{slug} ------------------------------------------

def test_a_member_reads_their_own_organisations_partnerships(monkeypatch, db, sponsor):
    member = _user(db, "sarah@hrt.com")
    orgs.add_member(db, sponsor, member)

    response = _client(monkeypatch, db, member).get(
        "/partnerships/for-org/hudson-river-trading", headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    row = body[0]
    assert row["organisation_slug"] == "hudson-river-trading"
    assert row["organisation_name"] == "Hudson River Trading"
    assert row["status"] == "signed"
    assert row["season"] == "2026/27"
    assert row["offering_label"] == "Talent Partner"
    assert row["snapshot_key"] == "hudson-river-trading-2026-27"
    assert row["starts_on"] == "2026-09-01" and row["ends_on"] == "2027-08-31"
    assert row["signed_on"] == "2026-07-28"
    # Expiry is computed per response, never stored:
    assert row["is_expired"] is False and isinstance(row["days_remaining"], int)


def test_a_non_member_without_the_grant_gets_the_not_found(monkeypatch, db, sponsor):
    # Unauthorized collapses onto not-found: a 403 here would confirm the
    # organisation exists, which is the enumeration this endpoint must not offer:
    outsider = _user(db, "nobody@x.com")
    response = _client(monkeypatch, db, outsider).get(
        "/partnerships/for-org/hudson-river-trading", headers=AUTH)
    assert response.status_code == 404


def test_an_unrelated_grant_does_not_open_the_door(monkeypatch, db, sponsor):
    # Grants are not interchangeable — only partnerships:manage substitutes for
    # membership here:
    other = _user(db, "ops@x.com", capabilities=[INBOX_READ])
    response = _client(monkeypatch, db, other).get(
        "/partnerships/for-org/hudson-river-trading", headers=AUTH)
    assert response.status_code == 404


def test_a_real_org_and_an_imaginary_one_are_indistinguishable(monkeypatch, db, sponsor):
    """The whole point of the collapse: identical responses, so no oracle."""
    outsider = _user(db, "nobody@x.com")
    client = _client(monkeypatch, db, outsider)

    real = client.get("/partnerships/for-org/hudson-river-trading", headers=AUTH)
    imaginary = client.get("/partnerships/for-org/citadel", headers=AUTH)

    assert real.status_code == imaginary.status_code == 404
    assert real.json() == imaginary.json()


def test_a_manage_holder_still_tells_the_two_apart(monkeypatch, db, sponsor):
    # The grant is what buys the distinction — an operator needs to know whether
    # a slug is wrong or the org is simply missing:
    manager = _user(db, "manager@x.com", capabilities=[PARTNERSHIPS_MANAGE])
    client = _client(monkeypatch, db, manager)

    assert client.get("/partnerships/for-org/hudson-river-trading", headers=AUTH).status_code == 200
    assert client.get("/partnerships/for-org/citadel", headers=AUTH).status_code == 404


# -- drafts are not the sponsor's business yet ---------------------------------

def test_a_member_never_sees_a_draft_row(monkeypatch, db, sponsor):
    # Next season's thinking about this firm — labels, dates, the fact we are
    # drafting at all — stays ours until somebody sends it:
    ps.create_partnership(db, sponsor, season="2027/28", status="draft",
                          offering_label="Headline Partner", starts_on=date(2027, 9, 1))
    member = _user(db, "sarah@hrt.com")
    orgs.add_member(db, sponsor, member)

    response = _client(monkeypatch, db, member).get(
        "/partnerships/for-org/hudson-river-trading", headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    # Filtered in the RESPONSE, not at the client — a row here is devtools-visible:
    assert [row["season"] for row in body] == ["2026/27"]
    assert all(row["status"] != "draft" for row in body)


def test_a_manage_holder_sees_the_draft_alongside_the_signed_row(monkeypatch, db, sponsor):
    ps.create_partnership(db, sponsor, season="2027/28", status="draft",
                          offering_label="Headline Partner", starts_on=date(2027, 9, 1))
    manager = _user(db, "manager@x.com", capabilities=[PARTNERSHIPS_MANAGE])

    response = _client(monkeypatch, db, manager).get(
        "/partnerships/for-org/hudson-river-trading", headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    # Newest start date first, and both statuses present:
    assert [row["season"] for row in body] == ["2027/28", "2026/27"]
    assert {row["status"] for row in body} == {"draft", "signed"}


def test_a_sent_row_does_reach_the_member(monkeypatch, db, sponsor):
    # Only DRAFT is withheld — sending a proposal is the decision to show it:
    ps.create_partnership(db, sponsor, season="2027/28", status="sent",
                          starts_on=date(2027, 9, 1))
    member = _user(db, "sarah@hrt.com")
    orgs.add_member(db, sponsor, member)

    body = _client(monkeypatch, db, member).get(
        "/partnerships/for-org/hudson-river-trading", headers=AUTH).json()
    assert [row["season"] for row in body] == ["2027/28", "2026/27"]


# -- a lapsed organisation stops opening the door ------------------------------

def test_a_member_of_an_inactive_organisation_is_not_found(monkeypatch, db, sponsor):
    # The same rule effective_capabilities applies: an inactive organisation
    # confers nothing, so it stops opening its member-facing read too:
    member = _user(db, "sarah@hrt.com")
    orgs.add_member(db, sponsor, member)
    orgs.set_active(db, sponsor, False)

    response = _client(monkeypatch, db, member).get(
        "/partnerships/for-org/hudson-river-trading", headers=AUTH)
    assert response.status_code == 404


def test_a_manage_holder_reads_an_inactive_organisation(monkeypatch, db, sponsor):
    # Operators need the lapsed rows most — "who did we lose" is the question:
    orgs.set_active(db, sponsor, False)
    manager = _user(db, "manager@x.com", capabilities=[PARTNERSHIPS_MANAGE])

    response = _client(monkeypatch, db, manager).get(
        "/partnerships/for-org/hudson-river-trading", headers=AUTH)
    assert response.status_code == 200 and len(response.json()) == 1


def test_a_manage_holder_reads_any_organisation(monkeypatch, db, sponsor):
    manager = _user(db, "manager@x.com", capabilities=[PARTNERSHIPS_MANAGE])
    response = _client(monkeypatch, db, manager).get(
        "/partnerships/for-org/hudson-river-trading", headers=AUTH)
    assert response.status_code == 200 and len(response.json()) == 1


def test_admin_passes_implicitly_without_holding_the_grant(monkeypatch, db, sponsor):
    admin = _user(db, "admin@x.com", role=Role.ADMIN, capabilities=None)
    response = _client(monkeypatch, db, admin).get(
        "/partnerships/for-org/hudson-river-trading", headers=AUTH)
    assert response.status_code == 200


def test_an_organisation_with_no_partnership_returns_an_empty_list(monkeypatch, db):
    prospect = orgs.create_organisation(db, "jane-street", "Jane Street")
    member = _user(db, "sam@js.com")
    orgs.add_member(db, prospect, member)

    response = _client(monkeypatch, db, member).get("/partnerships/for-org/jane-street", headers=AUTH)
    # Absence is what the client reads as "prospect" — not an error:
    assert response.status_code == 200 and response.json() == []


def test_an_unknown_slug_is_a_404(monkeypatch, db):
    manager = _user(db, "manager@x.com", capabilities=[PARTNERSHIPS_MANAGE])
    response = _client(monkeypatch, db, manager).get("/partnerships/for-org/nope", headers=AUTH)
    assert response.status_code == 404


# -- GET /partnerships ---------------------------------------------------------

def test_listing_every_partnership_requires_the_grant(monkeypatch, db, sponsor):
    member = _user(db, "sarah@hrt.com")
    orgs.add_member(db, sponsor, member)
    # Membership opens one organisation's row, never the cross-organisation view:
    assert _client(monkeypatch, db, member).get("/partnerships", headers=AUTH).status_code == 403


def test_a_manage_holder_lists_every_partnership(monkeypatch, db, sponsor):
    other = orgs.create_organisation(db, "jane-street", "Jane Street")
    ps.create_partnership(db, other, season="2026/27", status="draft", ends_on=date(2026, 12, 31))
    manager = _user(db, "manager@x.com", capabilities=[PARTNERSHIPS_MANAGE])

    response = _client(monkeypatch, db, manager).get("/partnerships", headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    # Soonest end date first — the shape an "expiring soon" view reads:
    assert [row["organisation_slug"] for row in body] == ["jane-street", "hudson-river-trading"]


def test_admin_lists_every_partnership_implicitly(monkeypatch, db, sponsor):
    admin = _user(db, "admin@x.com", role=Role.ADMIN, capabilities=None)
    assert _client(monkeypatch, db, admin).get("/partnerships", headers=AUTH).status_code == 200


# -- POST /partnerships --------------------------------------------------------

def test_creating_a_partnership_requires_the_grant(monkeypatch, db, sponsor):
    plain = _user(db, "plain@x.com")
    response = _client(monkeypatch, db, plain).post(
        "/partnerships", headers=AUTH,
        json={"organisation_slug": "hudson-river-trading", "season": "2027/28"})
    assert response.status_code == 403


def test_a_manage_holder_creates_by_slug(monkeypatch, db, sponsor):
    manager = _user(db, "manager@x.com", capabilities=[PARTNERSHIPS_MANAGE])

    response = _client(monkeypatch, db, manager).post(
        "/partnerships", headers=AUTH,
        json={"organisation_slug": "hudson-river-trading", "season": "2027/28",
              "status": "draft", "offering_label": "Talent Partner"})

    assert response.status_code == 201
    body = response.json()
    assert body["season"] == "2027/28" and body["status"] == "draft"
    # A renewal is a NEW row; the signed season is still there:
    assert len(ps.partnerships_for_organisation(db, sponsor)) == 2


def test_a_manage_holder_creates_by_organisation_id(monkeypatch, db, sponsor):
    manager = _user(db, "manager@x.com", capabilities=[PARTNERSHIPS_MANAGE])
    response = _client(monkeypatch, db, manager).post(
        "/partnerships", headers=AUTH,
        json={"organisation_id": sponsor.id, "season": "2027/28"})
    assert response.status_code == 201
    assert response.json()["organisation_slug"] == "hudson-river-trading"


def test_creating_without_an_organisation_is_a_422(monkeypatch, db):
    manager = _user(db, "manager@x.com", capabilities=[PARTNERSHIPS_MANAGE])
    response = _client(monkeypatch, db, manager).post(
        "/partnerships", headers=AUTH, json={"season": "2027/28"})
    assert response.status_code == 422


def test_creating_for_an_unknown_organisation_is_a_404(monkeypatch, db):
    manager = _user(db, "manager@x.com", capabilities=[PARTNERSHIPS_MANAGE])
    response = _client(monkeypatch, db, manager).post(
        "/partnerships", headers=AUTH, json={"organisation_slug": "nope", "season": "2027/28"})
    assert response.status_code == 404


def test_an_unknown_status_is_refused_with_the_registry_in_the_message(monkeypatch, db, sponsor):
    manager = _user(db, "manager@x.com", capabilities=[PARTNERSHIPS_MANAGE])
    response = _client(monkeypatch, db, manager).post(
        "/partnerships", headers=AUTH,
        json={"organisation_slug": "hudson-river-trading", "season": "2027/28", "status": "signd"})
    assert response.status_code == 400
    assert "signd" in response.json()["detail"]


# -- PATCH /partnerships/{id} --------------------------------------------------

def _only_partnership_id(db) -> int:
    return ps.list_partnerships(db)[0].id


def test_updating_requires_the_grant(monkeypatch, db, sponsor):
    plain = _user(db, "plain@x.com")
    response = _client(monkeypatch, db, plain).patch(
        f"/partnerships/{_only_partnership_id(db)}", headers=AUTH, json={"status": "ended"})
    assert response.status_code == 403


def test_a_member_cannot_change_their_own_organisations_partnership(monkeypatch, db, sponsor):
    # Reading is membership-scoped; WRITING is never — it is the one primitive:
    member = _user(db, "sarah@hrt.com")
    orgs.add_member(db, sponsor, member)
    response = _client(monkeypatch, db, member).patch(
        f"/partnerships/{_only_partnership_id(db)}", headers=AUTH, json={"status": "ended"})
    assert response.status_code == 403


def test_marking_a_partnership_signed_leaves_its_dates_alone(monkeypatch, db, sponsor):
    partnership = ps.create_partnership(
        db, sponsor, season="2027/28", status="sent",
        offering_label="Talent Partner", starts_on=date(2027, 9, 1), ends_on=date(2028, 8, 31),
    )
    manager = _user(db, "manager@x.com", capabilities=[PARTNERSHIPS_MANAGE])

    response = _client(monkeypatch, db, manager).patch(
        f"/partnerships/{partnership.id}", headers=AUTH,
        json={"status": "signed", "signed_on": "2027-07-01"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "signed" and body["signed_on"] == "2027-07-01"
    # The fields the call never mentioned survive it:
    assert body["offering_label"] == "Talent Partner"
    assert body["starts_on"] == "2027-09-01" and body["ends_on"] == "2028-08-31"


def test_updating_an_unknown_partnership_is_a_404(monkeypatch, db):
    manager = _user(db, "manager@x.com", capabilities=[PARTNERSHIPS_MANAGE])
    response = _client(monkeypatch, db, manager).patch(
        "/partnerships/999", headers=AUTH, json={"status": "signed"})
    assert response.status_code == 404


def test_clearing_a_date_is_still_allowed(monkeypatch, db, sponsor):
    # The nullable columns keep their "explicit null clears it" semantics — the
    # null guard is about the NOT NULL ones only:
    manager = _user(db, "manager@x.com", capabilities=[PARTNERSHIPS_MANAGE])
    partnership_id = _only_partnership_id(db)

    response = _client(monkeypatch, db, manager).patch(
        f"/partnerships/{partnership_id}", headers=AUTH, json={"ends_on": None})

    assert response.status_code == 200 and response.json()["ends_on"] is None


def test_a_null_status_is_refused_rather_than_resetting_the_row(monkeypatch, db, sponsor):
    # A null status used to read as "unset" and hand the create-path default
    # back, quietly demoting a signed partnership to draft:
    manager = _user(db, "manager@x.com", capabilities=[PARTNERSHIPS_MANAGE])
    partnership_id = _only_partnership_id(db)

    response = _client(monkeypatch, db, manager).patch(
        f"/partnerships/{partnership_id}", headers=AUTH, json={"status": None})

    assert response.status_code == 422
    assert ps.get_partnership(db, partnership_id).status == "signed"


def test_a_null_season_is_refused_rather_than_hitting_the_not_null_column(monkeypatch, db, sponsor):
    manager = _user(db, "manager@x.com", capabilities=[PARTNERSHIPS_MANAGE])
    partnership_id = _only_partnership_id(db)

    response = _client(monkeypatch, db, manager).patch(
        f"/partnerships/{partnership_id}", headers=AUTH, json={"season": None})

    # A 422 the client can act on, not the 500 the database would have raised:
    assert response.status_code == 422
    assert ps.get_partnership(db, partnership_id).season == "2026/27"


def test_updating_to_an_unknown_status_is_refused(monkeypatch, db, sponsor):
    manager = _user(db, "manager@x.com", capabilities=[PARTNERSHIPS_MANAGE])
    partnership_id = _only_partnership_id(db)

    response = _client(monkeypatch, db, manager).patch(
        f"/partnerships/{partnership_id}", headers=AUTH, json={"status": "lapsed"})

    assert response.status_code == 400
    assert ps.get_partnership(db, partnership_id).status == "signed"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
