"""GetDeploying's open GPU rental price history: a year of weekly medians per model, under CC BY 4.0.

Why this exists: FLOPX's own index (FCPI) started accumulating on 2026-11-07, so until it has a
year behind it the only lawful GPU price history found (N-DATA, 2026-11-08) is the dataset
GetDeploying publishes at https://getdeploying.com/dataset/gpu-prices: weekly rows for 103 GPU
models across 94 providers over the trailing 53 weeks, each row the median of that week's daily
captures of every advertised listing (min, max, median, the median of provider medians, provider
and offering counts), by billing type. It is a THIRD-PARTY aggregate of advertised prices, so it is
stored and served as its own labelled series beside FCPI (`gpu:<model>:getdeploying:weekly-median`,
`:weekly-min`, `:weekly-max`, loaded by `tools/getdeploying_load.py`) and never written into
`index:fcpi:*` or `gpu:<model>:on-demand:*`: a weekly median of other people's list prices is not
an FCPI point, and the pressure engine's momentum rule is unchanged (a design decision banked for
the founder).

Licence (row `getdeploying` in sources/licences.json, use `display-and-archive`, measured, not
asserted): the dataset page, read 2026-11-08, says "Free with attribution, CC BY 4.0: Credit
GetDeploying and link to the license", gives the citation "GetDeploying (2026). GPU rental price
history [dataset]. https://getdeploying.com/dataset/gpu-prices" and adds that the licence covers the
published dataset files only (the site and its API stay under GetDeploying's terms) and that
"prices are estimates for information only, not an offer to sell compute". The CSV is fetched, not
the API; every stored point carries the attribution and the licence link in its note.

Shape: the CSV (`gpu_slug, date, billing_type, reservation_months, min_price, max_price,
median_price, provider_median_price, provider_count, offering_count`; 1.09 MB measured, against the
JSON's 4.12 MB, which sits within 77 KB of `_http`'s 4 MiB body cap, hence the CSV). `date` is the
Monday the week begins (every row measured a Monday). The dataset documents that "the newest week
can change until its Sunday has been captured", so a week's observation time is its END: the
Monday 00:00 UTC that follows (`week_end`), and the loader holds a week back until that instant has
passed rather than store a value the upstream still revises. Only `ON_DEMAND` rows are taken,
matching FLOPX's on-demand model series; spot and reservation rows are ignored.

Model names: GetDeploying keys a model by family (`nvidia-h100` covers SXM and PCIe boards alike,
`nvidia-a100` both 40 GB and 80 GB), so the mapping below is from a family slug to the FLOPX model
id whose board that family is dominated by, and the note on every point says so. A slug not in
`MODEL_BY_SLUG` is ignored (97 of the 103 today); the rule is deliberate: a model FLOPX does not
index has no series to sit beside.

Fetching goes through `_http._through_cache` + `_http._request`, not `fetch_json`: in the
refresher's serving mode `fetch_json` answers from a snapshot and makes no request, and a loader
must fetch. The platform cache (six hours) and the per-host Retry-After block still apply; every
failure is an `unavailable` result with a reason token, never an exception.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from product.flopx.provenance import Provenance, live
from product.flopx.sources import SourceResult, _http, available, licences, unavailable

logger = logging.getLogger(__name__)

SOURCE = "getdeploying"
LICENCE = licences.licence(SOURCE)
DATASET_URL = "https://getdeploying.com/dataset/gpu-prices/weekly.csv"
DATASET_PAGE = "https://getdeploying.com/dataset/gpu-prices"
LICENCE_URL = "https://creativecommons.org/licenses/by/4.0/"
CACHE_TTL_SECONDS = 6 * 3600
BILLING_TYPE = "ON_DEMAND"
UNIT = "USD/GPU-hour"
SERIES_PREFIX = "gpu:"
SERIES_MARK = ":getdeploying:"
MEASURES = ("weekly-median", "weekly-min", "weekly-max")
# The CC BY 4.0 attribution every stored point carries (creator, title, source, licence and link, and what was changed):
ATTRIBUTION = ("GetDeploying (2026). GPU rental price history [dataset], https://getdeploying.com/dataset/gpu-prices, "
               "licensed CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/); six models selected and relabelled with FLOPX model ids")

# GetDeploying family slug -> FLOPX model id. Family slugs pool boards (H100 SXM and PCIe, A100 40 GB and 80 GB);
# the FLOPX id is the board the family's listings are dominated by. Everything else in the dataset is ignored.
MODEL_BY_SLUG: dict[str, str] = {
    "nvidia-h100": "h100-sxm-80gb",
    "nvidia-h200": "h200-sxm-141gb",
    "nvidia-b200": "b200-sxm-192gb",
    "nvidia-a100": "a100-sxm-80gb",
    "nvidia-l40s": "l40s-48gb",
    "nvidia-rtx-4090": "rtx-4090-24gb",
}
SLUG_BY_MODEL = {model: slug for slug, model in MODEL_BY_SLUG.items()}


@dataclass(frozen=True)
class WeeklyPrice:
    """One mapped ON_DEMAND row: a model's week of advertised asks across providers, in USD per GPU-hour."""
    gpu_model_id: str
    gpu_slug: str
    week_start: date
    t: datetime               # the week's end: the following Monday 00:00 UTC
    median: float
    minimum: float
    maximum: float
    provider_count: int
    offering_count: int
    provenance: Provenance


