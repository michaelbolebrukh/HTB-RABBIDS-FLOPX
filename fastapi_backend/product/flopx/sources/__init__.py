"""Live source adapters (architecture §7), one module per upstream.

Each exposes `fetch_*() -> SourceResult`: `ok` with records carrying `kind: live`
provenance and the upstream's own observation time (or the response Date
header, with the note saying so), or `unavailable` with the source name and a
reason token. An adapter degrades to UNAVAILABLE, never to sample data; the
sample tier is served by product.flopx.data and the UI says which it is showing.
All HTTP goes through `_http` (5 s timeout, declared User-Agent, platform cache
with a TTL per source and a short TTL for failures, `httpx.MockTransport` seam
for tests); entity mapping lives in `_mapping`.

Adapters: `vast`, `runpod`, `akash` (GPU asks), `polymarket`, `kalshi`
(prediction markets and their outcome series), `sec` (fundamentals, filings,
frames), `energy` (Energy-Charts DE-LU, Elexon GB), `news` (Hacker News and
four investor-relations RSS feeds), `wikidata` (notable data centres), `yahoo`
(equity bars, unofficial), `alpaca` (dormant behind PRODUCT_ALPACA_*),
`getdeploying` (a year of weekly GPU rental medians, CC BY 4.0, read by its loader
rather than by a terminal read) and `eia` (ERCOT and PJM hourly demand and day-ahead
demand forecast from Form EIA-930, US public domain, dormant behind EIA_API_KEY).
Research R3 (2026-11-07) verified every endpoint keyless from the sandbox;
GDELT and Google News RSS are deliberately absent (rate limit; licence).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from product.flopx.provenance import Provenance


@dataclass(frozen=True)
class SourceResult:
    source: str
    ok: bool
    records: list[Any] = field(default_factory=list)
    provenance: Provenance | None = None
    reason: str | None = None

    @property
    def unavailable(self) -> dict | None:
        return None if self.ok else {"source": self.source, "reason": self.reason or "unavailable"}


def unavailable(source: str, reason: str) -> SourceResult:
    return SourceResult(source=source, ok=False, reason=reason)


def available(source: str, records: list[Any], provenance: Provenance) -> SourceResult:
    return SourceResult(source=source, ok=True, records=records, provenance=provenance)
