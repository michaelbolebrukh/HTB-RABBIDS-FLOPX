"""Refresh rotation must tolerate the tabs a browser actually opens.

A restored browser window boots N tabs at once; each one boots its own auth
provider with an empty in-memory access token, and each fires a refresh against
the same shared cookie. They therefore present the SAME refresh jti. Exactly one
wins the row lock and rotates; the rest land on the rotation grace window.

That used to be survivable-if-ugly, because the losers' sibling tabs still held
working access tokens. Now that revoking a session kills its access tokens
instantly (`assert_session_live`), a false-positive revoke here signs the user
out of every tab at once - so the grace window's exact semantics are load
bearing, and pinned here.
"""

import os
from datetime import datetime, timedelta, UTC

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

import infrastructure.dependencies as deps
from infrastructure.database import Base
from infrastructure.settings import settings as s
import platform.models  # noqa: F401 - registers the models on Base.metadata
from platform.models import RefreshSession, User
from platform.routers.auth import router as auth_router
from platform.services.auth_services.auth_service import create_session_and_tokens
from security import set_all_auth_cookies
from security.cookies import CSRF_COOKIE_NAME, REFRESH_COOKIE_NAME


app = FastAPI()
app.include_router(auth_router)


# Postgres only, and not for dialect trivia: the refresh path compares
# `expires_at` against an aware `now`, and SQLite hands back NAIVE datetimes for
# a TIMESTAMPTZ column, so the comparison raises there before any assertion runs.
# Postgres is the production target (see the database rule) - the right move is
# to test the real dialect, not to weaken the production comparison so SQLite
# can watch. CI's postgres leg sets TEST_DATABASE_URL; local runs skip unless
# one is provisioned.
_URL = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="TEST_DATABASE_URL is not Postgres; refresh rotation runs in the postgres leg",
)


@pytest.fixture(scope="module")
def engine():
    """Creating any missing tables - and NEVER dropping one.

    An earlier version of this fixture ran drop_all/create_all per test, which
    is fine against a throwaway database and catastrophic against any other:
    pointed at a local dev database it deleted every table, and the running app
    started 500ing on `relation "users" does not exist`. A test module is not
    entitled to destroy the schema it finds. Isolation comes from the
    transaction below instead, which is both safer and faster.
    """
    engine = create_engine(_URL, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine, checkfirst=True)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db(engine):
    """One session per test, inside a transaction that is always rolled back.

    The code under test commits (create_session_and_tokens, rotate, revoke), so
    the session joins an outer transaction and each commit becomes a SAVEPOINT
    release; the outer rollback then undoes everything, including rows committed
    through a different session in the same connection. Nothing this module does
    survives it - no cleanup to forget, and no row left behind for the next test
    to trip over.
    """
    connection = engine.connect()
    outer = connection.begin()
    session = sessionmaker(bind=connection, autocommit=False, autoflush=False)()
    session.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def restart_savepoint(sess, transaction):
        if transaction.nested and not transaction._parent.nested:
            sess.begin_nested()

    try:
        yield session
    finally:
        event.remove(session, "after_transaction_end", restart_savepoint)
        session.close()
        outer.rollback()
        connection.close()