def week_end(week_start: date) -> datetime:
    """The instant a dataset week is complete: 00:00 UTC on the Monday after its `date` (a Monday)."""
    following = week_start + timedelta(days=7)
    return datetime(following.year, following.month, following.day, tzinfo=timezone.utc)


def series_id(gpu_model_id: str, measure: str) -> str:
    if measure not in MEASURES:
        raise ValueError(f"unknown GetDeploying measure {measure!r}")
    return f"{SERIES_PREFIX}{gpu_model_id}{SERIES_MARK}{measure}"


def is_series(series_id_: str) -> bool:
    return series_id_.startswith(SERIES_PREFIX) and SERIES_MARK in series_id_


def note_for(row: WeeklyPrice) -> str:
    return (f"{ATTRIBUTION}. Week beginning Monday {row.week_start.isoformat()}, stamped at its end: the median of that week's "
            f"daily captures of advertised on-demand asks for GetDeploying's {row.gpu_slug} family ({row.provider_count} providers, "
            f"{row.offering_count} offerings), shown as FLOPX {row.gpu_model_id}. A third-party history reference beside FCPI, not an FCPI point.")


def parse_weekly_csv(text: str, fetched_at: datetime) -> list[WeeklyPrice]:
    """The mapped ON_DEMAND rows of a weekly.csv body, oldest first per model; a row with an unmapped slug, another
    billing type, an unreadable date or a missing price is skipped."""
    rows: list[WeeklyPrice] = []
    for raw in csv.DictReader(io.StringIO(text or "")):
        slug = (raw.get("gpu_slug") or "").strip()
        model = MODEL_BY_SLUG.get(slug)
        if model is None or (raw.get("billing_type") or "").strip() != BILLING_TYPE:
            continue
        try:
            start = date.fromisoformat((raw.get("date") or "").strip())
        except ValueError:
            continue
        median, low, high = (_http.number(raw.get(k)) for k in ("median_price", "min_price", "max_price"))
        if median is None or low is None or high is None:
            continue
        t = week_end(start)
        providers = int(_http.number(raw.get("provider_count")) or 0)
        offerings = int(_http.number(raw.get("offering_count")) or 0)
        prov = live(SOURCE, t, fetched_at=fetched_at, licence=LICENCE, source_url=DATASET_PAGE, note=ATTRIBUTION)
        rows.append(WeeklyPrice(gpu_model_id=model, gpu_slug=slug, week_start=start, t=t, median=median, minimum=low, maximum=high,
                                provider_count=providers, offering_count=offerings, provenance=prov))
    rows.sort(key=lambda r: (r.gpu_model_id, r.t))
    return rows


def unmapped_slugs(text: str) -> set[str]:
    """The distinct slugs of a body that the mapping ignores (for the loader's log line)."""
    return {(raw.get("gpu_slug") or "").strip() for raw in csv.DictReader(io.StringIO(text or ""))} - set(MODEL_BY_SLUG)


def fetch_weekly() -> SourceResult:
    """The whole weekly dataset, one request, cached six hours; `records` are `WeeklyPrice` rows; never raises."""
    today = datetime.now(timezone.utc).date()
    key = f"flopx:getdeploying:weekly:{today.isoformat()}"   # a new day asks again: the file is republished daily
    fetched = _http._through_cache(key, CACHE_TTL_SECONDS, lambda: _http._request(
        SOURCE, "GET", DATASET_URL, params=None, headers={"Accept": "text/csv, text/plain;q=0.9, */*;q=0.8"}, json=None, as_json=False))
    if isinstance(fetched, _http.Failure):
        return unavailable(SOURCE, fetched.reason)
    body = fetched.payload if isinstance(fetched.payload, str) else ""
    rows = parse_weekly_csv(body, fetched.fetched_at)
    if not rows:
        return unavailable(SOURCE, "empty-response")
    ignored = unmapped_slugs(body)
    if ignored:
        logger.info("%s: %d weekly rows taken for %d models; %d unmapped models ignored", SOURCE, len(rows), len({r.gpu_model_id for r in rows}), len(ignored))
    prov = live(SOURCE, max(r.t for r in rows), fetched_at=fetched.fetched_at, licence=LICENCE, source_url=DATASET_PAGE, note=ATTRIBUTION)
    return available(SOURCE, rows, prov)
