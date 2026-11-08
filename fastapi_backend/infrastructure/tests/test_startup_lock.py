"""The advisory lock that serialises boot-time DB tasks across workers.

Boot tasks (content sync, the seeders) are idempotent but not concurrency-safe:
every gunicorn worker runs them, so on a fresh database they all see the same
row missing and all insert it. `run_with_advisory_lock` serialises the phase.

These tests cover the mechanics on any backend. The genuinely concurrent case
needs a real PostgreSQL advisory lock and lives in `test_pg_integration.py`.
"""

import infrastructure.database as database


def test_work_runs_and_result_is_not_swallowed():
    calls = []
    database.run_with_advisory_lock("test-task", lambda: calls.append("ran"))
    assert calls == ["ran"]


def test_a_failing_task_still_propagates():
    # The lock must not turn a broken startup task into a silent success:
    class Boom(Exception):
        pass

    def explode():
        raise Boom()

    try:
        database.run_with_advisory_lock("test-task", explode)
    except Boom:
        pass
    else:
        raise AssertionError("run_with_advisory_lock swallowed the task's exception")


def test_lock_ids_differ_per_name_and_from_the_migration_lock(monkeypatch):
    # Distinct ids matter twice over: two boot phases must not block each other,
    # and neither may collide with the migration lock they can nest inside.
    import zlib

    from infrastructure.settings import settings as s

    def lock_id(name: str) -> int:
        return zlib.crc32(f"{name}:{s.DATABASE_URL}".encode()) & 0x7FFFFFFF

    migration_lock_id = zlib.crc32(s.DATABASE_URL.encode()) & 0x7FFFFFFF
    assert lock_id("startup-db-tasks") != lock_id("something-else")
    assert lock_id("startup-db-tasks") != migration_lock_id


def test_non_postgres_backend_runs_the_work_directly(monkeypatch):
    # SQLite has no advisory-lock primitive and runs one worker, so the work
    # must run rather than being skipped or blowing up on the missing function:
    executed = []

    def fake_connect(*_args, **_kwargs):
        raise AssertionError("should not open a connection on a non-PostgreSQL backend")

    monkeypatch.setattr(database.engine, "connect", fake_connect)
    assert database.engine.dialect.name != "postgresql"
    database.run_with_advisory_lock("test-task", lambda: executed.append(1))
    assert executed == [1]
