"""Migration 20261107_02 adds the decision-ledger columns, idempotently and reversibly.

The suite otherwise builds schema with `create_all`, so this migration gets a
real upgrade/downgrade round-trip on a SQLite database stamped at its parent,
holding the parent's shape of `flopx_strategy_decisions` with one row. The
properties: the columns and indexes appear, the existing row survives with
status `proposed` (the server default), a second upgrade is a no-op, and the
downgrade removes exactly what was added.
"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

BACKEND_DIR = Path(__file__).resolve().parents[3]
PARENT_REVISION = "20261107_01"
TARGET_REVISION = "20261107_02"
ADDED = {"status", "signal_id", "signal_kind", "order_ids", "skipped_reason", "horizon_days", "proposed_weight"}

_PARENT_SHAPE = """
    CREATE TABLE flopx_strategy_decisions (
        id INTEGER PRIMARY KEY,
        strategy_id VARCHAR(64) NOT NULL,
        account_id INTEGER,
        t DATETIME NOT NULL,
        action VARCHAR(4) NOT NULL,
        ticker VARCHAR(16),
        rationale JSON NOT NULL,
        known_data_snapshot JSON NOT NULL,
        mode VARCHAR(8) NOT NULL,
        acted BOOLEAN NOT NULL DEFAULT 0,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
"""


@pytest.fixture
def stamped_db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'ledger.db'}"
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(_PARENT_SHAPE))
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text("INSERT INTO alembic_version VALUES (:rev)"), {"rev": PARENT_REVISION})
        conn.execute(text("INSERT INTO flopx_strategy_decisions (id, strategy_id, t, action, ticker, rationale, known_data_snapshot, mode, acted)"
                          " VALUES (1, 'divergence', '2026-11-01 21:00:00', 'buy', 'CRWV', '{}', '{}', 'forward', 1)"))
    engine.dispose()
    monkeypatch.setenv("DATABASE_URL", url)
    return url


def _config() -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    return config


def _columns(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_columns("flopx_strategy_decisions")}
    finally:
        engine.dispose()


def test_upgrade_adds_the_columns_keeps_the_row_and_is_replayable(stamped_db):
    before = _columns(stamped_db)
    assert not (ADDED & before)
    command.upgrade(_config(), TARGET_REVISION)
    after = _columns(stamped_db)
    assert ADDED <= after and before <= after
    engine = create_engine(stamped_db)
    with engine.begin() as conn:
        row = conn.execute(text("SELECT status, signal_id, order_ids, acted FROM flopx_strategy_decisions WHERE id = 1")).one()
        indexes = {i["name"] for i in inspect(conn).get_indexes("flopx_strategy_decisions")}
        conn.execute(text("UPDATE alembic_version SET version_num = :rev"), {"rev": PARENT_REVISION})
    engine.dispose()
    assert row[0] == "proposed" and row[1] is None and row[2] is None and row[3] == 1
    assert {"ix_flopx_strategy_decisions_status", "ix_flopx_strategy_decisions_signal_id"} <= indexes
    command.upgrade(_config(), TARGET_REVISION)       # replayed against a database that already has the columns
    assert _columns(stamped_db) == after


def test_downgrade_removes_exactly_what_was_added(stamped_db):
    command.upgrade(_config(), TARGET_REVISION)
    command.downgrade(_config(), PARENT_REVISION)
    assert not (ADDED & _columns(stamped_db))
    engine = create_engine(stamped_db)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM flopx_strategy_decisions")).scalar() == 1
    engine.dispose()
