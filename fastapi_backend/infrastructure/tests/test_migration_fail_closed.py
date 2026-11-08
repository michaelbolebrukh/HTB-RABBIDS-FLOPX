"""
Migrations must fail closed on boot.

`create_and_migrate()` runs at startup (`run_startup_tasks()` → `main.py` app
lifespan). If a migration fails, the exception must propagate — crashing boot so
Render's health gate keeps the old instance serving and marks the deploy failed —
never be swallowed into a running instance on a broken schema. These tests pin
that contract so a future refactor cannot silently reintroduce a swallow.
"""

import pytest

from infrastructure import database


def _raise_migration_failure(*_args, **_kwargs):
    raise RuntimeError("simulated migration failure")


def test_create_and_migrate_propagates_failure(monkeypatch):
    # Alembic's upgrade is imported lazily inside the function; patching the
    # module attribute reaches the same object the function calls:
    from alembic import command

    monkeypatch.setattr(command, "upgrade", _raise_migration_failure)

    with pytest.raises(RuntimeError, match="simulated migration failure"):
        database.create_and_migrate()


def test_lock_wrapper_propagates_failure(monkeypatch):
    # On non-PostgreSQL (SQLite here) the wrapper migrates directly; the failure
    # must still propagate through it rather than being swallowed:
    from alembic import command

    monkeypatch.setattr(command, "upgrade", _raise_migration_failure)

    with pytest.raises(RuntimeError, match="simulated migration failure"):
        database.create_and_migrate_with_lock()
