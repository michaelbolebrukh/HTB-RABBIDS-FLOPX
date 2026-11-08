"""Small, real validators shared by schemas and services."""

import re

# Series ids are composable, colon-separated, lowercase tokens:
#   gpu:h100-sxm-80gb:on-demand:us | index:fcpi:h100-sxm-80gb | equity:NVDA:close
_SERIES_ID = re.compile(r"^[a-z][a-z0-9-]*(:[A-Za-z0-9^.-]+){1,4}$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_TICKER = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")


def is_series_id(value: str) -> bool:
    return bool(_SERIES_ID.match(value or ""))


def is_slug(value: str) -> bool:
    return bool(_SLUG.match(value or ""))


def normalise_ticker(value: str) -> str:
    """Upper-cases and strips a ticker; raises ValueError when it is not ticker-shaped."""
    candidate = (value or "").strip().upper()
    if not _TICKER.match(candidate):
        raise ValueError(f"not a ticker: {value!r}")
    return candidate


def parse_range(value: str | None, default_days: int = 180) -> int:
    """Turns a range token (`30d`, `6m`, `1y`, `ytd`-less) into a number of days."""
    if not value:
        return default_days
    token = value.strip().lower()
    match = re.match(r"^(\d+)([dwmy])$", token)
    if not match:
        raise ValueError(f"not a range: {value!r}")
    n, unit = int(match.group(1)), match.group(2)
    return n * {"d": 1, "w": 7, "m": 30, "y": 365}[unit]
