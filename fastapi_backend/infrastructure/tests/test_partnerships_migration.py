"""The partnerships migration: one new table, and everything else left alone.

Migrations are otherwise only exercised at deploy time (the suite builds schemas
with create_all), so this one gets a real upgrade/downgrade round-trip. The
property under test is that it is PURELY ADDITIVE — a partnership confers
nothing, so `organisations` keeps its grants and `users` is not touched at all,
and a downgrade can strand nothing.

The historical chain is raw PostgreSQL SQL and is never replayed on SQLite, so
the fixture stamps the parent revision and creates just the shapes the new table
references, leaving exactly one step to run. That step is named explicitly
(TARGET_REVISION rather than the chain tip): this file asserts about ONE
migration, so later migrations must never silently become part of what it runs.
"""

import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


BACKEND_DIR = Path(__file__).resolve().parents[2]
# Parent is the single tip this migration revises — not an older mid-chain
# revision. Stamping anything else would hide a multi-head graph.
PARENT_REVISION = "20260804_01"
# The revision under test — the partnerships migration itself:
TARGET_REVISION = "20260806_01"

_LEGACY_ORGANISATIONS = """
    CREATE TABLE organisations (
        id INTEGER PRIMARY KEY,
        slug VARCHAR NOT NULL UNIQUE,
        name VARCHAR NOT NULL,
        is_active BOOLEAN NOT NULL DEFAULT 1,
        granted_capabilities JSON
    )
"""


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    """A SQLite database at the parent revision, holding a conferring organisation."""
    url = f"sqlite:///{tmp_path / 'partnerships.db'}"
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(_LEGACY_ORGANISATIONS))
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text("INSERT INTO alembic_version VALUES (:rev)"), {"rev": PARENT_REVISION})
        conn.execute(text(
            "INSERT INTO organisations (id, slug, name, granted_capabilities) VALUES"
            " (1, 'hudson-river-trading', 'Hudson River Trading',"
            " '[\"feature:preview\", \"feature:early-access\"]')"
        ))
    engine.dispose()

    # env.py resolves the target database from DATABASE_URL:
    monkeypatch.setenv("DATABASE_URL", url)
    return url


def _alembic_config() -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    return config


def test_upgrade_creates_the_table_with_its_columns_and_indexes(legacy_db):
    command.upgrade(_alembic_config(), TARGET_REVISION)

    engine = create_engine(legacy_db)
    inspector = inspect(engine)
    assert "partnerships" in inspector.get_table_names()

    columns = {column["name"]: column for column in inspector.get_columns("partnerships")}
    assert set(columns) == {
        "id", "organisation_id", "status", "season", "offering_label", "snapshot_key",
        "starts_on", "ends_on", "signed_on", "created_at", "updated_at",
    }
    # A plain string status with a draft default, never a database enum — the
    # workflow can change without a migration:
    assert "VARCHAR" in str(columns["status"]["type"]).upper()
    assert not columns["status"]["nullable"]
    assert not columns["season"]["nullable"]
    # Contractual days, not instants:
    assert "DATE" in str(columns["ends_on"]["type"]).upper()

    indexes = {index["name"] for index in inspector.get_indexes("partnerships")}
    assert {"ix_partnerships_organisation_id", "ix_partnerships_status",
            "ix_partnerships_season"} <= indexes

    # Renewals are new rows, and one season may hold more than one agreement —
    # so (organisation, season) is deliberately NOT unique:
    unique = {tuple(u["column_names"]) for u in inspector.get_unique_constraints("partnerships")}
    assert ("organisation_id", "season") not in unique
    engine.dispose()


def test_upgrade_leaves_organisations_and_their_grants_untouched(legacy_db):
    engine = create_engine(legacy_db)
    before = {column["name"] for column in inspect(engine).get_columns("organisations")}
    engine.dispose()

    command.upgrade(_alembic_config(), TARGET_REVISION)

    engine = create_engine(legacy_db)
    assert {column["name"] for column in inspect(engine).get_columns("organisations")} == before
    with engine.begin() as conn:
        grants = conn.execute(text("SELECT granted_capabilities FROM organisations")).scalar()
    engine.dispose()

    # A partnership confers nothing, so nothing about conferral changes:
    assert json.loads(grants) == ["feature:preview", "feature:early-access"]


def test_upgrade_is_replayable(legacy_db):
    config = _alembic_config()
    command.upgrade(config, TARGET_REVISION)

    # Replaying over an already-migrated database is a no-op, not an error:
    command.stamp(config, PARENT_REVISION)
    command.upgrade(config, TARGET_REVISION)

    engine = create_engine(legacy_db)
    assert "partnerships" in inspect(engine).get_table_names()
    engine.dispose()


def test_downgrade_drops_the_table_leaving_organisations_intact(legacy_db):
    config = _alembic_config()
    command.upgrade(config, TARGET_REVISION)
    command.downgrade(config, PARENT_REVISION)

    engine = create_engine(legacy_db)
    inspector = inspect(engine)
    assert "partnerships" not in inspector.get_table_names()
    assert "organisations" in inspector.get_table_names()
    engine.dispose()


def test_downgrade_then_upgrade_round_trips(legacy_db):
    config = _alembic_config()
    command.upgrade(config, TARGET_REVISION)
    command.downgrade(config, PARENT_REVISION)
    command.upgrade(config, TARGET_REVISION)

    engine = create_engine(legacy_db)
    assert "partnerships" in inspect(engine).get_table_names()
    engine.dispose()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
