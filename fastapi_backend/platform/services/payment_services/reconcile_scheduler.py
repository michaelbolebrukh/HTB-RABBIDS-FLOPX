"""Periodic entitlement reconciliation loop.

Webhooks are the primary, real-time freshness mechanism for `users.entitlements`.
This is the belt-and-suspenders failsafe: a low-frequency background sweep that
re-reads active entitlements from the provider and heals any drift a missed
webhook could leave (notably a downgrade the empty-cache on-read reconcile can't
catch). It is fully fail-open - a sweep failure logs and the loop continues - and
provider-gated: nothing runs unless payments are configured.

Single-execution across workers/instances: each tick takes a non-blocking
PostgreSQL advisory lock, so only one worker actually sweeps; the rest skip. On
non-PostgreSQL backends (the single-worker gateway/SQLite case) there is no
concurrency to guard, so it runs directly.
"""
import asyncio
import logging
import zlib

from sqlalchemy import text

from infrastructure.database import SessionLocal, engine
from infrastructure.settings import settings as s
from platform.services.payment_services.entitlement_service import reconcile_all_entitlements
from platform.services.payment_services.stripe_gateway import stripe_is_configured


logger = logging.getLogger(__name__)

# Distinct from the migration advisory-lock id so the two never collide:
_LOCK_ID = zlib.crc32(b"entitlement_reconcile_sweep") & 0x7FFFFFFF


def _run_one_sweep() -> None:
    """Runs a single reconcile sweep under an advisory lock (sync; DB + provider)."""
    if not stripe_is_configured():
        return

    is_postgres = engine.dialect.name == "postgresql"
    with SessionLocal() as db:
        if is_postgres:
            acquired = db.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"), {"lock_id": _LOCK_ID}
            ).scalar()
            if not acquired:
                # Another worker/instance is sweeping this tick:
                return
        try:
            scanned, changed = reconcile_all_entitlements(db)
            if scanned:
                logger.info(
                    "Entitlement reconcile swept %d customer(s), %d changed.", scanned, changed
                )
        finally:
            if is_postgres:
                db.execute(text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": _LOCK_ID})
                db.commit()


async def _reconcile_loop(interval_seconds: int) -> None:
    while True:
        # Sleeping first so boot is never delayed and workers desynchronise a little:
        await asyncio.sleep(interval_seconds)
        try:
            # Off the event loop - the sweep does blocking DB + provider I/O:
            await asyncio.to_thread(_run_one_sweep)
        except Exception:
            logger.exception("Entitlement reconcile sweep failed; continuing.")


def start_entitlement_reconcile_loop() -> asyncio.Task | None:
    """Starts the reconcile loop as a background task, or returns None if disabled.

    Called from the app lifespan (inside the running event loop). Cancel the
    returned task on shutdown.
    """
    interval = s.ENTITLEMENT_RECONCILE_INTERVAL_SECONDS
    if interval <= 0:
        return None
    logger.info("Starting entitlement reconcile loop (every %ds).", interval)
    return asyncio.create_task(_reconcile_loop(interval))
