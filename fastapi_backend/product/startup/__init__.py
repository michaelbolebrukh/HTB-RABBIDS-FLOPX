"""FLOPX startup tasks, run by infrastructure/startup.py through the stable export.

Three tasks. Validate the sample datasets at boot so a broken file is a loud log
line at deploy time rather than a 500 on the first request; it writes nothing.
Then load the founder's WRDS extracts into the archive when the deployment
mounts them as secret files (decision G13; no env var names them): the CRSP
closes at /etc/secrets/flopx-crsp.csv and the Compustat quarterly fundamentals
at /etc/secrets/flopx-compustat.csv, each an idempotent upsert that writes
nothing on a repeat boot, fails soft with one warning line when its file is
unreadable, and logs one line saying nothing was loaded when the file is absent.
They run under the startup advisory lock like every boot DB task, so gunicorn
workers do not race the first load. The same task also loads GetDeploying's weekly
GPU rental price history (CC BY 4.0, one keyless request, idempotent) as its own
labelled series beside FCPI.
Then start this process's live-source refresher (product.flopx.refresher): the
task runs in every gunicorn worker, so each worker warms and serves its own
snapshot, and no request waits on an upstream. It returns at once (a daemon
thread does the work) and refuses to start under pytest.
Then schedule the power-price history backfill (product.flopx.tools.history_backfill):
a daemon thread that waits for the refresher's warm-up, takes its own cross-worker
advisory lock and fills whatever of the last two years of Energy-Charts DE-LU and
Elexon GB slots the archive lacks, bounded per run and resumed on the next boot;
it also refuses to start under pytest.
The question-flows import is the factory's registration seam; FLOPX registers
no flow (see product/question_flows.py), the import stays so the seam is
visibly exercised rather than silently dropped.
"""

from collections.abc import Callable

from sqlalchemy.orm import Session

import product.question_flows  # noqa: F401
from product.flopx import refresher
import logging

from product.flopx.tools import alpaca_load, compustat_load, getdeploying_load, history_backfill, wrds_load
from product.startup.sample_data_check import check_sample_data


logger = logging.getLogger(__name__)


def load_research_archive(db: Session) -> None:
    """The WRDS CRSP closes and the Compustat quarterly fundamentals, when the deployment mounts the extracts;
    before the refresher starts, so its first snapshots of the derived reads already see the real data."""
    from product.flopx.refresher import rss_mb
    logger.info("flopx mem: before research loaders rss %.0f MB", rss_mb())
    wrds_load.load_secret_file_if_present(db)
    compustat_load.load_secret_file_if_present(db)
    logger.info("flopx mem: after WRDS loaders rss %.0f MB", rss_mb())
    alpaca_load.load_at_startup(db)   # the 2026 closes that continue the CRSP series (eight requests at most, idempotent)
    getdeploying_load.load_at_startup(db)   # a year of weekly GPU rental medians beside FCPI (one request, idempotent, CC BY 4.0)
    logger.info("flopx mem: after Alpaca and GetDeploying loaders rss %.0f MB", rss_mb())


def start_refresher(_db: Session) -> None:
    # Returns at once: the boot warm-up runs on the refresher's own thread, so a worker that
    # gunicorn recycles (max_requests) accepts traffic again without waiting for it.
    refresher.start()


def start_history_backfill(_db: Session) -> None:
    # Returns at once as well: the thread sleeps past the warm-up, then loads under its own lock.
    history_backfill.start_background()


STARTUP_TASKS: list[Callable[[Session], None]] = [
    check_sample_data,
    load_research_archive,
    start_refresher,
    start_history_backfill,
]
