import logging
from collections.abc import Callable

from sqlalchemy.orm import Session

from product.startup import STARTUP_TASKS as PRODUCT_STARTUP_TASKS
from platform.services.inbox_service import validate_inbox_config
from platform.services.storage import validate_storage_config
from platform.services.inbox_service.monitors import start_inbox_monitors
from infrastructure.database import (
    SessionLocal,
    create_and_migrate_with_lock,
    run_with_advisory_lock,
)
from infrastructure.seeder import (
    seed_admin_user_if_configured,
    seed_organisations_and_partnerships,
    seed_service_account_if_configured,
    seed_test_user_if_configured,
)


logger = logging.getLogger(__name__)


def _run_startup_db_task(task: Callable[[Session], None]) -> None:
    task_name = getattr(task, "__name__", task.__class__.__name__)
    try:
        db = SessionLocal()
        try:
            task(db)
        finally:
            db.close()
    except Exception:
        logger.exception("Startup task failed: %s", task_name)


def _run_product_startup_tasks() -> None:
    for task in PRODUCT_STARTUP_TASKS:
        _run_startup_db_task(task)


def run_startup_tasks() -> None:
    create_and_migrate_with_lock()
    # Shouting (ERROR -> Sentry) on a half-configured inbox every boot, same
    # contract as the Stripe gate - readable-but-cannot-reply must never lurk:
    validate_inbox_config()
    # Same contract for the storage plane: a production deployment with no
    # durable backend must announce itself, not discover it via a lost file:
    validate_storage_config()
    # Boot mailbox-auth probe + periodic full-loop canary (daemon threads;
    # every silent mailbox failure mode becomes a loud one):
    start_inbox_monitors()

    # Every worker runs the boot DB tasks, and they are idempotent but not
    # concurrency-safe: on a fresh database they all find the same row missing
    # and all insert it, so the losers die on a unique constraint. One lock
    # around the whole phase makes them serial, which is what they already
    # assume - see run_with_advisory_lock:
    def _db_tasks() -> None:
        _run_startup_db_task(seed_admin_user_if_configured)
        _run_startup_db_task(seed_service_account_if_configured)
        _run_startup_db_task(seed_test_user_if_configured)
        # Public product data (partners and their partnership records),
        # seeded from committed code the same way accounts are:
        _run_startup_db_task(seed_organisations_and_partnerships)
        _run_product_startup_tasks()

    run_with_advisory_lock("startup-db-tasks", _db_tasks)
