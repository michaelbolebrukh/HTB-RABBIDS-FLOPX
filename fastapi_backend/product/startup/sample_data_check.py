"""Boot-time validation of the sample datasets (idempotent, read-only)."""

import logging

from sqlalchemy.orm import Session

from product.exceptions import SampleDataInvalidError
from product.flopx.data.loader import load_sample_uncached

logger = logging.getLogger(__name__)


def check_sample_data(db: Session) -> None:  # noqa: ARG001 - signature fixed by the startup seam
    """Loads and cross-validates every sample file; logs ERROR (tracked) on a defect.

    Read-only: nothing is written to the database or the cache. A defect does not
    abort the boot (the startup runner already isolates task failures), it is
    reported as an error event so it is fixed before anyone reads a broken page.
    """
    try:
        data = load_sample_uncached()
    except SampleDataInvalidError as exc:
        logger.error("FLOPX sample data invalid: %s", exc.details)
        return
    logger.info(
        "FLOPX sample data valid: %d offers, %d facilities, %d companies, %d markets, %d feed items, %d series.",
        len(data.offers), len(data.facilities), len(data.companies), len(data.markets), len(data.feed), len(data.series),
    )
