"""The provenance envelope every FLOPX record carries (architecture §2).

Four kinds: `live` (read from a named source now), `archived` (read earlier and
stored), `sample` (authored, illustrative), `derived` (computed). A derived value
built on any sample input is treated as sample downstream, which is why
`derived()` records its inputs' kinds and `effective_kind()` collapses them.
Live aggregates never include sample rows; `assert_unmixed()` is the guard.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Iterable, Literal

from product.exceptions import MixedProvenanceError


Kind = Literal["live", "archived", "sample", "derived"]

SAMPLE_SOURCE = "flopx-sample-v1"
SAMPLE_AUTHORED_AT = datetime(2026, 11, 7, tzinfo=timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Provenance:
    kind: Kind
    source: str
    observed_at: datetime
    source_url: str | None = None
    fetched_at: datetime | None = None
    licence: str | None = None
    note: str | None = None
    # For `derived` only: the distinct kinds of the inputs it was computed from.
    # Not in the §2 TS type by name, but it is what lets the UI collapse a
    # derived-from-sample value to SAMPLE without re-deriving anything:
    input_kinds: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        data = asdict(self)
        data["observed_at"] = _aware(self.observed_at).isoformat()
        if self.fetched_at is not None:
            data["fetched_at"] = _aware(self.fetched_at).isoformat()
        data["input_kinds"] = list(self.input_kinds)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Provenance":
        """Accepts both the camelCase wire shape and the snake_case Python shape."""
        def pick(*names):
            for name in names:
                if name in data and data[name] is not None:
                    return data[name]
            return None

        observed = pick("observed_at", "observedAt")
        fetched = pick("fetched_at", "fetchedAt")
        kinds = pick("input_kinds", "inputKinds") or ()
        return cls(
            kind=data["kind"],
            source=data["source"],
            observed_at=_parse_dt(observed),
            source_url=pick("source_url", "sourceUrl"),
            fetched_at=_parse_dt(fetched) if fetched else None,
            licence=pick("licence"),
            note=pick("note"),
            input_kinds=tuple(kinds),
        )


def _parse_dt(value) -> datetime:
    if isinstance(value, datetime):
        return _aware(value)
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return _aware(datetime.fromisoformat(text))


# -- constructors ----------------------------------------------------------------

def sample(observed_at: datetime | None = None, note: str | None = None, source: str = SAMPLE_SOURCE) -> Provenance:
    return Provenance(kind="sample", source=source, observed_at=observed_at or SAMPLE_AUTHORED_AT, note=note)


def live(
    source: str,
    observed_at: datetime,
    *,
    fetched_at: datetime | None = None,
    source_url: str | None = None,
    licence: str | None = None,
    note: str | None = None,
) -> Provenance:
    return Provenance(
        kind="live", source=source, observed_at=observed_at,
        fetched_at=fetched_at or utcnow(), source_url=source_url, licence=licence, note=note,
    )


def archived(source: str, observed_at: datetime, fetched_at: datetime, **extra) -> Provenance:
    return Provenance(kind="archived", source=source, observed_at=observed_at, fetched_at=fetched_at, **extra)


def derived(method: str, inputs: Iterable[Provenance], *, observed_at: datetime | None = None, note: str | None = None) -> Provenance:
    """A computed value: `source` names the method, `input_kinds` the inputs' kinds.

    `observed_at` defaults to the newest input observation, never to "now": a
    derived number is only as fresh as the data it was derived from.
    """
    inputs = list(inputs)
    kinds = tuple(sorted({effective_kind(p) for p in inputs}))
    newest = max((_aware(p.observed_at) for p in inputs), default=None)
    return Provenance(
        kind="derived",
        source=method,
        observed_at=observed_at or newest or utcnow(),
        fetched_at=utcnow(),
        note=note,
        input_kinds=kinds,
    )


# -- predicates ------------------------------------------------------------------

def effective_kind(prov: Provenance) -> str:
    """`sample` if the value is sample OR derived from any sample input; else its kind."""
    if prov.kind == "sample":
        return "sample"
    if prov.kind == "derived" and "sample" in prov.input_kinds:
        return "sample"
    return prov.kind


def is_sample(prov: Provenance) -> bool:
    return effective_kind(prov) == "sample"


def is_stale(prov: Provenance, budget: timedelta, now: datetime | None = None) -> bool:
    """True when the observation is older than the metric's freshness budget."""
    return (now or utcnow()) - _aware(prov.observed_at) > budget


def assert_unmixed(provs: Iterable[Provenance]) -> str:
    """Refuses a set mixing sample rows with live/archived ones; returns the common kind.

    Returns the effective kind shared by every row (`sample`, or `live`/`archived`
    when nothing is sample). A live + archived mix is allowed (both observed), a
    sample + anything-observed mix is not.
    """
    kinds = {effective_kind(p) for p in provs}
    if "sample" in kinds and kinds != {"sample"}:
        raise MixedProvenanceError(kinds=sorted(kinds))
    if not kinds:
        return "derived"
    return "sample" if kinds == {"sample"} else ("live" if "live" in kinds else "archived")


@dataclass(frozen=True)
class Freshness:
    """The per-response freshness block every list response carries (§4).

    `sources` names every source whose rows the response used; `source_details`
    adds each live source's newest observation time and kind; `unavailable`
    lists the live sources that were asked and did not answer, with the reason.
    """
    generated_at: datetime
    oldest_observed_at: datetime | None
    sources: tuple[str, ...]
    source_details: tuple[dict, ...] = ()
    unavailable: tuple[dict, ...] = ()

    @classmethod
    def of(cls, provs: Iterable[Provenance], *, unavailable: Iterable[dict] = ()) -> "Freshness":
        provs = list(provs)
        oldest = min((_aware(p.observed_at) for p in provs), default=None)
        newest: dict[str, Provenance] = {}
        for p in provs:
            if p.kind in ("live", "archived") and (p.source not in newest or _aware(p.observed_at) > _aware(newest[p.source].observed_at)):
                newest[p.source] = p
        details = tuple(
            {"source": source, "kind": p.kind, "observed_at": _aware(p.observed_at).isoformat(), "licence": p.licence}
            for source, p in sorted(newest.items())
        )
        return cls(generated_at=utcnow(), oldest_observed_at=oldest, sources=tuple(sorted({p.source for p in provs})),
                   source_details=details, unavailable=tuple(unavailable))
