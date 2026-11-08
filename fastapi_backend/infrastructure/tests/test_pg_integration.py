"""
Postgres integration tests — exercise the real production dialect (JSONB, UUID,
TIMESTAMPTZ, real transactions) that the SQLite unit suite cannot reach.

Skipped unless TEST_DATABASE_URL points at Postgres. CI sets it to the postgres
service (the same one the migration-parity step uses), so this runs there; local
`pytest` leaves it unset and the module is skipped, keeping local runs SQLite-only
and green.
"""

import os
import uuid

import pytest
from sqlalchemy import Column, create_engine, inspect
from sqlalchemy.orm import declarative_base, sessionmaker

from infrastructure.db_types import JSONColumn, Uuid
from infrastructure.settings import settings as s
from platform.models import User
from platform.enums import Role
from infrastructure import seeder


_URL = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="TEST_DATABASE_URL is not Postgres; PG integration tests run only in the CI postgres leg",
)


# Self-contained probe on its own metadata so the portable db_types (JSONColumn,
# Uuid) are exercised against real Postgres without coupling to a production
# model's required columns:
_ProbeBase = declarative_base()


class _RoundTripProbe(_ProbeBase):
    __tablename__ = "pg_roundtrip_probe"
    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    payload = Column(JSONColumn, nullable=False)


@pytest.fixture()
def engine():
    eng = create_engine(_URL)
    try:
        yield eng
    finally:
        eng.dispose()


def test_jsoncolumn_emits_jsonb_and_round_trips(engine):
    _RoundTripProbe.__table__.drop(engine, checkfirst=True)
    _RoundTripProbe.__table__.create(engine)
    try:
        # JSONColumn must reflect as JSONB on Postgres (the whole point of db_types):
        cols = {c["name"]: c for c in inspect(engine).get_columns("pg_roundtrip_probe")}
        assert "JSONB" in str(cols["payload"]["type"]).upper()

        session = sessionmaker(bind=engine)()
        try:
            probe = _RoundTripProbe(payload={"k": "v", "n": 1, "nested": {"a": [1, 2, 3]}})
            session.add(probe)
            session.commit()
            probe_id = probe.id

            fetched = session.get(_RoundTripProbe, probe_id)
            assert isinstance(fetched.id, uuid.UUID)   # Uuid -> native UUID on Postgres
            assert fetched.payload == {"k": "v", "n": 1, "nested": {"a": [1, 2, 3]}}
        finally:
            session.close()
    finally:
        _RoundTripProbe.__table__.drop(engine, checkfirst=True)


@pytest.fixture()
def users_session(engine):
    # Fresh users table per test for isolation on the persistent PG database:
    User.__table__.drop(engine, checkfirst=True)
    User.__table__.create(engine)
    session = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    try:
        yield session
    finally:
        session.close()
        User.__table__.drop(engine, checkfirst=True)


def test_user_round_trips_on_postgres(users_session):
    # Writing the identity via role (is_admin is a read-only derived accessor):
    user = User(name="Owner", email="owner@example.com", is_verified=True,
                role=Role.ADMIN, marketing_consent=False)
    users_session.add(user)
    users_session.commit()

    got = users_session.query(User).filter(User.email == "owner@example.com").one()
    assert got.is_admin and got.is_verified
    assert got.created_at is not None  # TIMESTAMPTZ server default applied by Postgres


def test_admin_seeder_runs_on_postgres(users_session, monkeypatch):
    monkeypatch.setattr(s, "ADMIN_EMAIL", "a@x.io, b@x.io")
    monkeypatch.setattr(s, "PRODUCT_ADMIN_EMAIL", "b@x.io, c@x.io")  # b overlaps platform
    seeder.seed_admin_user_if_configured(users_session)

    # Filtering through the is_admin hybrid expression (role == 'admin'):
    admins = {u.email for u in users_session.query(User).filter(User.is_admin).all()}
    assert admins == {"a@x.io", "b@x.io", "c@x.io"}
    assert users_session.query(User).count() == 3  # de-duped despite the overlap


# ---------------------------------------------------------------------------
# STARTUP-PHASE CONCURRENCY
# ---------------------------------------------------------------------------
# The boot DB tasks (seeders, content sync) are idempotent but not
# concurrency-safe: every gunicorn worker runs them, and on a fresh database
# they all find the same row missing and all insert it. That is only observable
# against a real advisory lock, so it lives here rather than in the SQLite suite.

def test_advisory_lock_serialises_concurrent_workers(engine, monkeypatch):
    """Threads racing the same insert must all succeed when the lock is held.

    Without the lock this is the production failure it was written for: the
    losers of the race die on a unique constraint, so a newly authored course
    fails to sync on the first boot after a deploy.
    """
    import threading
    import time

    from sqlalchemy import text as sql_text

    import infrastructure.database as database

    # The lock and the work must share one engine, and it has to be the Postgres
    # one this module is pointed at rather than the suite's SQLite default:
    monkeypatch.setattr(database, "engine", engine)

    table = f"lock_probe_{uuid.uuid4().hex[:8]}"
    with engine.begin() as conn:
        conn.execute(sql_text(f"CREATE TABLE {table} (slug TEXT PRIMARY KEY)"))

    workers = 4
    barrier = threading.Barrier(workers)
    failures: list[str] = []

    # The shape of every boot task: look for the row, insert it if absent. The
    # sleep holds the read-then-write window open so the interleaving is
    # deterministic rather than a timing coin-flip - without the lock every
    # thread reads an empty table and they all try to insert, which is exactly
    # the production failure. (Verified by bypassing the lock here: the test
    # then fails every run.)
    def upsert() -> None:
        with database.engine.begin() as conn:
            found = conn.execute(sql_text(f"SELECT 1 FROM {table} WHERE slug = 'x'")).fetchone()
            time.sleep(0.25)
            if found is None:
                conn.execute(sql_text(f"INSERT INTO {table} (slug) VALUES ('x')"))

    def run() -> None:
        barrier.wait()
        try:
            database.run_with_advisory_lock("test-startup-race", upsert)
        except Exception as exc:  # noqa: BLE001 - the assertion is the report
            failures.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=run) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)

    try:
        assert not failures, f"advisory lock did not serialise the workers: {failures}"
        with engine.connect() as conn:
            rows = conn.execute(sql_text(f"SELECT count(*) FROM {table}")).scalar()
        assert rows == 1
    finally:
        with engine.begin() as conn:
            conn.execute(sql_text(f"DROP TABLE IF EXISTS {table}"))
