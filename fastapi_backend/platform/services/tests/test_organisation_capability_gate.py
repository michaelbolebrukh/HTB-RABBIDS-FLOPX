"""Conferral reaches the REAL gate, not just a column read.

The point of the computed design is that `require_capability` — the single
authorization primitive — admits an org-conferred grant without any gate
knowing organisations exist. Proving that needs the real thing, so this
exercises the actual dependency closure over a real JWT and a real database:
membership alone opens a gated endpoint, and losing it closes the endpoint
again on the very next request, with no sync step in between.

Also pinned here: the auth path EAGER-loads memberships and their
organisations, so resolving the effective set is one round-trip and never a
lazy load (the database rule's N+1 discipline — and a lazy load would raise
outright under raiseload).
"""

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, inspect as sa_inspect
from sqlalchemy.orm import object_session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

import infrastructure.dependencies as deps
from infrastructure.database import Base
import platform.models  # noqa: F401 — registers User on Base.metadata
from platform.capabilities import EARLY_ACCESS, FEATURE_PREVIEW, INBOX_READ
from platform.enums import Role
from platform.exceptions import CapabilityException
from platform.models import User
from platform.services import organisation_service as orgs
from platform.services.auth_services.auth_service import create_session_and_tokens


app = FastAPI()

preview_dependency = deps.require_capability(FEATURE_PREVIEW)


@app.get("/needs-preview")
def needs_preview(user: preview_dependency):
    return {"email": user.email}


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