@pytest.fixture
def client(db):
    app.dependency_overrides[deps.get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _user(db, email="tabs@x.com") -> User:
    user = User(name="X", email=email, is_verified=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _cookies_for(refresh_token: str, expiry) -> dict:
    """The cookie jar a real login would have left in the browser."""
    from fastapi import Response

    carrier = Response()
    set_all_auth_cookies(carrier, refresh_token, expiry)
    return {
        c.split("=", 1)[0]: c.split("=", 1)[1].split(";", 1)[0]
        for c in carrier.headers.getlist("set-cookie")
    }


def _refresh(client, jar: dict):
    """One tab's silent refresh, carrying the cookie jar it booted with.

    Deliberately re-seeded per call from the SAME jar rather than letting the
    client accumulate rotated cookies: that is the whole point - concurrent tabs
    each present the token they read before anyone rotated.
    """
    client.cookies.clear()
    client.cookies.set(REFRESH_COOKIE_NAME, jar[REFRESH_COOKIE_NAME])
    client.cookies.set(CSRF_COOKIE_NAME, jar[CSRF_COOKIE_NAME])
    return client.post("/auth/token/refresh", headers={"X-CSRF-Token": jar[CSRF_COOKIE_NAME]})


# -- the grace window ----------------------------------------------------------

def test_three_tabs_refreshing_the_same_token_all_succeed(client, db):
    """The regression this exists for: the third tab must not revoke the session.

    The grace window is bounded by TIME, not by a single use. Closing it on the
    first grace hit made tab three look like token reuse, revoking a session
    nobody attacked - and every tab would now be bounced to login instantly.
    """
    user = _user(db)
    session, _, rt = create_session_and_tokens(db, user.id)
    jar = _cookies_for(rt["refresh_token"], rt["refresh_token_expiry"])

    for tab in range(3):
        assert _refresh(client, jar).status_code == 200, f"tab {tab + 1} was refused"

    db.expire_all()
    assert db.get(RefreshSession, session.id).revoked_at is None


def test_the_grace_window_still_closes_on_time(client, db):
    """Multi-use is not unlimited-use: past the window the old jti is reuse."""
    user = _user(db)
    session, _, rt = create_session_and_tokens(db, user.id)
    jar = _cookies_for(rt["refresh_token"], rt["refresh_token_expiry"])

    assert _refresh(client, jar).status_code == 200          # rotates, opens the window

    row = db.get(RefreshSession, session.id)
    row.previous_valid_until = datetime.now(UTC) - timedelta(seconds=1)
    db.add(row)
    db.commit()

    assert _refresh(client, jar).status_code >= 400          # now genuine reuse
    db.expire_all()
    assert db.get(RefreshSession, session.id).revoked_at is not None


def test_the_grace_window_is_the_configured_length(db):
    """Pinning that rotation opens the window from now, not from some prior mark."""
    user = _user(db)
    session, _, _ = create_session_and_tokens(db, user.id)
    from platform.services.auth_services.auth_service import rotate_refresh_token

    before = datetime.now(UTC)
    rotate_refresh_token(db, session, user.id, str(session.id))
    db.refresh(session)

    valid_until = session.previous_valid_until
    if valid_until.tzinfo is None:
        valid_until = valid_until.replace(tzinfo=UTC)
    assert valid_until >= before + timedelta(seconds=s.REFRESH_PREVIOUS_JTI_GRACE_SECONDS - 1)


# -- the session limit ---------------------------------------------------------

def test_the_session_limit_evicts_a_live_session_not_an_expired_one(db):
    """Eviction must match the count's definition of "active", or it evicts nothing.

    The count only sees unexpired rows, so picking the oldest row *including*
    expired ones let a single stale row absorb every eviction while the live
    session count climbed past the cap unbounded.
    """
    user = _user(db)
    now = datetime.now(UTC)

    stale = RefreshSession(
        user_id=user.id, current_jti="stale",
        expires_at=now - timedelta(days=1),         # already expired, and the oldest
        last_used_at=now - timedelta(days=365),
    )
    db.add(stale)
    db.commit()

    live = [create_session_and_tokens(db, user.id)[0] for _ in range(s.MAX_SESSIONS_PER_USER)]

    # The next login is over the cap and must retire the oldest LIVE session:
    create_session_and_tokens(db, user.id)

    db.expire_all()
    assert db.get(RefreshSession, stale.id).revoked_at is None
    assert db.get(RefreshSession, live[0].id).revoked_at is not None

    active = db.query(RefreshSession).filter(
        RefreshSession.user_id == user.id,
        RefreshSession.revoked_at.is_(None),
        RefreshSession.expires_at > now,
    ).count()
    assert active == s.MAX_SESSIONS_PER_USER


def test_an_account_already_over_the_cap_is_brought_back_under_it(db):
    """Eviction has to converge, or an over-cap account never comes back.

    One eviction per login only breaks even: the account retires a session and
    gains one, so an account that is already over the limit - which the previous
    eviction bug produced, and which lowering MAX_SESSIONS_PER_USER produces for
    everyone - would sit above it forever.
    """
    user = _user(db)
    now = datetime.now(UTC)
    over_by = 4

    for i in range(s.MAX_SESSIONS_PER_USER + over_by):
        db.add(RefreshSession(
            user_id=user.id, current_jti=f"seeded-{i}",
            expires_at=now + timedelta(days=30),
            last_used_at=now - timedelta(days=over_by + s.MAX_SESSIONS_PER_USER - i),
        ))
    db.commit()

    create_session_and_tokens(db, user.id)

    db.expire_all()
    active = db.query(RefreshSession).filter(
        RefreshSession.user_id == user.id,
        RefreshSession.revoked_at.is_(None),
        RefreshSession.expires_at > now,
    ).count()
    assert active == s.MAX_SESSIONS_PER_USER, "one login must bring it back to the cap"


def test_a_never_used_session_is_evicted_first(db):
    """PostgreSQL sorts NULLs LAST on ASC, which would make the stalest row the
    last candidate rather than the first."""
    user = _user(db)
    now = datetime.now(UTC)

    never_used = RefreshSession(
        user_id=user.id, current_jti="never", expires_at=now + timedelta(days=30),
        last_used_at=None,
    )
    db.add(never_used)
    db.commit()

    used = [create_session_and_tokens(db, user.id)[0] for _ in range(s.MAX_SESSIONS_PER_USER - 1)]
    create_session_and_tokens(db, user.id)          # the one that trips the cap

    db.expire_all()
    assert db.get(RefreshSession, never_used.id).revoked_at is not None
    assert all(db.get(RefreshSession, r.id).revoked_at is None for r in used)


def test_concurrent_logins_cannot_push_past_the_session_cap(engine):
    """The cap is a security control, so it has to hold when it is being attacked.

    Counting and inserting are separate statements. Without serialising them, N
    simultaneous logins all read the same pre-insert count, all conclude there
    is room, and all insert - measured at 20 concurrent logins against a cap of
    10, that landed 15 live sessions. No predicate over the existing rows can
    see the problem, because the problem is a phantom insert.

    Real threads on real connections: this cannot be reproduced in one session.
    """
    import threading

    # Its OWN engine with a real pool: the module engine is StaticPool, which
    # hands every session the SAME connection - twenty threads would share one
    # transaction and the race under test could not happen at all.
    concurrent_engine = create_engine(_URL, pool_size=25, max_overflow=5)
    Session = sessionmaker(bind=concurrent_engine, autocommit=False, autoflush=False)
    setup = Session()
    user = _user(setup, "race@x.com")
    user_id = user.id
    setup.commit()
    setup.close()

    concurrent = s.MAX_SESSIONS_PER_USER * 2
    start = threading.Barrier(concurrent)
    errors = []

    def login():
        session = Session()
        try:
            start.wait(timeout=30)
            create_session_and_tokens(session, user_id)
        except Exception as exc:            # noqa: BLE001 - reported, not swallowed
            errors.append(exc)
            session.rollback()
        finally:
            session.close()

    threads = [threading.Thread(target=login) for _ in range(concurrent)]
    live = None
    try:
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=60)
    finally:
        # This test is the one place in the module that writes for real - it
        # needs its own pool, so it cannot use the rollback fixture - which
        # makes cleaning up its own responsibility, on every path including an
        # assertion failure or an interrupt. Nothing this module runs is
        # entitled to leave rows behind.
        live = _drain_user(Session, user_id)

    assert not errors, f"concurrent logins raised: {errors[:3]}"
    assert live <= s.MAX_SESSIONS_PER_USER, (
        f"{concurrent} concurrent logins left {live} live sessions, cap is {s.MAX_SESSIONS_PER_USER}"
    )
    concurrent_engine.dispose()


def _drain_user(Session, user_id: int) -> int:
    """Counting this user's live sessions, then removing every trace of them."""
    session = Session()
    try:
        live = session.query(RefreshSession).filter(
            RefreshSession.user_id == user_id,
            RefreshSession.revoked_at.is_(None),
            RefreshSession.expires_at > datetime.now(UTC),
        ).count()
        session.query(RefreshSession).filter(RefreshSession.user_id == user_id).delete()
        session.query(User).filter(User.id == user_id).delete()
        session.commit()
        return live
    finally:
        session.close()
