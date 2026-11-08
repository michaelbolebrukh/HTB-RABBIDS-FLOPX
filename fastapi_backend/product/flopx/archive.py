"""The archive: live observations appended to `flopx_observations` so history accumulates.

Every successful live fetch produces numeric observations (an offer's ask, a
model's live index level, a market's Yes price, a power price, an equity close,
a fundamental) keyed by a series id that embeds the source and the entity
(`offer:vast-55934598:price`, `market:kalshi:KXUSDCCAPACITY-27MAR05-T65.0:yes`),
so idempotency on (source, entity, observedAt) is the table's own unique
constraint on (series_id, t). A value that has not changed is re-archived at
most once an hour, which keeps a 5-minute poll of static asks from writing a
row per poll while still recording every move.

Reads come back labelled `archived` (the stored envelope is the original `live`
one with its kind rewritten), which is how a series can extend its sample or
live points with what earlier fetches saw. Every write path is wrapped by the
caller in its own try/except: an archive failure must never fail a read.

Only a source whose licence row (sources/licences.json) says `display-and-archive`
is ever stored: `record_results` drops every other result before writing, so a
display-only source (Yahoo Finance) and a `display-not-index` source (Vast.ai, a
current market reading under founder decision G9-b) are shown live and never kept.
`record` applies the same refusal row by row, so a Vast ask handed to it directly
is not written either.

The same table also governs reads (R1-5): rows stored while a source was allowed
stay in the table (nothing is deleted), but a row whose source is now `off`,
display-only or display-not-index never leaves this module. Every read here applies `refusal()`, which
also refuses:

- a point dated after it was fetched, or after now (R1-12): a day-ahead slot
  archived before its delivery time was never an observation of that time, and
  the live adapters already drop such slots;
- an FCPI index point that does not record the sources behind it (its basket):
  points archived before the basket was recorded cannot be shown to exclude a
  source that is now off, so they are withheld rather than republished;
- an FCPI index point whose basket holds a source that may never be in an index
  (`display-not-index`, Vast.ai): such a point is neither written nor served.

Index points are written with their basket (`record(..., baskets=...)`), which is
what lets `terminal._index_for` mark a basket change as a series break.

A `research-archive` source (the founder's WRDS CRSP closes, decision G13) is
stored like any other and read by every engine, but its raw points are not
public: every read here takes `public_only`, and a caller serving an anonymous
reader passes True so those rows stay in the table and out of the payload.
Such rows arrive through `upsert` (a bulk, idempotent load keyed on
(series_id, t) that rewrites a row only when its value changed) and leave
through `delete_source`, the one path that deletes anything from this table.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from infrastructure.cache import cache
from product.flopx import domain
from product.flopx.provenance import Provenance, utcnow
from product.flopx.sources import SourceResult, licences
from product.models import Observation

logger = logging.getLogger(__name__)

UNCHANGED_INTERVAL = timedelta(hours=1)
MARKER_TTL_SECONDS = 300
# Clock skew allowed between an upstream timestamp and this host before a point counts as future-dated.
FUTURE_TOLERANCE = timedelta(minutes=2)
INDEX_PREFIX = "index:"
BASKET_KEY = "input_sources"   # the stored provenance key that carries an index point's basket


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _parse(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _aware(value)
    text = str(value)
    return _aware(datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text))


def refusal(series_id: str, t: datetime, provenance: dict, *, now: datetime | None = None) -> str | None:
    """Why an archived (or about-to-be-archived) row must not be served; None when it may be.

    `source-off`: its source is `off`, display-only or display-not-index under the licence table today (or has no row).
    `future-dated`: dated after the fetch that produced it, or after now.
    `basket-unrecorded` / `basket-not-indexable` / `basket-source-off`: an index point with no recorded
    basket, one whose basket holds a `display-not-index` source (Vast.ai), or one built from a source
    that is now off. Other derived rows (first detections) carry no source.
    """
    now = now or utcnow()
    t = _aware(t)
    if t > now + FUTURE_TOLERANCE:
        return "future-dated"
    fetched = _parse(provenance.get("fetched_at") or provenance.get("fetchedAt"))
    if fetched is not None and t > fetched + FUTURE_TOLERANCE:
        return "future-dated"
    if provenance.get("kind") == "derived" or series_id.startswith(INDEX_PREFIX):
        if not series_id.startswith(INDEX_PREFIX):
            return None
        basket = provenance.get(BASKET_KEY)
        if not basket:
            return "basket-unrecorded"
        if any(not licences.in_index(s) for s in basket):
            return "basket-not-indexable"
        return "basket-source-off" if any(not licences.archivable(s) for s in basket) else None
    return None if licences.archivable(str(provenance.get("source", ""))) else "source-off"


def is_public(prov: Provenance | dict) -> bool:
    """Whether a stored point may reach an anonymous reader (its source's licence row is not `research-archive`)."""
    source = prov.source if isinstance(prov, Provenance) else str(prov.get("source", ""))
    return licences.public(source)


def basket_of(row_provenance: dict) -> tuple[str, ...] | None:
    basket = row_provenance.get(BASKET_KEY)
    return tuple(basket) if basket else None


# -- observations from live records -------------------------------------------------

def observations_from_offers(offers: Iterable[domain.GpuOffer]) -> list[domain.PriceObservation]:
    out = []
    for o in offers:
        out.append(domain.PriceObservation(series_id=f"offer:{o.id}:price", t=o.provenance.observed_at, value=o.price_per_gpu_hour_usd, unit="USD/GPU-hour", provenance=o.provenance))
        out.append(domain.PriceObservation(series_id=f"offer:{o.id}:available", t=o.provenance.observed_at,
                                           value={"available": 1.0, "limited": 0.5, "unavailable": 0.0}.get(o.availability, 0.0), unit="share", provenance=o.provenance))
    return out


def observations_from_index(index: domain.FcpiIndex) -> list[domain.PriceObservation]:
    if index.current is None:
        return []
    return [domain.PriceObservation(series_id=index.id, t=index.provenance.observed_at, value=index.current, unit="USD/GPU-hour", provenance=index.provenance)]


def observations_from_markets(markets: Iterable[domain.PredictionMarket]) -> list[domain.PriceObservation]:
    return [domain.PriceObservation(series_id=f"market:{m.id}:yes", t=m.last_updated, value=m.outcomes[0].price, unit="probability", provenance=m.provenance)
            for m in markets if m.outcomes]


def observations_from_bars(bars: Iterable[domain.EquityBar]) -> list[domain.PriceObservation]:
    return [domain.PriceObservation(series_id=f"equity:{b.ticker}:close", t=b.t, value=b.close, unit="USD", provenance=b.provenance) for b in bars]


def observations_from_fundamentals(rows: Iterable[domain.Fundamental]) -> list[domain.PriceObservation]:
    return [domain.PriceObservation(series_id=f"fundamental:{f.ticker}:{f.concept}", t=f.provenance.observed_at, value=f.value, unit=f.unit, provenance=f.provenance) for f in rows]


# -- writing -----------------------------------------------------------------------------

def record(db: Session, observations: Iterable[domain.PriceObservation], *, unchanged_interval: timedelta = UNCHANGED_INTERVAL,
           baskets: dict[str, Iterable[str]] | None = None) -> int:
    """Appends observations idempotently on (series_id, t); returns rows added.

    A point whose value equals the series' latest archived value and arrives
    within `unchanged_interval` of it is skipped (a static ask is not a new
    observation worth a row); an older-or-equal timestamp is always skipped.
    A point the read path would refuse (future-dated, a source no longer
    archivable, an index point with no basket) is never written: `baskets`
    maps an index series id to the sources behind its value.
    """
    now = utcnow()
    stored: dict[int, dict] = {}
    rows = []
    for o in observations:
        if o is None:
            continue
        prov = o.provenance.to_dict()
        if baskets and o.series_id in baskets:
            prov[BASKET_KEY] = sorted(set(baskets[o.series_id]))
        why = refusal(o.series_id, o.t, prov, now=now)
        if why is not None:
            logger.info("archive: %s at %s not recorded (%s)", o.series_id, _aware(o.t).isoformat(), why)
            continue
        stored[id(o)] = prov
        rows.append(o)
    if not rows:
        return 0
    ids = sorted({o.series_id for o in rows})
    latest: dict[str, tuple[datetime, float]] = {}
    newest_t = dict(db.query(Observation.series_id, func.max(Observation.t)).filter(Observation.series_id.in_(ids)).group_by(Observation.series_id).all())
    if newest_t:
        for row in db.query(Observation).filter(Observation.series_id.in_(list(newest_t))).all():
            t = _aware(row.t)
            if _aware(newest_t[row.series_id]) == t:
                latest[row.series_id] = (t, row.value)
    added = 0
    seen: set[tuple[str, datetime]] = set()
    for o in sorted(rows, key=lambda o: (o.series_id, _aware(o.t))):
        t = _aware(o.t)
        if (o.series_id, t) in seen:
            continue
        seen.add((o.series_id, t))
        prior = latest.get(o.series_id)
        if prior is not None:
            if t <= prior[0]:
                continue
            if prior[1] == o.value and t - prior[0] < unchanged_interval:
                continue
        db.add(Observation(series_id=o.series_id, t=t, value=float(o.value), unit=o.unit, provenance=stored[id(o)]))
        latest[o.series_id] = (t, float(o.value))
        added += 1
    if not added:
        return 0
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise
    return added


def record_results(db: Session, results: Iterable[SourceResult], to_observations, *, label: str) -> int:
    """Archives every successful result once per observation time (a cache marker skips repeats)."""
    total = 0
    for result in results:
        if not result.ok or result.provenance is None:
            continue
        if not licences.archivable(result.source):
            continue   # display-only (or unknown) under its licence row: shown, never stored
        # One archive pass per (source, observation time) per cache window: the loader
        # only runs on a miss, so a hit means this fetch was already archived. The cache
        # is fail-open and the unique constraint keeps a repeat idempotent anyway.
        marker = f"flopx:archived:{label}:{result.source}:{_aware(result.provenance.observed_at).isoformat()}"
        state = {"miss": False}

        def mark() -> bool:
            state["miss"] = True
            return True

        try:
            cache.get_or_set(marker, mark, ttl_seconds=MARKER_TTL_SECONDS, tags=None)
        except Exception:
            state["miss"] = True
        if not state["miss"]:
            continue
        try:
            total += record(db, to_observations(result.records))
        except Exception:
            db.rollback()
            logger.warning("archive: %s (%s) not recorded", label, result.source, exc_info=True)
    return total


def archive(db: Session, label: str, results: Iterable[SourceResult], to_observations) -> int:
    """The entry point terminal.py calls after a live fetch; never raises."""
    try:
        return record_results(db, list(results), to_observations, label=label)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("archive: %s failed", label, exc_info=True)
        return 0


def upsert(db: Session, observations: Iterable[domain.PriceObservation], *, extra: dict[int, dict] | None = None,
           window: tuple[datetime, datetime] | None = None) -> tuple[int, int, int]:
    """A bulk, idempotent load keyed on (series_id, t): returns (inserted, updated, unchanged).

    Unlike `record`, which only ever appends past a series' newest point, this
    rewrites a stored row whose value differs from the one handed in (a
    corrected close) and leaves a row with the same value untouched, provenance
    included, so a second load of the same file writes nothing. A point the read
    path would refuse (an unarchivable source, a future date) is skipped and
    counted as unchanged. `extra` maps `id(observation)` to keys stored beside
    the provenance (an audit trail the reads ignore). `window` (inclusive first
    and last `t` of the batch) limits the stored rows compared against to that
    span, so a loader writing a long series chunk by chunk does not re-read the
    whole series per chunk; every observation must fall inside it. One commit
    for the batch.
    """
    now = utcnow()
    rows: list[tuple[domain.PriceObservation, dict]] = []
    for o in observations:
        prov = o.provenance.to_dict()
        if extra and id(o) in extra:
            prov.update(extra[id(o)])
        if refusal(o.series_id, o.t, prov, now=now) is not None:
            continue
        rows.append((o, prov))
    if not rows:
        return 0, 0, 0
    ids = sorted({o.series_id for o, _ in rows})
    existing: dict[tuple[str, datetime], Observation] = {}
    for chunk in range(0, len(ids), 200):
        query = db.query(Observation).filter(Observation.series_id.in_(ids[chunk:chunk + 200]))
        if window is not None:
            query = query.filter(Observation.t >= _aware(window[0]), Observation.t <= _aware(window[1]))
        for row in query.all():
            existing[(row.series_id, _aware(row.t))] = row
    inserted = updated = unchanged = 0
    seen: set[tuple[str, datetime]] = set()
    for o, prov in rows:
        key = (o.series_id, _aware(o.t))
        if key in seen:
            continue
        seen.add(key)
        value = float(o.value)
        row = existing.get(key)
        if row is None:
            db.add(Observation(series_id=o.series_id, t=key[1], value=value, unit=o.unit, provenance=prov))
            inserted += 1
        elif row.value != value or row.unit != o.unit:
            row.value, row.unit, row.provenance = value, o.unit, prov
            updated += 1
        else:
            unchanged += 1
    if inserted or updated:
        try:
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            raise
    return inserted, updated, unchanged


def delete_source(db: Session, source: str, *, series_prefix: str = "") -> int:
    """Deletes every stored row whose provenance names `source`; returns how many went.

    The archive otherwise never deletes (R1-5 keeps rows and refuses them at read); this
    exists for a source whose licence ends with the project (decision G13). The match is
    on the stored provenance, in Python, so it holds on every database the suite runs on.
    """
    query = db.query(Observation)
    if series_prefix:
        query = query.filter(Observation.series_id.like(f"{series_prefix}%"))
    gone = 0
    for row in query.all():
        if str((row.provenance or {}).get("source", "")) == source:
            db.delete(row)
            gone += 1
    if gone:
        try:
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            raise
    return gone


# -- reading -------------------------------------------------------------------------------
#
# Reads come back as column tuples, never ORM `Observation` rows (B-MEM2). A sweep over the two-year power
# archive materialised tens of thousands of ORM instances, each with its identity-map entry, its state and
# its whole JSONB provenance decoded into a fresh dict (measured with tracemalloc: a 225 MB transient per
# sweep, the JSONB strings alone 39.5 MB). The provenance fields a point needs are extracted in SQL, one
# column each, through SQLAlchemy's dialect-portable JSON index (`->>` on PostgreSQL, `json_extract` on
# SQLite), and the `Provenance` is built from those columns with the repeated strings (licence, note,
# source, URL) shared across the rows of one read. Equal strings are interchangeable: nothing a reader sees
# changes. Times are parsed per row and the text dropped.

BUCKETS = {"hour": ("hourly mean", "%Y-%m-%dT%H:00:00"), "day": ("daily mean", "%Y-%m-%dT00:00:00")}

_PROV = Observation.provenance
_KIND, _SOURCE = _PROV["kind"].as_string(), _PROV["source"].as_string()
_OBSERVED = func.coalesce(_PROV["observed_at"].as_string(), _PROV["observedAt"].as_string())
_FETCHED = func.coalesce(_PROV["fetched_at"].as_string(), _PROV["fetchedAt"].as_string())
_URL = func.coalesce(_PROV["source_url"].as_string(), _PROV["sourceUrl"].as_string())
_LICENCE, _NOTE = _PROV["licence"].as_string(), _PROV["note"].as_string()
_INPUT_KINDS = func.coalesce(_PROV["input_kinds"], _PROV["inputKinds"])
_BASKET = _PROV[BASKET_KEY]
# One row per stored point: (series_id, t, value, unit, kind, source, observed_at, fetched_at, source_url, licence, note, input_kinds, basket).
_COLUMNS = (Observation.series_id, Observation.t, Observation.value, Observation.unit, _KIND, _SOURCE, _OBSERVED, _FETCHED, _URL,
            _LICENCE, _NOTE, _INPUT_KINDS, _BASKET)


def _rows(db: Session, *filters, order=None):
    query = db.query(*_COLUMNS).filter(*filters)
    return query.order_by(*order) if order else query


def _refusal_row(row, now: datetime, *, public_only: bool = False) -> str | None:
    """`refusal()` over the tuple's columns: the stored provenance keys it reads (kind, source, fetched_at, basket)."""
    source = row[5] or ""
    if public_only and not licences.public(source):
        return "source-off"
    return refusal(row[0], row[1], {"kind": row[4], "source": source, "fetched_at": row[7], BASKET_KEY: row[12]}, now=now)


def _servable_row(row, now: datetime, *, public_only: bool = False) -> bool:
    return _refusal_row(row, now, public_only=public_only) is None


def _share(value, strings: dict[str, str] | None):
    return strings.setdefault(value, value) if strings is not None and isinstance(value, str) else value


def _archived_point(row, strings: dict[str, str] | None = None) -> domain.PriceObservation:
    """A stored point from its column tuple, labelled archived (a sample value could never have been archived,
    but if one ever were it keeps saying sample rather than borrowing the observed label; a derived point keeps
    its method name and input kinds on the envelope)."""
    series_id, t, value, unit, kind, source, observed, fetched, url, licence, note, kinds, _basket = row
    t = _aware(t)
    prov = Provenance(kind="sample" if kind == "sample" else "archived", source=_share(source, strings), observed_at=_parse(observed) or t,
                      source_url=_share(url, strings), fetched_at=_parse(fetched), licence=_share(licence, strings), note=_share(note, strings),
                      input_kinds=tuple(kinds) if kinds else ())
    return domain.PriceObservation(series_id=series_id, t=t, value=value, unit=unit, provenance=prov)


def _bucket_expr(db: Session, bucket: str):
    """`t` truncated to the bucket: `date_trunc` on PostgreSQL, `strftime` text on SQLite (the suite's backend)."""
    if db.get_bind().dialect.name == "postgresql":
        return func.date_trunc(bucket, func.timezone("UTC", Observation.t))   # UTC buckets whatever the session time zone
    return func.strftime(BUCKETS[bucket][1], Observation.t)


def _bucketed_series(db: Session, series_id: str, since: datetime | None, bucket: str, *, public_only: bool) -> domain.Series | None:
    """`archived_series` aggregated in SQL: one point per `bucket` (hour or day), its value the mean of the stored
    slots in that bucket, dated at the bucket start. Slots are grouped by the provenance strings that identify
    their class (source, kind, licence, URL), so a mean is only ever taken over one class; the refusal rules run
    per group with the group's newest slot time and fetch time (a future-dated slot was already refused when it
    was written). The point's note says what it is ("hourly mean of 4 slots") and quotes one slot's own note
    (a slot note names its settlement period or delivery slot, which is why notes do not define a group)."""
    label, _ = BUCKETS[bucket]
    period = _bucket_expr(db, bucket)
    query = (db.query(Observation.series_id, period, func.avg(Observation.value), func.min(Observation.unit), _KIND, _SOURCE, func.max(_OBSERVED),
                      func.max(_FETCHED), _URL, _LICENCE, func.min(_NOTE), func.count(), func.max(Observation.t))
             .filter(Observation.series_id == series_id))
    if since is not None:
        query = query.filter(Observation.t >= _aware(since))
    query = query.group_by(Observation.series_id, period, _KIND, _SOURCE, _URL, _LICENCE).order_by(period.asc())
    now = utcnow()
    strings: dict[str, str] = {}
    points: list[domain.PriceObservation] = []
    for sid, start, mean, unit, kind, source, observed, fetched, url, licence, note, count, newest in query.all():
        if not _servable_row((sid, newest, mean, unit, kind, source, observed, fetched, url, licence, note, None, None), now, public_only=public_only):
            continue
        slots = f"{label} of {count} slot{'s' if count != 1 else ''}"
        row = (sid, _parse(start), float(mean), unit, kind, source, observed, fetched, url, licence, f"{slots} (one slot's note: {note})" if note else slots, None, None)
        points.append(_archived_point(row, strings))
    return domain.Series(id=series_id, unit=points[0].unit, points=points) if points else None


def archived_series(db: Session, series_id: str, since: datetime | None = None, *, public_only: bool = False,
                    bucket: str | None = None) -> domain.Series | None:
    """Servable observations archived under `series_id` since `since`, labelled archived; None when none are.
    `public_only` leaves out every point of a `research-archive` source (the view an anonymous reader gets).
    `bucket` ("hour" or "day") returns one mean point per bucket instead of every stored slot (see
    `_bucketed_series`); the raw slots stay in the archive."""
    if bucket is not None:
        return _bucketed_series(db, series_id, since, bucket, public_only=public_only)
    filters = [Observation.series_id == series_id]
    if since is not None:
        filters.append(Observation.t >= _aware(since))
    now = utcnow()
    strings: dict[str, str] = {}
    points = [_archived_point(r, strings) for r in _rows(db, *filters, order=(Observation.t.asc(),)) if _servable_row(r, now, public_only=public_only)]
    return domain.Series(id=series_id, unit=points[0].unit, points=points) if points else None


def observed_series(db: Session, series_ids: Iterable[str]) -> dict[str, domain.Series]:
    """Every listed series the archive holds servable points for, whole, oldest first: what an engine reads in
    place of an authored series when real observations exist (the gated source included; the engines' results
    are derived and public). Series with no stored point are absent from the result."""
    ids = sorted(set(series_ids))
    if not ids:
        return {}
    now = utcnow()
    out: dict[str, list[domain.PriceObservation]] = {}
    strings: dict[str, str] = {}
    for row in _rows(db, Observation.series_id.in_(ids), order=(Observation.series_id, Observation.t.asc())):
        if _servable_row(row, now):
            out.setdefault(row[0], []).append(_archived_point(row, strings))
    return {sid: domain.Series(id=sid, unit=points[0].unit, points=points) for sid, points in out.items()}


def archived_index_history(db: Session, series_id: str) -> tuple[list[tuple[domain.PriceObservation, tuple[str, ...]]], int]:
    """An index's servable archived points with the basket each was computed from, oldest first, plus how
    many stored points were withheld (no recorded basket, or a basket holding a source that is now off)."""
    now = utcnow()
    strings: dict[str, str] = {}
    kept, withheld = [], 0
    for row in _rows(db, Observation.series_id == series_id, order=(Observation.t.asc(),)):
        if _servable_row(row, now):
            kept.append((_archived_point(row, strings), tuple(row[12]) if row[12] else ()))
        else:
            withheld += 1
    return kept, withheld


def latest_before(db: Session, series_id: str, before: datetime, *, public_only: bool = False) -> domain.PriceObservation | None:
    """The newest servable archived point strictly older than `before` (the previous observation of an entity)."""
    now = utcnow()
    for row in _rows(db, Observation.series_id == series_id, Observation.t < _aware(before), order=(Observation.t.desc(),)):
        if _servable_row(row, now, public_only=public_only):
            return _archived_point(row)
    return None


def latest_points(db: Session, series_ids: Iterable[str], at_or_before: datetime) -> dict[str, domain.PriceObservation]:
    """One query: the newest servable archived point at or before `at_or_before` for each series id."""
    ids = sorted(set(series_ids))
    if not ids:
        return {}
    out: dict[str, domain.PriceObservation] = {}
    now = utcnow()
    strings: dict[str, str] = {}
    for row in _rows(db, Observation.series_id.in_(ids), Observation.t <= _aware(at_or_before), order=(Observation.series_id, Observation.t.desc())):
        if row[0] not in out and _servable_row(row, now):
            out[row[0]] = _archived_point(row, strings)
    return out


def extend(series: domain.Series, db: Session | None, *, since: datetime | None = None, public_only: bool = False) -> domain.Series:
    """`series` plus any archived points newer than its last point (labelled archived)."""
    if db is None:
        return series
    last = _aware(series.points[-1].t) if series.points else None
    try:
        # Only rows that can extend the series are read: a point at or before its last one is dropped below anyway,
        # and reading a whole archived equity series (two thousand rows with their provenance) per sample series per
        # sweep was a measured share of the worker's memory peak (B-MEM).
        archived = archived_series(db, series.id, since if since is not None else last, public_only=public_only)
    except SQLAlchemyError:
        logger.warning("archive: read of %s failed", series.id, exc_info=True)
        return series
    if archived is None:
        return series
    extra = [p for p in archived.points if last is None or p.t > last]
    if not extra:
        return series
    return domain.Series(id=series.id, unit=series.unit, points=[*series.points, *extra])