@pytest.fixture
def client(db):
    app.dependency_overrides[deps.get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _user(db, email, *, role=Role.USER, capabilities=None) -> User:
    user = User(name="X", email=email, role=role, capabilities=capabilities, is_verified=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _auth(user: User) -> dict:
    """Minting through the real login path, so the token carries a live `sid`.

    Access tokens are session-bound (`assert_session_live` rejects one whose
    RefreshSession is missing, revoked or expired), so a hand-rolled token with
    no session is refused by construction - as it should be. Going through
    `create_session_and_tokens` also keeps these suites honest: they exercise
    the same mint path production does.
    """
    _, access, _ = create_session_and_tokens(object_session(user), user.id)
    return {"Authorization": f"Bearer {access['access_token']}"}


def test_membership_alone_opens_a_capability_gated_endpoint(client, db):
    user = _user(db, "sarah@x.com")
    # Holding nothing directly, the account is refused:
    assert client.get("/needs-preview", headers=_auth(user)).status_code == 403

    org = orgs.create_organisation(
        db, "hudson-river-trading", "Hudson River Trading",
        granted_capabilities=[FEATURE_PREVIEW, EARLY_ACCESS],
    )
    orgs.add_member(db, org, user, title="University Relations")

    # ...and admitted purely because it now belongs to a sponsor organisation.
    # Nothing was written to users.capabilities to make this work:
    response = client.get("/needs-preview", headers=_auth(user))
    assert response.status_code == 200
    assert response.json() == {"email": "sarah@x.com"}
    assert not user.capabilities


def test_losing_the_membership_closes_the_gate_again(client, db):
    user = _user(db, "sarah@x.com")
    org = orgs.create_organisation(
        db, "hudson-river-trading", "Hudson River Trading",
        granted_capabilities=[FEATURE_PREVIEW],
    )
    orgs.add_member(db, org, user)
    assert client.get("/needs-preview", headers=_auth(user)).status_code == 200

    # Revocation is the row delete and nothing else:
    orgs.remove_member(db, org, user)
    assert client.get("/needs-preview", headers=_auth(user)).status_code == 403


def test_deactivating_the_organisation_closes_the_gate_immediately(client, db):
    user = _user(db, "lapsed@x.com")
    org = orgs.create_organisation(db, "sponsor", "Sponsor", granted_capabilities=[FEATURE_PREVIEW])
    orgs.add_member(db, org, user)
    assert client.get("/needs-preview", headers=_auth(user)).status_code == 200

    orgs.set_active(db, org, False)

    assert client.get("/needs-preview", headers=_auth(user)).status_code == 403


def test_changing_what_the_organisation_confers_moves_the_gate(client, db):
    user = _user(db, "shift@x.com")
    org = orgs.create_organisation(db, "sponsor", "Sponsor", granted_capabilities=[EARLY_ACCESS])
    orgs.add_member(db, org, user)
    assert client.get("/needs-preview", headers=_auth(user)).status_code == 403

    orgs.set_granted_capabilities(db, org, [EARLY_ACCESS, FEATURE_PREVIEW])
    assert client.get("/needs-preview", headers=_auth(user)).status_code == 200


def test_conferral_never_widens_beyond_what_the_organisation_grants(client, db):
    # The gate stays exact: an organisation conferring feature:preview does not
    # smuggle in any other grant:
    user = _user(db, "narrow@x.com")
    org = orgs.create_organisation(db, "sponsor", "Sponsor", granted_capabilities=[FEATURE_PREVIEW])
    orgs.add_member(db, org, user)

    assert deps.verify_capability(user, FEATURE_PREVIEW) is None
    with pytest.raises(CapabilityException):
        deps.verify_capability(user, INBOX_READ)


def test_a_directly_held_grant_keeps_the_gate_open_after_leaving(client, db):
    # The revocation rule at the gate: a hand-granted feature:preview survives
    # membership churn, while a purely conferred one does not:
    user = _user(db, "both@x.com", capabilities=[FEATURE_PREVIEW])
    org = orgs.create_organisation(db, "sponsor", "Sponsor", granted_capabilities=[FEATURE_PREVIEW])

    orgs.add_member(db, org, user)
    orgs.remove_member(db, org, user)

    assert client.get("/needs-preview", headers=_auth(user)).status_code == 200


def test_admin_passes_the_gate_with_no_membership(client, db):
    admin = _user(db, "admin@x.com", role=Role.ADMIN)
    assert client.get("/needs-preview", headers=_auth(admin)).status_code == 200


# -- the auth path must not lazy-load ------------------------------------------

def test_get_user_eager_loads_memberships_and_their_organisations(db):
    """No N+1 on the hottest path in the app.

    Every gated request resolves the effective set, so if the relation were
    lazy the auth check would fire extra queries per request — and would raise
    under raiseload discipline. Asserting on the instrumentation state proves
    the load already happened, not merely that the value is reachable."""
    user = _user(db, "eager@x.com")
    org = orgs.create_organisation(db, "sponsor", "Sponsor", granted_capabilities=[FEATURE_PREVIEW])
    orgs.add_member(db, org, user)
    db.expunge_all()

    loaded = deps.get_user(db, user.id)

    # The collection is already populated, not a pending lazy load:
    assert "organisation_memberships" not in sa_inspect(loaded).unloaded
    memberships = loaded.organisation_memberships
    assert len(memberships) == 1
    # ...and so is the organisation hanging off each membership, which
    # effective_capabilities reads for is_active and granted_capabilities:
    assert "organisation" not in sa_inspect(memberships[0]).unloaded


def test_effective_set_resolves_without_touching_the_database_again(db):
    """The eager load makes the capability check itself query-free."""
    user = _user(db, "noquery@x.com")
    org = orgs.create_organisation(db, "sponsor", "Sponsor", granted_capabilities=[FEATURE_PREVIEW])
    orgs.add_member(db, org, user)
    db.expunge_all()

    loaded = deps.get_user(db, user.id)

    queries: list[str] = []
    from sqlalchemy import event

    def _record(conn, cursor, statement, *args):
        queries.append(statement)

    event.listen(db.get_bind(), "before_cursor_execute", _record)
    try:
        assert loaded.has_capability(FEATURE_PREVIEW)
        deps.verify_capability(loaded, FEATURE_PREVIEW)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", _record)

    assert queries == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
