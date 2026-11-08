"""Alpaca paper broker: dormant behind the optional PRODUCT_ALPACA_* settings.

Brief F6: the built-in simulated broker is the default; Alpaca is an option, not
a mandate. The settings are read for truthiness only and never logged. Until an
operator proves the key works, every call returns `unavailable` (`not-configured`
or `not-implemented`), so no credential is ever sent from this build.
"""

from __future__ import annotations

from infrastructure.settings import settings as s
from product.flopx.sources import SourceResult, unavailable

SOURCE = "alpaca"


def configured() -> bool:
    """True only when every Alpaca setting is present; the values are never read beyond truthiness."""
    return bool(s.PRODUCT_ALPACA_API_KEY and s.PRODUCT_ALPACA_SECRET_KEY and s.PRODUCT_ALPACA_BASE_URL)


def fetch_account() -> SourceResult:
    if not configured():
        return unavailable(SOURCE, "not-configured")
    return unavailable(SOURCE, "not-implemented")
