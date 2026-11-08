"""The organisations migration: two new tables, and `users` left alone.

Migrations are otherwise only exercised at deploy time (the suite builds schemas
with create_all), so this migration gets a real upgrade/downgrade round-trip
here. The property under test is that it is PURELY ADDITIVE: because conferral
is computed rather than materialised, no existing column changes meaning, so
there is no backfill and `users` must come out byte-for-byte the same shape it
went in — including everyone's existing grants.

The historical chain is raw PostgreSQL SQL and is never replayed on SQLite, so
the fixture stamps the parent revision and creates just the `users` shape,
leaving exactly one step to run. That step is named explicitly (TARGET_REVISION
rather than the chain tip): this file asserts about ONE migration, so later
migrations landing on the chain must never silently become part of what it runs.
"""

import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


BACKEND_DIR = Path(__file__).resolve().parents[2]
PARENT_REVISION = "20260724_02"
# The revision under test — the organisations migration itself:
TARGET_REVISION = "20260724_03"

_LEGACY_USERS = """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        name VARCHAR NOT NULL,
        email VARCHAR NOT NULL UNIQUE,
        role VARCHAR NOT NULL DEFAULT 'user',
        capabilities JSON,
        is_verified BOOLEAN NOT NULL DEFAULT 0
    )
"""


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    """A SQLite database at the parent revision, holding hand-granted capabilities."""
    url = f"sqlite:///{tmp_path / 'orgs.db'}"
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(_LEGACY_USERS))
        conn.execute(text("CREATE INDEX ix_users_email ON users (email)"))
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text("INSERT INTO alembic_version VALUES (:rev)"), {"rev": PARENT_REVISION})
        conn.execute(text(
            "INSERT INTO users (id, name, email, capabilities) VALUES"
            " (1, 'Sarah', 's@x.com', '[\"feature:preview\", \"feature:early-access\"]'),"
            " (2, 'Plain', 'p@x.com', NULL),"
            " (3, 'Ops', 'o@x.com', '[\"inbox:read\"]')"
        ))
    engine.dispose()

    # env.py resolves the target database from DATABASE_URL:
    monkeypatch.setenv("DATABASE_URL", url)
    return url


def _alembic_config() -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    return config


def _as_list(value):
    # SQLite hands JSON back as text; PostgreSQL would already have decoded it:
    if value is None or isinstance(value, list):
        return value
    return json.loads(value)


def _users_columns(engine) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns("users")}


def test_upgrade_creates_both_tables_with_their_indexes(legacy_db):
    command.upgrade(_alembic_config(), TARGET_REVISION)

    engine = create_engine(legacy_db)
    inspector = inspect(engine)
    assert {"organisations", "organisation_members"} <= set(inspector.get_table_names())

    member_indexes = {index["name"] for index in inspector.get_indexes("organisation_members")}
    assert "ix_organisation_members_organisation_id" in member_indexes
    assert "ix_organisation_members_user_id" in member_indexes

    # One membership per (organisation, user) — what makes add_member idempotent:
    unique = {tuple(u["column_names"]) for u in inspector.get_unique_constraints("organisation_members")}
    assert ("organisation_id", "user_id") in unique

    slug_index = next(i for i in inspector.get_indexes("organisations") if i["name"] == "ix_organisations_slug")
    assert slug_index["unique"]
    engine.dispose()


def test_upgrade_leaves_the_users_table_completely_untouched(legacy_db):
    engine = create_engine(legacy_db)
    before_columns = _users_columns(engine)
    engine.dispose()

    command.upgrade(_alembic_config(), TARGET_REVISION)

    engine = create_engine(legacy_db)
    # No column added, none removed — the migration is purely additive, because
    # conferred grants are computed and never written onto an account:
    assert _users_columns(engine) == before_columns
    assert "independent_capabilities" not in _users_columns(engine)

    with engine.begin() as conn:
        rows = conn.execute(text("SELECT id, capabilities FROM users ORDER BY id")).all()
    engine.dispose()

    by_id = {row[0]: _as_list(row[1]) for row in rows}
    # Every account keeps exactly the grants it held; nothing is rewritten:
    assert by_id[1] == ["feature:preview", "feature:early-access"]
    assert by_id[2] is None
    assert by_id[3] == ["inbox:read"]


def test_upgrade_is_replayable(legacy_db):
    config = _alembic_config()
    command.upgrade(config, TARGET_REVISION)

    # Replaying over an already-migrated database is a no-op, not an error:
    command.stamp(config, PARENT_REVISION)
    command.upgrade(config, TARGET_REVISION)

    engine = create_engine(legacy_db)
    assert {"organisations", "organisation_members"} <= set(inspect(engine).get_table_names())
    engine.dispose()


def test_downgrade_drops_both_tables_leaving_grants_intact(legacy_db):
    config = _alembic_config()
    command.upgrade(config, TARGET_REVISION)
    command.downgrade(config, PARENT_REVISION)

    engine = create_engine(legacy_db)
    inspector = inspect(engine)
    assert "organisations" not in inspector.get_table_names()
    assert "organisation_members" not in inspector.get_table_names()

    with engine.begin() as conn:
        rows = conn.execute(text("SELECT id, capabilities FROM users ORDER BY id")).all()
    # `users` was never touched on the way up, so nothing can be stranded on the
    # way down — every account keeps the grants it holds directly:
    assert _as_list(rows[0][1]) == ["feature:preview", "feature:early-access"]
    assert _as_list(rows[2][1]) == ["inbox:read"]
    assert "ix_users_email" in {index["name"] for index in inspector.get_indexes("users")}
    engine.dispose()


def test_downgrade_then_upgrade_round_trips(legacy_db):
    config = _alembic_config()
    command.upgrade(config, TARGET_REVISION)
    command.downgrade(config, PARENT_REVISION)
    command.upgrade(config, TARGET_REVISION)

    engine = create_engine(legacy_db)
    assert {"organisations", "organisation_members"} <= set(inspect(engine).get_table_names())
    engine.dispose()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
