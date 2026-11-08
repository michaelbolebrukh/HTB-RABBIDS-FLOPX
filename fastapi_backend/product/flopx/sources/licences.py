"""The licence policy table: one row per live source, read by both tiers.

`licences.json` beside this module is the single source of truth (the frontend
holds a byte-identical copy, pinned by its drift test). Each row names the
source id the adapters stamp on provenance (plus aliases, for a source with two
endpoints), the terms URL, the licence string, the attribution a reader must
see, whether the licence statement is `asserted` (not read in full) or
measured, and the `use` the terms allow:

- `display-and-archive`: shown and stored in the archive;
- `display-only`: shown, never stored (`archivable()` is False);
- `display-not-index`: shown and used to derive (offers, the regional pressure
  grid), never stored (`archivable()` is False) and never in an FCPI basket or
  any cross-provider aggregate presented as an index or benchmark
  (`in_index()` is False). Every surface carrying such a row must say so: the
  row's `attribution` is that line, and the adapter puts it first in each
  record's provenance note. Vast.ai is the one such row (founder decision G9-b);
- `research-archive`: stored in the archive (`archivable()` is True) and used by
  every derived engine, whose results are public, but the raw points are served
  only to signed-in accounts (`public()` is False). The one such row is the
  founder's WRDS CRSP extract (decision G13): licensed to an institution for
  research, so the series itself is not republished to the world. Changing the
  row's use to `display-and-archive` lifts the gate; nothing else has to move;
- `off`: never called and never listed anywhere, not even as unavailable
  (`enabled()` is False, and the composition layer leaves the adapter out).

A source id the table does not know is treated as `off`, so a new adapter
cannot ship without a decided row. Lookup takes the exact id, then an alias,
then the part before the first colon (`kalshi:KXPOWERKWH` -> `kalshi`).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

TABLE_PATH = Path(__file__).with_name("licences.json")
USES = ("display-and-archive", "display-only", "display-not-index", "research-archive", "off")
ARCHIVABLE_USES = ("display-and-archive", "research-archive")


@lru_cache(maxsize=1)
def table() -> dict:
    data = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    for row in data["sources"]:
        if row["use"] not in USES:
            raise ValueError(f"licences.json: {row['id']} has unknown use {row['use']!r}")
    return data


@lru_cache(maxsize=1)
def _index() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in table()["sources"]:
        for key in (row["id"], *row.get("aliases", [])):
            if key in out:
                raise ValueError(f"licences.json: duplicate source id {key!r}")
            out[key] = row
    return out


def policy(source: str) -> dict | None:
    """The table row for a source id (exact, alias, then the prefix before ':'), or None."""
    index = _index()
    if source in index:
        return index[source]
    return index.get(source.split(":", 1)[0])


def enabled(source: str) -> bool:
    """False for a source whose terms rule it out (`off`) or that has no row at all."""
    row = policy(source)
    return row is not None and row["use"] != "off"


def archivable(source: str) -> bool:
    row = policy(source)
    return row is not None and row["use"] in ARCHIVABLE_USES


def public(source: str) -> bool:
    """False for a source whose row says `research-archive`: its raw points reach signed-in accounts only, while
    everything derived from them is public. A source with no row has nothing to gate (it is never served at all)."""
    row = policy(source)
    return row is None or row["use"] != "research-archive"


def in_index(source: str) -> bool:
    """False for a source whose row says `display-not-index`: it may be shown and derived from, never put
    in an index basket. Sample rows and derived-method ids have no row and are not excluded here; whether a
    source may be called at all is `enabled()`'s question."""
    row = policy(source)
    return row is None or row["use"] != "display-not-index"


def licence(source: str) -> str:
    """The licence string a provenance carries: the table's licence, plus the asserted caveat when unverified."""
    row = policy(source)
    if row is None:
        raise KeyError(f"no licence row for {source!r}")
    return f"{row['licence']} {table()['assertedSuffix']}" if row["asserted"] else row["licence"]


def attribution(source: str) -> str | None:
    row = policy(source)
    return row["attribution"] if row else None


def rows() -> list[dict]:
    """Every source that is not `off`, for the public sources page."""
    return [row for row in table()["sources"] if row["use"] != "off"]
