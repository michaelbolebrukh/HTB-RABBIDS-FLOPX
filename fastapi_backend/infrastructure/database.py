import logging
import zlib
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base

from infrastructure.settings import settings as s
from infrastructure.cache import install_cache_invalidation


# Declaring the engine to connect with the DB:
engine = create_engine(s.DATABASE_URL, pool_pre_ping=True)


@event.listens_for(Engine, "connect")
def _enforce_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    """Turning on SQLite's foreign-key enforcement, which is OFF by default.

    Relationships declare `ondelete="CASCADE"` with `passive_deletes=True`, which
    deliberately leaves the delete to the database. PostgreSQL honours that;
    SQLite silently ignores it unless this pragma is set, so child rows would
    survive their parent. That is not merely stale data now that organisation
    membership CONFERS capabilities: SQLite also recycles row ids, so an orphaned
    membership could hand a departed member's grants to whoever next takes that
    id. Bound to the Engine class rather than one engine so every SQLite
    connection in the process behaves like production, tests included:
    """
    if type(dbapi_connection).__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

# sessionmaker class is used to create session objects to connect & interact with the DB:
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Wiring automatic cache invalidation (read-tracking + committed-write tag bumps):
install_cache_invalidation(SessionLocal)

# declarative_base function is used to create a base class for all data models:
Base = declarative_base()


def create_and_migrate() -> None:
    """Creates database tables and runs Alembic migrations to upgrade the database to the latest schema"""
    try:
        # Importing models to register them with Base.metadata:
        import platform.models  # noqa: F401
        import product.models  # noqa: F401
        
        # Creating tables first (idempotent - safe for existing tables):
        logging.getLogger(__name__).info("Creating database tables if they don't exist...")
        Base.metadata.create_all(bind=engine)
        
        # Importing Alembic lazily to keep import graph light for app startup:
        from alembic import command  # type: ignore
        from alembic.config import Config  # type: ignore
        from alembic.script import ScriptDirectory  # type: ignore

        base_dir = Path(__file__).resolve().parents[1]
        alembic_ini = base_dir / "alembic.ini"
        cfg = Config(str(alembic_ini))

        # Ensuring Alembic resolves the scripts directory correctly regardless of CWD:
        cfg.set_main_option("script_location", str(base_dir / "migrations"))

        # Clearing orphaned alembic_version rows (revisions present in DB but missing from
        # local migration scripts). This can happen if a migration file was renamed or removed
        # before any production data existed. Without this, alembic raises
        # "Can't locate revision identified by 'X'" and refuses to upgrade.
        script_dir = ScriptDirectory.from_config(cfg)
        known_revisions = {script.revision for script in script_dir.walk_revisions()}
        # Using SQLAlchemy's inspector for a dialect-agnostic existence check
        # (works on PostgreSQL, SQLite, MySQL, etc. without backend-specific SQL):
        if inspect(engine).has_table("alembic_version"):
            with engine.begin() as conn:
                current_rows = conn.execute(text("SELECT version_num FROM alembic_version")).fetchall()
                for (current_rev,) in current_rows:
                    if current_rev not in known_revisions:
                        logging.getLogger(__name__).warning(
                            f"Clearing orphaned alembic revision '{current_rev}' (no longer exists in migration scripts)."
                        )
                        conn.execute(
                            text("DELETE FROM alembic_version WHERE version_num = :rev"),
                            {"rev": current_rev},
                        )

        logging.getLogger(__name__).info("Running database migrations...")
        command.upgrade(cfg, "head")
        logging.getLogger(__name__).info("Migrations complete.")
        
    except Exception:
        logging.getLogger(__name__).exception("Database migration failed:")
        raise


def create_and_migrate_with_lock() -> None:
    """
    Creates database tables and runs migrations with cross-worker coordination.

    On PostgreSQL, a session-level advisory lock ensures only one worker runs the
    migrations when multiple workers boot concurrently; the others block until it
    completes. Other backends (e.g. SQLite) provide no advisory-lock primitive and
    run a single worker, so there is no concurrent-migration race to guard against -
    in that case migrations run directly.
    """
    logger = logging.getLogger(__name__)

    # Advisory locks are PostgreSQL-specific; on any other backend, migrate directly:
    if engine.dialect.name != "postgresql":
        logger.info(
            "Non-PostgreSQL backend (%s); running migrations without advisory lock.",
            engine.dialect.name,
        )
        create_and_migrate()
        return

    # Generating unique lock ID using CRC32 (industry standard pattern used by Django, etc.):
    lock_id = zlib.crc32(s.DATABASE_URL.encode()) & 0x7FFFFFFF

    with engine.connect() as conn:
        try:
            # Attempting to acquire advisory lock (blocks until lock is available):
            logger.info(f"Attempting to acquire migration lock (ID: {lock_id})...")
            result = conn.execute(text("SELECT pg_advisory_lock(:lock_id)"), {"lock_id": lock_id})
            result.fetchone()
            logger.info("Migration lock acquired. Running migrations...")
            
            # Running migrations while holding the lock:
            create_and_migrate()
            
            logger.info("Migrations complete. Releasing lock...")
            
        finally:
            # Releasing the advisory lock:
            conn.execute(text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": lock_id})
            conn.commit()
            logger.info("Migration lock released.")


def run_with_advisory_lock(name: str, work: Callable[[], None]) -> None:
    """Running `work` while holding a named cross-worker advisory lock.

    Boot-time DB tasks - content sync, the seeders - are written to be
    idempotent, which makes them safe to REPEAT but not safe to run
    CONCURRENTLY. Every gunicorn worker runs them on startup, so on a fresh
    database they all read "this row is missing" at the same moment and all try
    to insert it; the losers die on a unique constraint. That surfaced as a
    failed content sync on the first boot after any new track or course was
    authored, self-healing only on the next deploy.

    Serialising them fixes it without making any task cleverer: the first worker
    through does the inserts, and the rest then find the rows already there and
    take their update path. Same primitive and same reasoning as the migration
    lock above, with its own lock id so the two can nest.

    Non-PostgreSQL backends have no advisory-lock primitive and run a single
    worker, so there is no race to guard against and the work runs directly.
    """
    logger = logging.getLogger(__name__)

    if engine.dialect.name != "postgresql":
        work()
        return

    lock_id = zlib.crc32(f"{name}:{s.DATABASE_URL}".encode()) & 0x7FFFFFFF
    with engine.connect() as conn:
        try:
            logger.info("Waiting for the %s lock (id %s)...", name, lock_id)
            conn.execute(text("SELECT pg_advisory_lock(:lock_id)"), {"lock_id": lock_id}).fetchone()
            logger.info("Acquired the %s lock.", name)
            work()
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": lock_id})
            conn.commit()
            logger.info("Released the %s lock.", name)
