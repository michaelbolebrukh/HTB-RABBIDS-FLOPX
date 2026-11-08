"""Request-shaped assembly over the FLOPX domain: what every router calls.

Functions take the database session first, then request data, and return
plain data (dicts / dataclasses) the response models serialise. Every payload
carries `freshness` built from the provenances it actually used plus the live
sources that were asked and did not answer.

The merge policy (brief B1, deliverable D), applied by every read here:

- A row carries its own provenance. Lists mix live and sample rows freely;
  each row says which it is.
- An aggregate (an FCPI level, a heatmap cell, a snapshot median) is computed
  over ONE provenance class and labelled with it. If live offers exist for a
  model, that model's index is live-only; a model with no live offers keeps
  its sample index. Never a median over a mix.
- Live where an adapter answered, sample otherwise; an adapter that did not
  answer is listed in `freshness.unavailable` with its reason, never silently
  replaced.
- Prediction markets from Polymarket and Kalshi replace a sample market with
  the same id and are appended otherwise. Feed items from an investor-relations
  release or an SEC filing are `verified` (primary source); a Hacker News story
  is `interpretation`.
- Every successful live fetch is archived (archive.py) inside its own
  try/except, so an archive failure never fails a read; archived points extend
  a series labelled `archived`.

Live calls fan out on a small thread pool so a cold cache costs one timeout,
not the sum of them; every adapter is cached and fail-soft, so the pool only
ever waits on the network.
"""

from __future__ import annotations

import functools
import inspect
import logging
import statistics
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

from sqlalchemy.orm import Session

from infrastructure.cache import cache
from platform.models import User
from product.exceptions import PaperOrderError, SignInRequiredError, UnknownEntityError, UnknownSeriesError
from product.flopx import archive, refresher
from product.flopx import ask as ask_grammar
from product.flopx import changes as change_detection
from product.flopx import anomalies as anomaly_engine, assistant, domain, effective_supply as supply_engine, evaluation, exposure as exposure_engine
from product.flopx import forecast as forecasting, fund as fund_engine, fundamentals as fundamentals_engine, infra as infra_reads, pressure, procurement, scenario as scenarios, signals as signal_chains
from product.flopx import strategies, xray as xray_lab
from product.flopx.adjust import adjust_offer
from product.flopx.data.loader import SampleData, load_sample
from product.flopx.indices import compute_index, sample_index
from product.flopx.paper import broker
from product.flopx.provenance import Freshness, Provenance, derived, effective_kind, utcnow
from product.flopx.series import change_pct, last_point, resolve_series
from product.flopx.sources import _http
from product.flopx.sources import SourceResult, akash, eia, energy, kalshi, licences, news, polymarket, runpod, sec, vast, wikidata, yahoo
from product.models import PaperAccount, PaperOrder, SavedWorkload, StrategyDecision
from product.validators import parse_range

logger = logging.getLogger(__name__)

__all__ = [
    "freshness", "overview", "offers", "heatmap", "series", "indices", "index", "changes",
    "facilities", "regions", "companies", "company", "bars", "markets", "market", "feed",
    "xray", "forecast", "scenario", "factors", "strategies_view", "strategy_view", "strategy_step",
    "paper_account", "paper_place_order", "paper_orders", "paper_cancel", "paper_close", "paper_close_all",
    "procure_plan", "save_workload", "ask", "price_lookup_for", "instrument_check_for",
    "pressure_grid", "pressure_cell", "effective_supply_view", "anomalies_view", "anomaly_detail", "scenario_infra", "exposure_view", "exposure_view_for",
    "evaluation_view", "evaluation_portfolio", "signals_view", "signal_detail", "decision_accept", "fund_summary",
    "infra_energy", "infra_projects", "infra_grid",
]

POOL_WORKERS = 24   # the overview's five independent stages run as one fan-out (V6 #12)
FEED_FILING_TICKERS = ("IREN", "NBIS")   # the two with no reachable press feed (R3)


def freshness(provs: Iterable[Provenance], unavailable: Iterable[dict] = ()) -> Freshness:
    return Freshness.of(provs, unavailable=unavailable)


def _data() -> SampleData:
    return load_sample()


def _round(value: float | None, places: int = 4) -> float | None:
    return None if value is None else round(value, places)


def _snapshotted(fn=None, *, ttl: float | None = None, follows_upstream: bool = True):
    """B-PERF: a user-independent read answers from this process's warm composition of the same
    arguments (`refresher.composed`: stale-while-revalidate while the refresher runs, a plain call
    otherwise). The first positional argument is the session when the function takes one; the key
    is the function name and every other argument. `ttl` / `follows_upstream` pass through."""
    if fn is None:
        return functools.partial(_snapshotted, ttl=ttl, follows_upstream=follows_upstream)
    options = {"follows_upstream": follows_upstream, **({"ttl": ttl} if ttl is not None else {})}
    takes_db = next(iter(inspect.signature(fn).parameters), None) == "db"

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if takes_db:
            db, rest = args[0], args[1:]
            key = f"flopx:read:{fn.__name__}:{rest!r}:{sorted(kwargs.items())!r}"
            return refresher.composed(key, lambda session: fn(session, *rest, **kwargs), db, **options)
        key = f"flopx:read:{fn.__name__}:{args!r}:{sorted(kwargs.items())!r}"
        return refresher.composed(key, lambda _session: fn(*args, **kwargs), None, **options)

    wrapper.uncomposed = fn
    return wrapper


# -- live fan-out and the per-request ledger ---------------------------------------------

def _gather(calls: list[Callable[[], SourceResult]]) -> list[SourceResult]:
    """Runs independent adapter calls concurrently; an adapter that raises is reported unavailable."""
    if not calls:
        return []

    def safe(call: Callable[[], SourceResult]) -> SourceResult:
        try:
            return call()
        except Exception:  # adapters are fail-soft by contract; this is the last net under a bug
            logger.warning("live adapter raised", exc_info=True)
            return SourceResult(source=getattr(call, "__module__", "live").rsplit(".", 1)[-1], ok=False, reason="adapter-error")

    if len(calls) == 1 or _http.serving():
        # Serving (B-PERF): every adapter answers from memory, so a per-request pool of up to 24
        # threads would buy nothing and cost thread stacks under a 30-request page load.
        return [safe(call) for call in calls]
    with ThreadPoolExecutor(max_workers=min(POOL_WORKERS, len(calls))) as pool:
        return list(pool.map(safe, calls))


def _enabled(*pairs: tuple[str, Callable[[], SourceResult]]) -> list[Callable[[], SourceResult]]:
    """The adapter calls whose licence row allows use; an `off` source is never called and never named."""
    return [call for source, call in pairs if licences.enabled(source)]


class Ledger:
    """What a request used and what it asked for in vain, for the freshness block."""

    def __init__(self):
        self.results: list[SourceResult] = []
        self.unavailable: list[dict] = []

    def note_unavailable(self, entry: dict) -> None:
        """Adds an unavailable entry once: the same source and reason is never listed twice."""
        if not any(e["source"] == entry["source"] and e["reason"] == entry["reason"] for e in self.unavailable):
            self.unavailable.append(entry)

    def take(self, results: Iterable[SourceResult]) -> list:
        records = []
        for result in results:
            self.results.append(result)
            if result.ok:
                records.extend(result.records)
            elif result.unavailable:
                self.note_unavailable(result.unavailable)
        return records

    def freshness(self, provs: Iterable[Provenance]) -> Freshness:
        return Freshness.of(provs, unavailable=self.unavailable)

    def ok(self, source_prefix: str) -> list[SourceResult]:
        return [r for r in self.results if r.ok and r.source.startswith(source_prefix)]


def _offer_calls() -> list[Callable[[], SourceResult]]:
    # Vast.ai's licence row is `display-not-index` (founder decision G9-b): its asks are shown in the
    # offers table and feed the regional pressure grid, but never an index basket (`_index_rows`)
    # and never the archive (`_archive_offers_and_indices` and archive.py's refusal).
    return _enabled((vast.SOURCE, vast.fetch_gpu_offers), (runpod.SOURCE, runpod.fetch_gpu_offers), (akash.SOURCE, akash.fetch_gpu_offers))


def _market_calls() -> list[Callable[[], SourceResult]]:
    return _enabled((polymarket.SOURCE, polymarket.fetch_prediction_markets), *[(kalshi.SOURCE, (lambda s=s: kalshi.fetch_series(s))) for s in kalshi.SERIES])


def _news_calls(*, with_filings: bool) -> list[Callable[[], SourceResult]]:
    pairs = [(news.HN, news.fetch_hn), *[(f"rss:{k}", (lambda k=k: news.fetch_rss(k))) for k in news.RSS_FEEDS]]
    if with_filings:
        pairs += [(sec.SOURCE, (lambda t=t: sec.fetch_filings(sec.CIKS[t], t))) for t in FEED_FILING_TICKERS]
    return _enabled(*pairs)


def _energy_calls() -> list[Callable[[], SourceResult]]:
    # EIA (ERCOT and PJM hourly demand, N-EIA) joins the list only with its key: without it the zones read
    # exactly as before the adapter existed, no job registers and no `not-configured` entry reaches a freshness block.
    pairs = [(energy.ENERGY_CHARTS, energy.fetch_energy_charts), (energy.ELEXON, energy.fetch_elexon)]
    if eia.configured():
        pairs += [(eia.SOURCE, (lambda z=z: eia.fetch_zone(z))) for z in eia.ZONES]
    return _enabled(*pairs)


def _facility_calls() -> list[Callable[[], SourceResult]]:
    return _enabled((wikidata.SOURCE, wikidata.fetch_facilities))


def _live_offers(ledger: Ledger) -> list[domain.GpuOffer]:
    return ledger.take(_gather(_offer_calls()))


def _index_rows(offers: Iterable[domain.GpuOffer]) -> list[domain.GpuOffer]:
    """The asks an index basket, or any cross-provider median presented as a market level, may read:
    every row except those from a `display-not-index` source (Vast.ai, a current market reading only)."""
    return [o for o in offers if licences.in_index(o.provenance.source)]


def _live_markets(ledger: Ledger) -> list[domain.PredictionMarket]:
    return ledger.take(_gather(_market_calls()))


def _live_news(ledger: Ledger, *, with_filings: bool) -> list[domain.IntelItem]:
    return ledger.take(_gather(_news_calls(with_filings=with_filings)))


def _archive(db: Session | None, label: str, results: Iterable[SourceResult], to_observations) -> None:
    if db is None:
        return
    try:
        archive.archive(db, label, results, to_observations)
    except Exception:
        logger.warning("archive %s skipped", label, exc_info=True)


def _archive_offers_and_indices(db: Session | None, ledger: Ledger, indices_live: list[domain.FcpiIndex]) -> None:
    if db is None:
        return
    # Only display-and-archive marketplaces are passed; archive.record_results refuses any other source
    # again (Vast.ai's `display-not-index` row is never stored), so a slip here still writes nothing.
    _archive(db, "offers", ledger.ok(runpod.SOURCE) + ledger.ok(akash.SOURCE), archive.observations_from_offers)
    try:
        archive.record(db, [o for ix in indices_live for o in archive.observations_from_index(ix)], baskets={ix.id: ix.basket for ix in indices_live})
    except Exception:
        db.rollback()
        logger.warning("archive of live indices skipped", exc_info=True)


# -- pricing (the one live seam the desk uses) ----------------------------------

def price_lookup_for(data: SampleData, *, allow_live: bool = True) -> broker.PriceLookup:
    """Latest close: live (Yahoo) when it answers, else the sample close, else None."""
    def lookup(ticker: str) -> Optional[domain.PriceObservation]:
        if allow_live and licences.enabled(yahoo.SOURCE):
            live_close = yahoo.fetch_latest_close(ticker)
            if live_close is not None:
                return live_close
        sample = data.series.get(f"equity:{ticker}:close")
        return last_point(sample) if sample else None
    return lookup


def instrument_check_for(data: SampleData):
    return lambda ticker: ticker.upper() in data.companies


def _theme_lookup(data: SampleData):
    return lambda ticker: data.companies[ticker].compute_exposure.tags if ticker in data.companies else []


# -- indices: live-only per model when live offers exist, sample otherwise ----------------

def _index_for(db: Session | None, data: SampleData, gpu_model_id: str, live_offers: list[domain.GpuOffer]) -> domain.FcpiIndex:
    model = data.gpu_models.get(gpu_model_id)
    if model is None:
        raise UnknownEntityError(entity="gpuModel", id=gpu_model_id)
    # The basket never holds a `display-not-index` source (Vast.ai, decision G9-b): with only such
    # asks for the model, the index stays the sample one, labelled sample.
    rows = [o for o in _index_rows(live_offers) if o.gpu_model_id == gpu_model_id]
    if not rows:
        return sample_index(data, gpu_model_id)
    ix = compute_index(rows, model, data.regions)
    basket = sorted({o.provenance.source for o in rows})
    # History for a live index is what earlier fetches archived (never the rescaled sample series), each point
    # with the basket of sources it was computed from; points with no recorded basket, or built from a source
    # that is now off, are withheld by the archive (R1-5).
    history: list[tuple[domain.PriceObservation, tuple[str, ...]]] = []
    withheld = 0
    if db is not None:
        try:
            history, withheld = archive.archived_index_history(db, ix.id)
        except Exception:
            logger.warning("archived index history unavailable for %s", ix.id, exc_info=True)
    current = domain.PriceObservation(series_id=ix.id, t=ix.provenance.observed_at, value=ix.current, unit="USD/GPU-hour", provenance=ix.provenance)
    pairs = [(p, tuple(b)) for p, b in history if p.t < current.t] + [(current, tuple(basket))]
    # A change in the basket is a constituent change, not a price move (R1-4): it is marked as a series break.
    breaks = [domain.SeriesBreak(t=p.t, reason="basket-changed", before=list(prev_b), after=list(b))
              for (_, prev_b), (p, b) in zip(pairs, pairs[1:]) if prev_b != b]
    values = [p for p, _ in pairs]
    limitations = [*ix.limitations, f"history is FLOPX-collected since {values[0].t.date().isoformat()} ({len(values)} points), nothing earlier exists"]
    if withheld:
        limitations.append(f"{withheld} earlier archived points withheld: their sources were not recorded or include one that is no longer used")
    if breaks:
        limitations.append(f"basket changed on {breaks[-1].t.date().isoformat()}: change, momentum and forecasts read only the points since then")
    return replace(ix, values=values, limitations=limitations, basket=basket, breaks=breaks)


def _since_last_break(points: list[domain.PriceObservation], breaks: list[domain.SeriesBreak]) -> list[domain.PriceObservation]:
    """The points a comparison may read: those from the last series break on (all of them when there is none)."""
    if not breaks:
        return points
    cut = max(b.t for b in breaks)
    return [p for p in points if p.t >= cut]


def _indices(db: Session | None, data: SampleData, live_offers: list[domain.GpuOffer]) -> list[domain.FcpiIndex]:
    return [_index_for(db, data, gid, live_offers) for gid in data.gpu_models]


# -- overview ---------------------------------------------------------------------

def overview(db: Session, user: User | None) -> dict:
    """The shared composition (every reader's, from the warm snapshot) plus this reader's own
    portfolio overlay, which is never part of the shared value (caching rule: per-user overlay)."""
    payload = refresher.composed("flopx:read:overview", _overview_shared, db)
    portfolio = None
    if user is not None:
        data = _data()
        account = broker.get_or_create_account(db, user.id)
        summary = broker.summary(db, account, price_lookup_for(data, allow_live=False), theme_lookup=_theme_lookup(data))
        portfolio = {"equity_usd": summary.equity_usd, "cash_usd": summary.cash_usd, "unrealised_pnl_usd": summary.unrealised_pnl_usd,
                     "realised_pnl_usd": summary.realised_pnl_usd, "positions": len(summary.positions)}
    return {**payload, "portfolio_summary": portfolio}


def _overview_shared(db: Session | None) -> dict:
    data = _data()
    ledger = Ledger()
    provs: list[Provenance] = []

    # The five upstream stages (asks, power, closes, markets, news) are independent, so they
    # run as ONE fan-out: a cold cache costs the slowest upstream, not the sum of five stages.
    tickers = list(data.companies)
    stages = {
        "offers": _offer_calls(), "energy": _energy_calls(), "closes": _close_calls(tickers),
        "markets": _market_calls(), "news": _news_calls(with_filings=False),
    }
    flat = _gather([call for calls in stages.values() for call in calls])
    results: dict[str, list[SourceResult]] = {}
    cursor = 0
    for name, calls in stages.items():
        results[name] = flat[cursor:cursor + len(calls)]
        cursor += len(calls)

    live_offers = ledger.take(results["offers"])
    all_offers = [*live_offers, *data.offers]
    indices_all = _indices(db, data, live_offers)
    indicators = []
    for ix in indices_all:
        hist = domain.Series(id=ix.id, unit="USD/GPU-hour", points=_since_last_break(ix.values, ix.breaks))
        indicators.append({
            "id": ix.id, "label_key": f"indicator.fcpi.{ix.gpu_model_id}", "value": ix.current, "unit": "USD/GPU-hour",
            "change_pct": _round(change_pct(hist, 7)), "change_window_days": 7, "provenance": ix.provenance,
            "route": f"/gpu/{ix.gpu_model_id}",
        })
        provs.append(ix.provenance)

    energy_live = ledger.take(results["energy"])
    for sid, label, route in (("energy:DE-LU:day-ahead", "indicator.energy.de-lu-day-ahead", "/map?layer=energy&r=eu-de"),
                              ("energy:GB:market-index", "indicator.energy.gb-market-index", "/map?layer=energy&r=gb")):
        points = sorted((p for p in energy_live if p.series_id == sid), key=lambda p: p.t)
        if not points and licences.enabled(live_series()[sid][0]):
            # The source did not answer (warming, blocked, down): the newest archived slots stand in, labelled
            # archived, rather than the indicator vanishing while two years of history sit in the archive.
            points = _archived_tail(db, sid, 3)
        if points:
            s = domain.Series(id=sid, unit=points[0].unit, points=points)
            indicators.append({"id": sid, "label_key": label, "value": points[-1].value, "unit": points[0].unit,
                               "change_pct": _round(change_pct(s, 1)), "change_window_days": 1, "provenance": points[-1].provenance, "route": route})
            provs.append(points[-1].provenance)
    ercot = data.series.get("energy:ERCOT:day-ahead")
    if ercot and ercot.points:
        indicators.append({
            "id": ercot.id, "label_key": "indicator.energy.ercot-day-ahead", "value": ercot.points[-1].value, "unit": ercot.unit,
            "change_pct": _round(change_pct(ercot, 7)), "change_window_days": 7, "provenance": ercot.points[-1].provenance,
            "route": "/map?layer=energy&r=us-tx",
        })
        provs.append(ercot.points[-1].provenance)

    # The snapshot medians are cross-provider market levels: a `display-not-index` source stays out (G9-b).
    snapshot = [_snapshot_row(data, gid, model, [*_index_rows(live_offers), *data.offers]) for gid, model in data.gpu_models.items()]

    closes = _closes_into(ledger, tickers, results["closes"])
    watchlist = []
    for ticker, company in data.companies.items():
        row = _watchlist_row(data, ticker, company, closes.get(ticker))
        if row["last_close"]:
            provs.append(row["last_close"].provenance)
        watchlist.append(row)

    merged_markets = _merge_markets(data.markets, ledger.take(results["markets"]))
    markets_top = sorted(merged_markets, key=lambda m: m.volume_24h or 0, reverse=True)[:4]
    live_news = ledger.take(results["news"])
    feed_first = _merge_feed(data.feed, live_news)[:10]
    provs.extend(m.provenance for m in markets_top)
    provs.extend(i.provenance for i in feed_first)
    provs.extend(o.provenance for o in _index_rows(live_offers))

    _archive_offers_and_indices(db, ledger, [ix for ix in indices_all if effective_kind(ix.provenance) != "sample"])
    _archive(db, "markets", ledger.ok("polymarket") + ledger.ok("kalshi"), archive.observations_from_markets)
    _archive(db, "energy", ledger.ok("energy-charts") + ledger.ok("elexon") + ledger.ok("eia"), lambda rows: rows)
    _archive(db, "closes", ledger.ok("yahoo"), archive.observations_from_bars)

    return {"indicators": indicators, "gpu_snapshot": snapshot, "watchlist": watchlist, "markets": markets_top,
            "feed": feed_first, "freshness": ledger.freshness(provs)}


def _snapshot_row(data: SampleData, gid: str, model: domain.GpuModel, all_offers: list[domain.GpuOffer]) -> dict:
    """One class only: the live rows for the model when any exist, else the sample rows."""
    live_rows = [o for o in all_offers if o.gpu_model_id == gid and o.provenance.kind == "live"]
    rows = live_rows or [o for o in all_offers if o.gpu_model_id == gid and o.provenance.kind == "sample"]
    raw = [o.price_per_gpu_hour_usd for o in rows]
    adjusted = [adjust_offer(o, model, data.regions.get(o.region_id)).adjusted for o in rows]
    return {
        "gpu_model_id": gid, "offers": len(rows),
        "median_price": _round(statistics.median(raw)) if raw else None,
        "median_adjusted_price": _round(statistics.median(adjusted)) if adjusted else None,
        "min_price": min(raw) if raw else None,
        "available": sum(o.availability == "available" for o in rows),
        "limited": sum(o.availability == "limited" for o in rows),
        "unavailable": sum(o.availability == "unavailable" for o in rows),
        "regions": len({o.region_id for o in rows}),
        "provenance": derived("snapshot:median-of-asks", [o.provenance for o in rows]) if rows else data.file_provenance["offers"],
    }


def _close_calls(tickers: list[str]) -> list[Callable[[], SourceResult]]:
    """One Yahoo call per ticker (spaced to one a second by `_http`), or none when its licence row is `off`."""
    if not licences.enabled(yahoo.SOURCE):
        return []
    return [(lambda t=t: yahoo.fetch_bars(t, "5d", "1d")) for t in tickers]


def _closes_into(ledger: Ledger, tickers: list[str], results: list[SourceResult]) -> dict[str, SourceResult]:
    # One ledger entry for Yahoo rather than eight: the reason of the first failure is kept.
    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    if ok:
        ledger.results.extend(ok)
    if failed and not ok:
        ledger.take([failed[0]])
    elif failed:
        ledger.note_unavailable({"source": yahoo.SOURCE, "reason": f"{failed[0].reason} for {len(failed)} of {len(tickers)} tickers"})
    return dict(zip(tickers, results))


def _live_closes(data: SampleData, ledger: Ledger) -> dict[str, SourceResult]:
    tickers = list(data.companies)
    return _closes_into(ledger, tickers, _gather(_close_calls(tickers)))


def _watchlist_row(data: SampleData, ticker: str, company: domain.Company, result: SourceResult | None) -> dict:
    if result is not None and result.ok and result.records:
        bars = result.records
        last = domain.PriceObservation(series_id=f"equity:{ticker}:close", t=bars[-1].t, value=bars[-1].close, unit="USD", provenance=bars[-1].provenance)
        change = _round((bars[-1].close / bars[-2].close - 1.0) * 100.0) if len(bars) > 1 and bars[-2].close else None
        return {"ticker": ticker, "name": company.name, "last_close": last, "change_pct": change}
    s = data.series.get(f"equity:{ticker}:close")
    return {"ticker": ticker, "name": company.name, "last_close": last_point(s) if s else None, "change_pct": _round(change_pct(s, 1)) if s else None}


# -- GPU market -------------------------------------------------------------------

def _filter_offers(rows: list[domain.GpuOffer], gpu, region, provider, contract) -> list[domain.GpuOffer]:
    if gpu:
        rows = [o for o in rows if o.gpu_model_id == gpu]
    if region:
        wanted = set(region.split(","))
        rows = [o for o in rows if o.region_id in wanted]
    if provider:
        rows = [o for o in rows if o.provider_id == provider]
    if contract:
        rows = [o for o in rows if o.contract == contract]
    return rows


@_snapshotted
def offers(db: Session | None, gpu: str | None, region: str | None, provider: str | None, contract: str | None, sort: str | None) -> dict:
    data = _data()
    ledger = Ledger()
    live_rows = _live_offers(ledger)
    rows = _filter_offers([*live_rows, *data.offers], gpu, region, provider, contract)
    enriched = []
    for o in rows:
        adj = adjust_offer(o, data.gpu_models[o.gpu_model_id], data.regions.get(o.region_id))
        enriched.append({**o.__dict__, "adjustment": adj})
    key = {
        "adjusted": lambda r: r["adjustment"].adjusted,
        "quoted": lambda r: r["price_per_gpu_hour_usd"],
        "reliability": lambda r: -(r["reliability_score"] or 0),
        "count": lambda r: -r["gpu_count"],
    }.get(sort or "adjusted", lambda r: r["adjustment"].adjusted)
    enriched.sort(key=key)
    _archive_offers_and_indices(db, ledger, [])
    return {"offers": enriched, "freshness": ledger.freshness(o.provenance for o in rows)}


HEATMAP_MEASURES = {
    "median-adjusted-price": "median comparable price (USD/GPU-hour) of the cell's offers",
    "median-raw-price": "median quoted price (USD/GPU-hour) of the cell's offers",
    "min-price": "lowest quoted price (USD/GPU-hour) in the cell",
    "offer-count": "number of offers in the cell",
    "available-share": "share of the cell's offers flagged available",
}


def _cell_value(measure: str, rows: list[domain.GpuOffer], model: domain.GpuModel, data: SampleData) -> float:
    if measure == "median-adjusted-price":
        return statistics.median(adjust_offer(o, model, data.regions.get(o.region_id)).adjusted for o in rows)
    if measure == "median-raw-price":
        return statistics.median(o.price_per_gpu_hour_usd for o in rows)
    if measure == "min-price":
        return min(o.price_per_gpu_hour_usd for o in rows)
    if measure == "offer-count":
        return float(len(rows))
    return sum(o.availability == "available" for o in rows) / len(rows)


@_snapshotted
def heatmap(db: Session | None, measure: str, axis: str) -> dict:
    data = _data()
    if measure not in HEATMAP_MEASURES:
        raise UnknownEntityError(entity="measure", id=measure, allowed=sorted(HEATMAP_MEASURES))
    ledger = Ledger()
    # A heatmap cell is a cross-provider median with no row beside it to carry a caveat, so a
    # `display-not-index` source (Vast.ai, a market reading only, G9-b) does not enter it.
    live_rows = _index_rows(_live_offers(ledger))
    all_rows = [*live_rows, *data.offers]
    attr = "region_id" if axis == "region" else "provider_id"
    base_keys = sorted(data.regions) if axis == "region" else sorted(data.providers)
    extra_keys = sorted({getattr(o, attr) for o in live_rows} - set(base_keys))
    keys = base_keys + extra_keys
    cells, values = [], []
    kinds_used: set[str] = set()
    for gid, model in data.gpu_models.items():
        for key in keys:
            in_cell = [o for o in all_rows if o.gpu_model_id == gid and getattr(o, attr) == key]
            # One class per cell: live rows when any exist, else sample rows, never both.
            live_cell = [o for o in in_cell if o.provenance.kind == "live"]
            rows = live_cell or [o for o in in_cell if o.provenance.kind == "sample"]
            kind = "live" if live_cell else ("sample" if rows else None)
            value = _cell_value(measure, rows, model, data) if rows else None
            if value is not None:
                values.append(value)
                kinds_used.add(kind)
            cells.append({"gpu_model_id": gid, ("region_id" if axis == "region" else "provider_id"): key, "value": _round(value), "n": len(rows), "kind": kind})
    legend = {"min": _round(min(values)) if values else None, "max": _round(max(values)) if values else None,
              "unit": "count" if measure == "offer-count" else ("share" if measure == "available-share" else "USD/GPU-hour"),
              "kind": "mixed-per-cell" if len(kinds_used) > 1 else (next(iter(kinds_used)) if kinds_used else "sample"),
              "liveCells": sum(c["kind"] == "live" for c in cells), "sampleCells": sum(c["kind"] == "sample" for c in cells)}
    _archive_offers_and_indices(db, ledger, [])
    return {"measure": measure, "measure_definition": HEATMAP_MEASURES[measure], "axis": axis, "cells": cells, "legend": legend,
            "freshness": ledger.freshness(o.provenance for o in all_rows)}


def _window(points: list[domain.PriceObservation], days: int) -> list[domain.PriceObservation]:
    if not points:
        return points
    cutoff = points[-1].t - timedelta(days=days)
    return [p for p in points if p.t >= cutoff]


# -- the research archive (decision G13) ------------------------------------------------------------------
# Real closes the archive holds (the founder's WRDS CRSP extract, source `wrds-crsp`, licence use
# `research-archive`) replace the authored equity series wherever an engine reads one: the derived
# results are public, the raw points reach signed-in accounts only. `archive.py` keeps the gate.

AUDIENCE_PUBLIC = "public"
AUDIENCE_MEMBER = "member"


def _audience(user: User | None) -> str:
    return AUDIENCE_MEMBER if user is not None else AUDIENCE_PUBLIC


def _observed_series(db: Session | None, series_id: str) -> domain.Series | None:
    """The whole archived series when the archive holds real observations under `series_id` (gated source included:
    the caller decides who sees it); None without a database, without rows, or on a read failure. Inside a
    refresher sweep an equity series is served from the sweep's one read of every observed equity series
    (`_observed_equity`), so the evaluation's per-ticker and portfolio reads share one pass over the archive (B-MEM2)."""
    if db is None:
        return None
    if series_id.startswith("equity:") and refresher.sweep_memo() is not None:
        return _observed_equity(db, _data()).get(series_id)
    try:
        return archive.archived_series(db, series_id)
    except Exception:
        logger.warning("archived %s unavailable", series_id, exc_info=True)
        return None


def _observed_equity(db: Session | None, data: SampleData) -> dict[str, domain.Series]:
    """Every equity series the archive holds real observations for, whole, for the engines; empty without a
    database or on a read failure, so an engine falls back to the authored bag it always had. One read per
    refresher sweep and session (`refresher.sweep_memo`); outside a sweep every call reads the archive."""
    if db is None:
        return {}
    memo = refresher.sweep_memo()
    key = ("observed_equity", id(db))
    if memo is not None and key in memo:
        return memo[key]
    try:
        held = archive.observed_series(db, [sid for sid in data.series if sid.startswith("equity:")])
    except Exception:
        logger.warning("archived equity closes unavailable", exc_info=True)
        return {}
    if memo is not None:
        memo[key] = held
    return held


def series(db: Session, series_id: str, range_: str | None, user: User | None = None) -> dict:
    """A series for its reader. An anonymous reader gets the public view (sample, live and public archived points,
    as before); a signed-in account also receives a `research-archive` source's points, which for an equity series
    with real observations means the observed series itself in place of the authored one."""
    return _series_view(db, series_id, range_, _audience(user))


@_snapshotted
def _series_view(db: Session, series_id: str, range_: str | None, audience: str) -> dict:
    days = parse_range(range_, default_days=180)
    data = _data()
    ledger = Ledger()
    if series_id.startswith("index:fcpi:"):
        live_rows = _live_offers(ledger)
        ix = _index_for(db, data, series_id.split(":", 2)[2], live_rows)
        points = _window(ix.values, days)
        _archive_offers_and_indices(db, ledger, [ix] if effective_kind(ix.provenance) != "sample" else [])
        provs = [p.provenance for p in points] + [o.provenance for o in _index_rows(live_rows) if o.gpu_model_id == ix.gpu_model_id]
        first = points[0].t if points else None
        breaks = [b for b in ix.breaks if first is not None and b.t > first]
        return {"series_id": series_id, "unit": "USD/GPU-hour", "points": points, "breaks": breaks, "freshness": ledger.freshness(provs)}
    if series_id in live_series() and series_id not in data.series:
        return _live_series(db, ledger, series_id, days)
    s = _extended_series(db, data, series_id, days, audience=audience)
    return {"series_id": s.id, "unit": s.unit, "points": s.points, "freshness": ledger.freshness(p.provenance for p in s.points)}


# Series that exist only live (no sample twin): the id the overview publishes resolves here, live
# plus what the archive kept, and a failed upstream yields an empty series that names it (V6 #8),
# never a bare 404.
LIVE_SERIES = {
    "energy:DE-LU:day-ahead": (energy.ENERGY_CHARTS, lambda: energy.fetch_energy_charts(), "EUR/MWh"),   # resolved per call (test seam)
    "energy:GB:market-index": (energy.ELEXON, lambda: energy.fetch_elexon(), "GBP/MWh"),
}
# The EIA series (N-EIA: ERCOT and PJM hourly demand and day-ahead demand forecast, MWh, never a price) exist only
# while EIA_API_KEY is set; `live_series()` is the table every read consults, so without the key these ids are
# unknown series, exactly as before the adapter existed.
EIA_SERIES = {sid: (eia.SOURCE, (lambda z=eia.zone_of(sid)[0]: eia.fetch_zone(z)), eia.UNIT) for sid in eia.SERIES_IDS}


def live_series() -> dict:
    return {**LIVE_SERIES, **EIA_SERIES} if eia.configured() else LIVE_SERIES


# How far back the inferred-state inputs read an archived power series (N-DATA): the history backfill
# (tools/history_backfill.py) holds two years of 15-minute to half-hourly slots, but the engines that
# read these inputs want the trailing 30-day median (power-stress) and the 30-day change (infra energy),
# and reading the whole DE-LU window measured 2.2 s per sweep on one local core against 0.17 s for 70
# days, so the sweep reads this tail; the series read exposes any range. The evaluation harness's
# 30-day energy change therefore has data only for origins inside this window.
ENERGY_HISTORY_DAYS = 120
# A power series is read as bucket means, never slot by slot (B-MEM2): the inferred inputs held 17,000
# quarter-hour and half-hour slots per sweep (a measured share of the worker's memory peak), and the frontend
# draws at most 90 days hourly or two years daily. The archive aggregates in SQL (`archive.archived_series`
# with `bucket`), the live slots of the same sweep are aggregated here the same way, and every mean point
# says so in its note; the raw slots stay in the archive.
ENERGY_INPUT_BUCKET = "hour"
ENERGY_HOURLY_MAX_DAYS = 90


def _energy_bucket(days: int) -> str:
    return "hour" if days <= ENERGY_HOURLY_MAX_DAYS else "day"


def _bucketed(points: list[domain.PriceObservation], bucket: str) -> list[domain.PriceObservation]:
    """Slots aggregated to one mean point per `bucket` ("hour" or "day"), dated at the bucket start and carrying the
    newest slot's provenance with the aggregation named in its note, as `archive._bucketed_series` labels its
    rows. Every slot of a series read comes from one source and class, so the mean is over one provenance class."""
    groups: dict[datetime, list[domain.PriceObservation]] = {}
    for p in points:
        start = p.t.replace(minute=0, second=0, microsecond=0) if bucket == "hour" else p.t.replace(hour=0, minute=0, second=0, microsecond=0)
        groups.setdefault(start, []).append(p)
    out = []
    for start, rows in sorted(groups.items()):
        newest = max(rows, key=lambda p: p.t)
        slots = f"{archive.BUCKETS[bucket][0]} of {len(rows)} slot{'s' if len(rows) != 1 else ''}"
        prov = replace(newest.provenance, note=f"{slots} (one slot's note: {newest.provenance.note})" if newest.provenance.note else slots)
        out.append(domain.PriceObservation(series_id=newest.series_id, t=start, value=statistics.fmean(p.value for p in rows), unit=newest.unit, provenance=prov))
    return out


def _archived_tail(db: Session | None, series_id: str, days: int, *, bucket: str | None = None) -> list[domain.PriceObservation]:
    """The servable archived points of `series_id` from `days` ago on, oldest first (one mean point per `bucket` when
    given); empty without a database or on a read failure."""
    if db is None:
        return []
    try:
        stored = archive.archived_series(db, series_id, since=utcnow() - timedelta(days=days), bucket=bucket)
    except Exception:
        logger.warning("archived %s skipped", series_id, exc_info=True)
        return []
    return stored.points if stored else []


def _live_series(db: Session | None, ledger: Ledger, series_id: str, days: int) -> dict:
    source, call, unit = live_series()[series_id]
    if not licences.enabled(source):
        raise UnknownSeriesError(series_id=series_id)
    bucket = _energy_bucket(days)
    live_points = _bucketed(sorted((p for p in ledger.take(_gather([call])) if p.series_id == series_id), key=lambda p: p.t), bucket)
    _archive(db, "energy", ledger.ok(source), lambda rows: rows)
    # The archive is read for the requested range only (plus the day the range's cutoff falls in), as hourly or
    # daily means: the backfilled history runs to two years and the `_window` below keeps `days` from the last point anyway.
    prior = _archived_tail(db, series_id, days + 1, bucket=bucket)
    known = {p.t for p in live_points}
    points = _window(sorted([*[p for p in prior if p.t not in known], *live_points], key=lambda p: p.t), days)
    return {"series_id": series_id, "unit": points[0].unit if points else unit, "points": points,
            "freshness": ledger.freshness(p.provenance for p in points)}


def _extended_series(db: Session | None, data: SampleData, series_id: str, days: int, *, audience: str = AUDIENCE_PUBLIC) -> domain.Series:
    """A sample series plus archived points newer than it, or an archived-only series; UnknownSeriesError otherwise.
    The public view (the default, every lab read) never carries a `research-archive` point. A signed-in reader of
    an equity series the archive holds real observations for gets that series whole in place of the authored one:
    appending authored 2026 values to real history would present fiction as its continuation."""
    public_only = audience != AUDIENCE_MEMBER
    sample = data.series.get(series_id)
    if sample is not None:
        if not public_only and series_id.startswith("equity:"):
            observed = _observed_series(db, series_id)
            if observed is not None:
                return domain.Series(id=observed.id, unit=observed.unit, points=_window(observed.points, days))
        extended = archive.extend(domain.Series(id=sample.id, unit=sample.unit, points=sample.points), db, public_only=public_only)
        return domain.Series(id=extended.id, unit=extended.unit, points=_window(extended.points, days))
    return resolve_series(db, series_id, days, public_only=public_only)


@_snapshotted
def indices(db: Session | None) -> dict:
    data = _data()
    ledger = Ledger()
    live_rows = _live_offers(ledger)
    rows = _indices(db, data, live_rows)
    _archive_offers_and_indices(db, ledger, [ix for ix in rows if effective_kind(ix.provenance) != "sample"])
    # The freshness names the live asks behind the live indices, not only the derivation method:
    provs = [ix.provenance for ix in rows] + [o.provenance for o in _index_rows(live_rows)]
    return {"indices": rows, "freshness": ledger.freshness(provs)}


@_snapshotted
def index(db: Session | None, gpu_model_id: str) -> dict:
    data = _data()
    ledger = Ledger()
    live_rows = _live_offers(ledger)
    ix = _index_for(db, data, gpu_model_id, live_rows)
    _archive_offers_and_indices(db, ledger, [ix] if effective_kind(ix.provenance) != "sample" else [])
    # Same freshness block as the list read, scoped to this model: the asks behind a live index,
    # the index itself, and every marketplace that was asked and did not answer.
    provs = [ix.provenance] + ([o.provenance for o in _index_rows(live_rows) if o.gpu_model_id == gpu_model_id] if effective_kind(ix.provenance) != "sample" else [])
    return {**ix.__dict__, "freshness": ledger.freshness(provs)}


def _previous_live_offers(db: Session | None, live_rows: list[domain.GpuOffer]) -> list[domain.GpuOffer]:
    """Each live offer as the archive last saw it: the newest archived point at or before this
    observation. An offer whose only archived point IS this observation is unchanged (the same
    fetch served from cache is not news); an offer the archive has never seen is left out, so the
    detector reports it as `new`. Known limitation: a live offer that disappeared is not reported,
    because the archive keeps numeric observations and cannot rebuild the offer it belonged to."""
    if db is None or not live_rows:
        return []
    newest = max(o.provenance.observed_at for o in live_rows)
    try:
        prices = archive.latest_points(db, (f"offer:{o.id}:price" for o in live_rows), newest)
        availability = archive.latest_points(db, (f"offer:{o.id}:available" for o in live_rows), newest)
    except Exception:
        logger.warning("archive read for changes skipped", exc_info=True)
        return []
    levels = {1.0: "available", 0.5: "limited", 0.0: "unavailable"}
    before: list[domain.GpuOffer] = []
    for o in live_rows:
        prior = prices.get(f"offer:{o.id}:price")
        if prior is None:
            continue
        if prior.t >= o.provenance.observed_at:
            before.append(o)
            continue
        avail = availability.get(f"offer:{o.id}:available")
        availability_then = levels.get(avail.value, o.availability) if avail is not None and avail.t < o.provenance.observed_at else o.availability
        before.append(replace(o, price_per_gpu_hour_usd=prior.value, availability=availability_then, provenance=prior.provenance))
    return before


@_snapshotted
def changes(db: Session | None, since: datetime | None) -> dict:
    data = _data()
    ledger = Ledger()
    # A move can only be detected against what the archive saw before, so a source that is never
    # archived (Vast.ai, `display-not-index`) would read as all-new on every call; it is left out.
    live_rows = [o for o in _live_offers(ledger) if licences.archivable(o.provenance.source)]
    # Sample snapshot pair, as authored:
    before = data.offers_previous
    until = max(o.provenance.observed_at for o in data.offers)
    snapshot_at = max(o.provenance.observed_at for o in before)
    if since is not None and since > snapshot_at:
        before = data.offers
    detected = change_detection.detect_changes(before, data.offers)
    # Live snapshot against what the archive last saw, one class on each side:
    live_before = _previous_live_offers(db, live_rows)
    if live_rows:
        live_detected = change_detection.detect_changes(live_before, live_rows)
        if since is not None:
            live_detected = [c for c in live_detected if (c.after or c.before or {}).get("observedAt", "") >= since.isoformat()]
        detected = [*live_detected, *detected]
    _archive_offers_and_indices(db, ledger, [])
    # The window ends at the newest snapshot a reported value comes from, never at the fetch:
    # seven authored moves must not read as "detected up to a minute ago" (V1 F12).
    shown = [c.provenance for c in detected if c.provenance is not None]
    if shown:
        until = max(p.observed_at for p in shown)
    # Freshness names only what the rows carry: each change's snapshot and the "before" side it was compared with.
    by_id = {o.id: o for o in [*before, *live_before]}
    provs = shown + [by_id[c.offer_id].provenance for c in detected if c.kind in ("price", "availability") and c.offer_id in by_id]
    return {"since": since or snapshot_at, "until": until, "changes": detected, "freshness": ledger.freshness(provs)}


# -- map ----------------------------------------------------------------------------

@_snapshotted
def facilities(status: str | None, region: str | None) -> dict:
    data = _data()
    ledger = Ledger()
    rows = [*data.facilities, *ledger.take(_gather(_facility_calls()))]
    if status:
        wanted = set(status.split(","))
        rows = [f for f in rows if f.status in wanted]
    if region:
        wanted = set(region.split(","))
        rows = [f for f in rows if f.region_id in wanted]
    return {"facilities": rows, "freshness": ledger.freshness(f.provenance for f in rows)}


def _energy_indicators(data: SampleData, ledger: Ledger) -> dict[str, domain.PriceObservation]:
    """The latest point per grid zone's day-ahead series, from the same reads the overview shows (R1-11): the live
    series where its source answered (Energy-Charts for DE-LU), the authored sample otherwise (ERCOT). Keyed by
    series id `energy:<zone>:day-ahead`; a zone with neither has no entry, and a failed live source is named in
    the ledger's unavailable list rather than replaced."""
    out: dict[str, domain.PriceObservation] = {}
    for sid, s in data.series.items():
        if sid.startswith("energy:") and sid.endswith(":day-ahead") and s.points:
            out[sid] = s.points[-1]
    live = sorted(ledger.take(_gather(_energy_calls())), key=lambda p: p.t)
    for p in live:
        if p.series_id.endswith(":day-ahead"):
            out[p.series_id] = p   # ascending, so the newest live point wins over the sample one
    return out


@_snapshotted
def regions() -> dict:
    data = _data()
    ledger = Ledger()
    indicators = _energy_indicators(data, ledger)
    rollups = []
    for rid, region in data.regions.items():
        rows = [o for o in data.offers if o.region_id == rid]
        raw = [o.price_per_gpu_hour_usd for o in rows]
        adjusted = [adjust_offer(o, data.gpu_models[o.gpu_model_id], region).adjusted for o in rows]
        by_status: dict[str, int] = {}
        for f in data.facilities:
            if f.region_id == rid:
                by_status[f.status] = by_status.get(f.status, 0) + 1
        rollups.append({
            "region": region, "offers": len(rows),
            "median_raw_price": _round(statistics.median(raw)) if raw else None,
            "median_adjusted_price": _round(statistics.median(adjusted)) if adjusted else None,
            "facilities_by_status": by_status,
            "energy_indicator": indicators.get(f"energy:{region.grid_zone}:day-ahead") if region.grid_zone else None,
        })
    provs = [o.provenance for o in data.offers] + [f.provenance for f in data.facilities]
    provs += [r["energy_indicator"].provenance for r in rollups if r["energy_indicator"] is not None]
    return {"regions": rollups, "freshness": ledger.freshness(provs)}


# -- companies --------------------------------------------------------------------

def _sample_close(data: SampleData, ticker: str) -> domain.PriceObservation | None:
    s = data.series.get(f"equity:{ticker}:close")
    return last_point(s) if s else None


def _archived_fundamentals(db: Session | None, tickers: Iterable[str]) -> dict[str, list[domain.Fundamental]]:
    """Every company fundamental the archive holds for these tickers (the founder's Compustat extract, source
    `wrds-compustat`, gated at the payload, not here), keyed by ticker; empty without a database or on a read failure,
    so a company read falls back to the SEC rows and the authored sample it always had."""
    if db is None:
        return {}
    ids = [f"{fundamentals_engine.SERIES_PREFIX}{t}:{suffix}" for t in tickers for suffix in fundamentals_engine.CONCEPT_BY_SUFFIX]
    try:
        held = archive.observed_series(db, ids)
    except Exception:
        logger.warning("archived fundamentals unavailable", exc_info=True)
        return {}
    out: dict[str, list[domain.Fundamental]] = {}
    for f in fundamentals_engine.from_archive(held.values()):
        out.setdefault(f.ticker, []).append(f)
    return out


def _merged_fundamentals(live_rows: list[domain.Fundamental], archived_rows: list[domain.Fundamental], sample_rows: list[domain.Fundamental]) -> tuple[list[domain.Fundamental], dict]:
    """The observed rows under the merge rule (live SEC facts win where both report a period; Compustat supplies the
    rest) and the summary derived from them, or from the authored sample when nothing was observed. The summary's
    figures are public; which raw rows a reader receives is the caller's decision by audience."""
    observed = fundamentals_engine.merge(live_rows, archived_rows)
    return observed, fundamentals_engine.summarise(observed or sample_rows)


@_snapshotted
def companies(db: Session | None) -> dict:
    data = _data()
    ledger = Ledger()
    closes = _live_closes(data, ledger)
    frame_rows = ledger.take(_gather([sec.fetch_frame]))
    by_ticker: dict[str, list[domain.Fundamental]] = {}
    for f in frame_rows:
        by_ticker.setdefault(f.ticker, []).append(f)
    archived = _archived_fundamentals(db, data.companies)
    rows, provs = [], []
    for ticker, c in data.companies.items():
        result = closes.get(ticker)
        close = (domain.PriceObservation(series_id=f"equity:{ticker}:close", t=result.records[-1].t, value=result.records[-1].close, unit="USD", provenance=result.records[-1].provenance)
                 if result is not None and result.ok and result.records else _sample_close(data, ticker))
        # The list is a public read: its raw rows are the SEC frame (or the authored sample), never a Compustat row;
        # the derived summary is public and may draw on the archive.
        company_row = replace(c, fundamentals=by_ticker[ticker]) if ticker in by_ticker else c
        observed, summary = _merged_fundamentals(by_ticker.get(ticker, []), archived.get(ticker, []), c.fundamentals)
        related = sum(ticker in i.entities.tickers for i in data.feed)
        rows.append({"company": company_row, "latest_close": close, "related_item_count": related, "fundamentals_summary": summary})
        provs.append(c.provenance)
        provs.extend(f.provenance for f in company_row.fundamentals)
        provs.extend(f.provenance for f in observed)
        if close:
            provs.append(close.provenance)
    _archive(db, "closes", ledger.ok("yahoo"), archive.observations_from_bars)
    _archive(db, "fundamentals", ledger.ok("sec-edgar"), archive.observations_from_fundamentals)
    return {"companies": rows, "freshness": ledger.freshness(provs)}


def _archived_close(db: Session | None, ticker: str, audience: str) -> domain.PriceObservation | None:
    """The newest archived close for this reader when no live bar answered: a `research-archive` point only for a
    signed-in account. Today no public source archives closes (Yahoo is display-only), so the public view is sample."""
    if db is None:
        return None
    try:
        return archive.latest_before(db, f"equity:{ticker}:close", utcnow(), public_only=audience != AUDIENCE_MEMBER)
    except Exception:
        logger.warning("archived close for %s unavailable", ticker, exc_info=True)
        return None


def company(db: Session | None, ticker: str, user: User | None = None) -> dict:
    """The company detail for its reader: the latest close is the live bar when Yahoo answered, else the newest
    archived close the reader may see (the WRDS CRSP close for a signed-in account), else the authored sample close.
    Fundamentals follow the same gate: every reader gets the live SEC rows (or the authored sample) and the derived
    summary; a signed-in account also gets the archived Compustat rows merged under the rule in `fundamentals.py`."""
    return _company_view(db, ticker, _audience(user))


@_snapshotted
def _company_view(db: Session | None, ticker: str, audience: str) -> dict:
    data = _data()
    ticker = ticker.upper()
    c = data.companies.get(ticker)
    if c is None:
        raise UnknownEntityError(entity="company", id=ticker)
    ledger = Ledger()
    calls: list[Callable[[], SourceResult]] = [lambda: yahoo.fetch_bars(ticker, "5d", "1d")]
    if c.cik:
        calls += [lambda: sec.fetch_fundamentals(c.cik, ticker), lambda: sec.fetch_filings(c.cik, ticker)]
    results = _gather(calls)
    bars_result = results[0]
    ledger.take(results)
    latest_bar = bars_result.records[-1] if bars_result.ok and bars_result.records else None
    close = (domain.PriceObservation(series_id=f"equity:{ticker}:close", t=latest_bar.t, value=latest_bar.close, unit="USD", provenance=latest_bar.provenance)
             if latest_bar else (_archived_close(db, ticker, audience) or _sample_close(data, ticker)))
    live_fundamentals = [f for r in results[1:2] if r.ok for f in r.records] if c.cik else []
    observed, summary = _merged_fundamentals(live_fundamentals, _archived_fundamentals(db, [ticker]).get(ticker, []), c.fundamentals)
    # Raw rows by audience: a signed-in account reads the merged observed rows (SEC where it answered, Compustat for
    # the rest); an anonymous reader gets the SEC rows or the authored sample, exactly as before, never a Compustat
    # row. The summary's derived figures are public for both and may rest on the archived rows.
    fundamentals = (observed if audience == AUDIENCE_MEMBER else live_fundamentals) or c.fundamentals
    filings = [i for r in results[2:3] if r.ok for i in r.records] if c.cik else []
    related_items = _merge_feed([i for i in data.feed if ticker in i.entities.tickers], filings)
    related_facilities = [f for f in data.facilities if f.id in set(c.related_facility_ids)]
    provs = ([c.provenance] + [i.provenance for i in related_items] + [f.provenance for f in fundamentals] + [f.provenance for f in observed]
             + ([close.provenance] if close else []))
    _archive(db, "closes", [bars_result], archive.observations_from_bars)
    _archive(db, "fundamentals", results[1:2] if c.cik else [], archive.observations_from_fundamentals)
    return {"company": c, "latest_bar": latest_bar, "latest_close": close, "fundamentals": fundamentals, "fundamentals_summary": summary,
            "related_items": related_items, "related_facilities": related_facilities,
            "unavailable": bars_result.unavailable, "freshness": ledger.freshness(provs)}


@_snapshotted
def bars(db: Session | None, ticker: str, range_: str, interval: str) -> dict:
    data = _data()
    ticker = ticker.upper()
    if ticker not in data.companies:
        raise UnknownEntityError(entity="company", id=ticker)
    result = yahoo.fetch_bars(ticker, range_, interval)
    records = result.records if result.ok else []
    if interval == "1d":
        _archive(db, "closes", [result], archive.observations_from_bars)
    return {"ticker": ticker, "interval": interval, "range": range_, "bars": records, "unavailable": result.unavailable,
            "sample_series_id": f"equity:{ticker}:close" if f"equity:{ticker}:close" in data.series else None,
            "freshness": freshness((b.provenance for b in records), [result.unavailable] if result.unavailable else [])}


# -- markets and feed ----------------------------------------------------------------

def _merge_markets(sample_rows: list[domain.PredictionMarket], live_rows: list[domain.PredictionMarket]) -> list[domain.PredictionMarket]:
    """Live replaces a sample market with the same id; everything else is appended, live first."""
    live_ids = {m.id for m in live_rows}
    return [*live_rows, *[m for m in sample_rows if m.id not in live_ids]]


def _merge_feed(sample_rows: list[domain.IntelItem], live_rows: list[domain.IntelItem]) -> list[domain.IntelItem]:
    live_ids = {i.id for i in live_rows}
    merged = [*live_rows, *[i for i in sample_rows if i.id not in live_ids]]
    merged.sort(key=lambda i: i.published_at, reverse=True)
    return merged


@_snapshotted
def markets(db: Session | None) -> dict:
    data = _data()
    ledger = Ledger()
    rows = _merge_markets(data.markets, _live_markets(ledger))
    _archive(db, "markets", ledger.ok("polymarket") + ledger.ok("kalshi"), archive.observations_from_markets)
    return {"markets": rows, "freshness": ledger.freshness(m.provenance for m in rows)}


@_snapshotted
def market(db: Session | None, market_id: str) -> domain.PredictionMarket:
    data = _data()
    if market_id.startswith(polymarket.ID_PREFIX) or market_id.startswith(kalshi.ID_PREFIX):
        ledger = Ledger()
        live_rows = _live_markets(ledger)
        found = next((m for m in live_rows if m.id == market_id), None)
        if found is None:
            raise UnknownEntityError(entity="market", id=market_id, unavailable=ledger.unavailable)
        history = polymarket.fetch_market_history(market_id) if market_id.startswith(polymarket.ID_PREFIX) else kalshi.fetch_market_history(market_id)
        points = history.records if history.ok else []
        if db is not None:
            try:
                archived = archive.archived_series(db, f"market:{market_id}:yes")
                known = {p.t for p in points}
                points = sorted([*points, *[p for p in (archived.points if archived else []) if p.t not in known]], key=lambda p: p.t)
            except Exception:
                logger.warning("archived market history skipped", exc_info=True)
        _archive(db, "markets", [r for r in ledger.results if r.ok], archive.observations_from_markets)
        return replace(found, history=points)
    m = data.markets_by_id.get(market_id)
    if m is None:
        raise UnknownEntityError(entity="market", id=market_id)
    return m


@_snapshotted
def feed(db: Session | None, topic: str | None, ticker: str | None, region: str | None, severity: int | None, since: datetime | None, limit: int = 50) -> dict:
    data = _data()
    ledger = Ledger()
    rows = _merge_feed(data.feed, _live_news(ledger, with_filings=True))
    if topic:
        wanted = set(topic.split(","))
        rows = [i for i in rows if i.kind in wanted]
    if ticker:
        rows = [i for i in rows if ticker.upper() in i.entities.tickers]
    if region:
        wanted = set(region.split(","))
        rows = [i for i in rows if wanted.intersection(i.geography.region_ids)]
    if severity:
        rows = [i for i in rows if i.severity >= severity]
    if since:
        rows = [i for i in rows if i.published_at >= since]
    rows = rows[:limit]
    return {"items": rows, "freshness": ledger.freshness(i.provenance for i in rows)}


# -- lab ---------------------------------------------------------------------------------

@_snapshotted
def xray(db: Session, target: str, overlay: str | None, range_: str | None) -> dict:
    data = _data()
    ledger = Ledger()
    days = parse_range(range_, default_days=180)
    target_series = _lab_series(db, data, target, days, ledger)
    overlays = [_lab_series(db, data, oid, days, ledger) for oid in (overlay.split(",") if overlay else []) if oid]
    result = xray_lab.xray(target_series, overlays, data.feed)
    provs = [p.provenance for s in [target_series, *overlays] for p in s.points]
    return {**result.__dict__, "freshness": ledger.freshness(provs)}


def _lab_series_with_break(db: Session, data: SampleData, series_id: str, days: int, ledger: Ledger) -> tuple[domain.Series, domain.SeriesBreak | None]:
    """The lab's series and, for an index whose basket changed, the break its points start at: a forecast,
    scenario or X-Ray never reads across a basket change (R1-4)."""
    if series_id.startswith("index:fcpi:"):
        ix = _index_for(db, data, series_id.split(":", 2)[2], _live_offers(ledger))
        return domain.Series(id=ix.id, unit="USD/GPU-hour", points=_since_last_break(ix.values, ix.breaks)[-days:]), (ix.breaks[-1] if ix.breaks else None)
    return _extended_series(db, data, series_id, days), None


def _lab_series(db: Session, data: SampleData, series_id: str, days: int, ledger: Ledger | None = None) -> domain.Series:
    return _lab_series_with_break(db, data, series_id, days, ledger or Ledger())[0]


def forecast(db: Session, series_id: str, horizon: int, level: float) -> dict:
    data = _data()
    ledger = Ledger()
    s, history_break = _lab_series_with_break(db, data, series_id, 10_000, ledger)
    y = [p.value for p in s.points]
    result = forecasting.forecast(y, horizon, level=level)
    step = (s.points[-1].t - s.points[-2].t) if len(s.points) > 1 else timedelta(days=1)
    baselines = []
    for b in result.baselines:
        baselines.append({"method": b.method, "residual_std": b.residual_std, "parameters": b.parameters,
                          "points": [{"step": k, "t": s.points[-1].t + step * k, "mean": m, "lower": lo, "upper": hi}
                                     for k, (m, lo, hi) in enumerate(zip(b.mean, b.lower, b.upper), start=1)]})
    prov = derived("forecast:baselines-v1", [p.provenance for p in s.points], note="in-sample residual intervals; rolling-origin backtest vs naive")
    reasons = exploratory_reasons(result.caveats, [p.provenance for p in s.points])
    return {"series_id": s.id, "horizon": horizon, "level": level, "history_points": result.history_points, "baselines": baselines,
            "backtest": [sc.__dict__ for sc in result.backtest], "chosen": result.chosen, "caveats": result.caveats,
            "exploratory": bool(reasons), "exploratory_reasons": reasons, "history_break": history_break,
            "provenance": prov, "freshness": ledger.freshness(p.provenance for p in s.points)}


# A forecast is EXPLORATORY (architecture §10.1; refinement brief: "a forecast based on
# limited collected history must be marked exploratory") when its history is thin for the
# horizon, when the rolling-origin backtest could not run, or when no input was observed.
EXPLORATORY_CAVEATS = ("thin-history-relative-to-horizon", "backtest-skipped:too-few-origins")


def exploratory_reasons(caveats: list[str], inputs) -> list[str]:
    reasons = [c for c in caveats if c in EXPLORATORY_CAVEATS]
    kinds = {getattr(p, "kind", None) for p in inputs}
    if kinds and kinds <= {"sample"}:
        reasons.append("sample-inputs-only")
    return reasons


def scenario(db: Session, target: str, assumptions: dict[str, float], horizon: int) -> dict:
    data = _data()
    ledger = Ledger()
    s = _lab_series(db, data, target, 10_000, ledger)
    step = (s.points[-1].t - s.points[-2].t) if len(s.points) > 1 else timedelta(days=1)
    result = scenarios.project(s.id, [(p.t, p.value) for p in s.points], assumptions, horizon, step=step)
    prov = derived("scenario:elasticities-v1", [p.provenance for p in s.points], note="demonstrative; authored elasticities")
    return {**result.__dict__, "provenance": prov, "freshness": ledger.freshness(p.provenance for p in s.points)}


FACTORS = [
    {"id": "fcpi-level", "name": "FCPI level", "definition": "median comparable ask per GPU model (indices.py)", "inputs": ["gpu offers"], "unit": "USD/GPU-hour"},
    {"id": "fcpi-change-20d", "name": "FCPI 20-day change", "definition": "ratio of today's index to 20 days earlier minus one", "inputs": ["index:fcpi:*"], "unit": "ratio"},
    {"id": "spot-discount", "name": "Spot discount", "definition": "1 - median spot ask / median on-demand ask per model", "inputs": ["gpu offers"], "unit": "ratio"},
    {"id": "divergence-z", "name": "Compute/equity divergence z", "definition": "z-score of (index 20d change - basket 20d change) over 60 days", "inputs": ["index:fcpi:h100-sxm-80gb", "equity:*:close"], "unit": "z"},
    {"id": "regime-ratio", "name": "Supply regime ratio", "definition": "20-day mean / 60-day mean of the index", "inputs": ["index:fcpi:h100-sxm-80gb"], "unit": "ratio"},
    {"id": "event-severity", "name": "Verified event severity", "definition": "max severity of verified announcement/earnings items in 3 days", "inputs": ["feed"], "unit": "1-5"},
    {"id": "energy-day-ahead", "name": "Day-ahead power price", "definition": "regional day-ahead price indicator", "inputs": ["energy:*"], "unit": "USD/MWh"},
]


@_snapshotted
def factors() -> dict:
    return {"factors": FACTORS, "freshness": freshness([_data().file_provenance["series"]])}


def _strategy_payload(db: Session, data: SampleData, spec: strategies.StrategySpec, account: PaperAccount | None, *, with_backtest: bool) -> dict:
    payload = {"id": spec.id, "name": spec.name, "objective": spec.objective, "rule": spec.rule, "parameters": spec.parameters,
               "universe": spec.universe, "backtest": None, "forward": None}
    if with_backtest:
        # The one costed backtest: the same run, at the same cost, as this strategy's compute row in
        # `evaluation_portfolio`, so the strategy page and /research can never state two results for one window.
        bt = strategies.backtest(spec, data, variant=strategies.VARIANT_COMPUTE, cost_bps=strategies.COST_BPS_EVALUATION)
        payload["backtest"] = {k: v for k, v in bt.__dict__.items() if k != "equity_curve"}
    rows = strategies.forward_decisions(db, spec.id, account)
    # A replay is hindsight: it is listed (marked) but never counted as forward evidence (falsifier R2-06).
    forward_rows = [r for r in rows if not strategies.is_replay(r)]
    payload["forward"] = {"decisions": [_decision_row(r) for r in rows], "orders": sum(1 for r in forward_rows if r.acted),
                          "proposed": sum(1 for r in forward_rows if r.status == strategies.STATUS_PROPOSED),
                          "replays": len(rows) - len(forward_rows)}
    return payload


def _decision_row(row) -> dict:
    base = {"t": row.t, "action": row.action.value if hasattr(row.action, "value") else row.action, "ticker": row.ticker,
            "rationale": row.rationale, "known_data_snapshot": row.known_data_snapshot, "mode": "forward", "acted": row.acted,
            "replay_of": strategies.replay_of(row)}
    if isinstance(row, StrategyDecision):
        base.update({"id": row.id, "strategy_id": row.strategy_id, "status": row.status, "signal_id": row.signal_id, "signal_kind": row.signal_kind,
                     "order_ids": list(row.order_ids or []), "skipped_reason": row.skipped_reason, "horizon_days": row.horizon_days,
                     "proposed_weight": row.proposed_weight})
    return base


def strategies_view(db: Session, user: User | None) -> dict:
    data = _data()
    account = broker.get_or_create_account(db, user.id) if user else None
    if account is not None:
        strategies.expire_proposals(db, account)
    rows = [_strategy_payload(db, data, spec, account, with_backtest=False) for spec in strategies.STRATEGIES.values()]
    return {"strategies": rows, "freshness": freshness([data.file_provenance["series"]])}


def strategy_view(db: Session, strategy_id: str, user: User | None) -> dict:
    data = _data()
    spec = strategies.STRATEGIES.get(strategy_id)
    if spec is None:
        raise UnknownEntityError(entity="strategy", id=strategy_id)
    account = broker.get_or_create_account(db, user.id) if user else None
    if account is not None:
        # A lapsed proposal reads lapsed here too, not only after a fund read (falsifier R2-33).
        strategies.expire_proposals(db, account)
    return _strategy_payload(db, data, spec, account, with_backtest=True)


def replay_window(data: SampleData) -> tuple[datetime, datetime]:
    """The dates a step may be replayed at: from the first index point a rule can decide on (the
    backtests' 61-point warm-up) to the last sample point."""
    points = data.series[strategies.INDEX_SERIES].points
    return points[min(61, len(points) - 1)].t, points[-1].t


def strategy_step(db: Session, strategy_id: str, user: User, as_of: datetime | None = None) -> dict:
    """Proposes from the data known now, or, with `as_of`, REPLAYS the rule at a past sample date
    so a reader can see (and accept) what the rule proposed then; the row carries `replayOf` and
    the response `replay: true`, so nothing reads it as today's rule output (falsifier V3 F4)."""
    data = _data()
    spec = strategies.STRATEGIES.get(strategy_id)
    if spec is None:
        raise UnknownEntityError(entity="strategy", id=strategy_id)
    if as_of is not None:
        as_of = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=timezone.utc)
        first, last = replay_window(data)
        # A date names the whole day: replay at its end, so that day's close is known.
        as_of = as_of.replace(hour=23, minute=59, second=59, microsecond=0) if (as_of.hour, as_of.minute, as_of.second) == (0, 0, 0) else as_of
        if as_of.date() < first.date() or as_of.date() > last.date():
            raise PaperOrderError(reason="replay-out-of-range", earliest=first.date().isoformat(), latest=last.date().isoformat())
    account = broker.get_or_create_account(db, user.id)
    strategies.expire_proposals(db, account)
    row, order = strategies.step_forward(db, spec, data, account, price_lookup_for(data), at=as_of, replay=as_of is not None)
    return {"strategy_id": spec.id, "decision": _decision_row(row), "order": broker.order_view(order) if order else None,
            "replay": as_of is not None, "as_of": as_of}


# -- paper desk ---------------------------------------------------------------------------

def _equity_series(db: Session | None, data: SampleData) -> dict[str, domain.Series]:
    return {sid: archive.extend(s, db) for sid, s in data.series.items() if sid.startswith("equity:")}


def _account_payload(db: Session, data: SampleData, account: PaperAccount) -> dict:
    summary = broker.summary(db, account, price_lookup_for(data), theme_lookup=_theme_lookup(data))
    # The fund's drawdown computation, not the broker's peak-at-last-touch figure: the desk and
    # the fund show one number for one account (falsifier V3, desk 0.01% against fund 0.00%).
    max_dd = fund_engine.account_max_drawdown_pct(db, account, _equity_series(db, data), utcnow(), summary.equity_usd)
    return {**{k: v for k, v in summary.__dict__.items() if k != "provenances"}, "max_drawdown_pct": max_dd, "freshness": freshness(summary.provenances)}


def paper_account(db: Session, user: User) -> dict:
    data = _data()
    return _account_payload(db, data, broker.get_or_create_account(db, user.id))


def paper_place_order(db: Session, user: User, request) -> dict:
    data = _data()
    account = broker.get_or_create_account(db, user.id)
    order = broker.place_order(
        db, account, ticker=request.ticker, side=request.side, order_type=request.order_type,
        quantity=request.quantity, notional_usd=request.notional_usd, limit_price=request.limit_price,
        origin="manual", price_lookup=price_lookup_for(data), instrument_exists=instrument_check_for(data),
    )
    return broker.order_view(order)


def paper_orders(db: Session, user: User) -> dict:
    data = _data()
    account = broker.get_or_create_account(db, user.id)
    broker.settle_open_orders(db, account, price_lookup_for(data))
    rows = [broker.order_view(o) for o in broker.list_orders(db, account)]
    provs = [Provenance.from_dict(r["fill_provenance"]) for r in rows if r["fill_provenance"]]
    return {"orders": rows, "freshness": freshness(provs)}


def paper_cancel(db: Session, user: User, order_id) -> dict:
    account = broker.get_or_create_account(db, user.id)
    return broker.order_view(broker.cancel_order(db, account, order_id))


def paper_close(db: Session, user: User, ticker: str) -> dict:
    data = _data()
    account = broker.get_or_create_account(db, user.id)
    return broker.order_view(broker.close_position(db, account, ticker, price_lookup_for(data)))


def paper_close_all(db: Session, user: User, confirm: bool) -> dict:
    data = _data()
    account = broker.get_or_create_account(db, user.id)
    closed = broker.close_all(db, account, confirm=confirm, price_lookup=price_lookup_for(data))
    return {"closed": [broker.order_view(o) for o in closed], "account": _account_payload(db, data, account)}


# -- procurement and ask ---------------------------------------------------------------------

def procure_plan(db: Session | None, workload: domain.Workload) -> dict:
    """Ranks live and sample asks together; each plan's offer carries its own provenance, so the
    planner's provenance names both input kinds when both were present."""
    data = _data()
    ledger = Ledger()
    live_rows = _live_offers(ledger)
    all_rows = [*live_rows, *data.offers]
    result = procurement.plan(workload, all_rows, data.gpu_models, data.regions)
    used_ids = {p.offer_id for p in result.plans}
    used = [o for o in all_rows if o.id in used_ids] or all_rows
    prov = derived("procurement:plan-v1", [o.provenance for o in used], note="ranked over advertised asks")
    _archive_offers_and_indices(db, ledger, [])
    return {"workload": workload, "plans": result.plans, "excluded": result.excluded, "caveats": result.caveats,
            "provenance": prov, "freshness": ledger.freshness(o.provenance for o in all_rows)}


def save_workload(db: Session, user: User, spec: dict) -> SavedWorkload:
    row = SavedWorkload(user_id=user.id, spec=spec)
    db.add(row)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(row)
    return row


def ask(query: str, db: Session | None = None) -> dict:
    parsed = ask_grammar.parse(query, _data())
    out = parsed.__dict__
    if parsed.intent == "brief":
        try:
            out["brief"] = _brief(db, parsed.filters["target"])
        except Exception:
            logger.warning("brief composition failed for %r", query, exc_info=True)
            out["brief"] = None
    return out


# ============================================================================
# Inferred state (architecture §10): pressure, supply, anomalies, exposure, scenarios
# ============================================================================

HISTORY_ARCHIVE_DAYS = 60
# The GetDeploying dataset is 53 trailing weeks; a year and a bit keeps every stored week in the momentum read (N-GDH-BE).
GETDEPLOYING_HISTORY_DAYS = 400
EVALUATION_CACHE_TTL_SECONDS = 600
EVALUATION_FEATURE_MODEL = "h100-sxm-80gb"
SNAPSHOT_PREVIOUS_AT = None   # resolved from the sample files at runtime


class InferredInputs:
    """Everything the inferred-state engines read, merged live-over-sample under the aggregate rule:
    per GPU model the offers are live rows when any of them sits in a region of the table, else
    sample rows, never both; series are
    the sample series extended by the archive plus the live index where a model has live asks;
    facilities, markets and feed are the merged lists each row carrying its own provenance."""

    def __init__(self, db: Session | None, ledger: Ledger, data: SampleData, *, with_news: bool = True):
        self.db, self.ledger, self.data = db, ledger, data
        self.live_offers = _live_offers(ledger)
        by_model_live = {gid: [o for o in self.live_offers if o.gpu_model_id == gid] for gid in data.gpu_models}
        self.offers = []
        self.offer_kind_by_model = {}
        for gid in data.gpu_models:
            # The grid is regional: live asks replace a model's sample asks only when at least one of
            # them is placed in a region of the table. Asks with no region (RunPod and Akash publish
            # none) cannot populate a single cell, so a model with only those keeps its sample asks,
            # labelled sample; its live asks still drive its FCPI index elsewhere.
            regional = any(o.region_id in data.regions for o in by_model_live[gid])
            rows = by_model_live[gid] if regional else [o for o in data.offers if o.gpu_model_id == gid]
            self.offer_kind_by_model[gid] = "live" if regional else "sample"
            self.offers.extend(rows)
        self.facilities = [*data.facilities, *ledger.take(_gather(_facility_calls()))]
        self.markets = _merge_markets(data.markets, _live_markets(ledger))
        self.feed = _merge_feed(data.feed, _live_news(ledger, with_filings=False)) if with_news else list(data.feed)
        energy_live = ledger.take(_gather(_energy_calls()))
        self.series: dict[str, domain.Series] = {}
        for sid, sample in data.series.items():
            self.series[sid] = archive.extend(sample, db)
        # Every live-only power series the licence table allows is read from the archive whether or not its
        # source answered this sweep: the backfilled history (tools/history_backfill.py) is what gives the
        # power-stress median and the infra energy change their 30 days, and a warming or blocked upstream
        # must not empty them. Live slots win over an archived row at the same time.
        energy_ids = {p.series_id for p in energy_live} | {sid for sid, (source, _, _) in live_series().items() if licences.enabled(source)}
        for sid in sorted(energy_ids):
            points = _bucketed(sorted((p for p in energy_live if p.series_id == sid), key=lambda p: p.t), ENERGY_INPUT_BUCKET)
            known = {p.t for p in points}
            merged = sorted([*[p for p in _archived_tail(db, sid, ENERGY_HISTORY_DAYS, bucket=ENERGY_INPUT_BUCKET) if p.t not in known], *points], key=lambda p: p.t)
            if merged:
                self.series[sid] = domain.Series(id=sid, unit=merged[0].unit, points=merged)
        for gid, kind in self.offer_kind_by_model.items():
            if kind == "live":
                ix = _index_for(db, data, gid, self.live_offers)
                if effective_kind(ix.provenance) == "sample":
                    continue   # the model's live asks are all `display-not-index` (Vast.ai): no live index exists
                self.series[ix.id] = domain.Series(id=ix.id, unit="USD/GPU-hour", points=_since_last_break(ix.values, ix.breaks))
        # N-GDH-BE: GetDeploying's weekly median (tools/getdeploying_load.py, CC BY 4.0) is read whole for every model, under
        # its own id, so the momentum factor can fall back to it while the model's FLOPX index is younger than the 30-day
        # window (pressure.momentum_series_for) and a history recomputed at a past date finds the weeks known then. It is
        # never merged into an FCPI or on-demand series; absent without a database or rows, so the sample path is untouched.
        for gid in data.gpu_models:
            sid = f"gpu:{gid}:getdeploying:weekly-median"
            weekly = _archived_tail(db, sid, GETDEPLOYING_HISTORY_DAYS)
            if weekly:
                self.series[sid] = domain.Series(id=sid, unit=weekly[0].unit, points=weekly)
        self.as_of = max([o.provenance.observed_at for o in self.offers] + [utcnow() if self.live_offers else max(o.provenance.observed_at for o in data.offers)])
        _archive_offers_and_indices(db, ledger, [])
        _archive(db, "energy", ledger.ok("energy-charts") + ledger.ok("elexon") + ledger.ok("eia"), lambda rows: rows)
        _archive(db, "markets", ledger.ok("polymarket") + ledger.ok("kalshi"), archive.observations_from_markets)

    @property
    def cell_kwargs(self) -> dict:
        return dict(gpu_models=self.data.gpu_models, regions=self.data.regions, series=self.series, facilities=self.facilities,
                    markets=self.markets, feed=self.feed)

    def provenances(self) -> list[Provenance]:
        return [o.provenance for o in self.offers] + [f.provenance for f in self.facilities] + [m.provenance for m in self.markets] + [i.provenance for i in self.feed[:50]]

    def snapshots(self, gpu_model_id: str) -> list[tuple[datetime, list[domain.GpuOffer]]]:
        """Offer snapshots by date for one model: the two sample snapshots, then one per archive day of live asks."""
        data = self.data
        if self.offer_kind_by_model.get(gpu_model_id) == "sample":
            previous_at = max(o.provenance.observed_at for o in data.offers_previous)
            current_at = max(o.provenance.observed_at for o in data.offers)
            return [(previous_at, [o for o in data.offers_previous if o.gpu_model_id == gpu_model_id]),
                    (current_at, [o for o in data.offers if o.gpu_model_id == gpu_model_id])]
        live = [o for o in self.live_offers if o.gpu_model_id == gpu_model_id]
        return [*_archived_snapshots(self.db, live), (self.as_of, live)]


def _inputs(db: Session | None, data: SampleData, *, with_news: bool = True) -> InferredInputs:
    """The inferred-state inputs for one composition; inside a refresher sweep (B-PERF) one build per
    session and news flag serves every composition of the sweep (pressure, supply, infra, anomalies,
    evaluation all read the same merged inputs), so a sweep builds them once, not once per read."""
    memo = refresher.sweep_memo()
    if memo is None:
        return InferredInputs(db, Ledger(), data, with_news=with_news)
    key = (id(db), with_news)
    if key not in memo:
        memo[key] = InferredInputs(db, Ledger(), data, with_news=with_news)
    return memo[key]


def _archived_snapshots(db: Session | None, live_rows: list[domain.GpuOffer]) -> list[tuple[datetime, list[domain.GpuOffer]]]:
    """One snapshot per archive day: each live offer as the archive last saw it by the end of that day.
    Only prices and availability come from the archive; the descriptors are the live offer's."""
    if db is None or not live_rows:
        return []
    try:
        series_by_offer = {o.id: archive.archived_series(db, f"offer:{o.id}:price", since=utcnow() - timedelta(days=HISTORY_ARCHIVE_DAYS)) for o in live_rows}
        avail_by_offer = {o.id: archive.archived_series(db, f"offer:{o.id}:available", since=utcnow() - timedelta(days=HISTORY_ARCHIVE_DAYS)) for o in live_rows}
    except Exception:
        logger.warning("archived offer history skipped", exc_info=True)
        return []
    days = sorted({p.t.date() for s in series_by_offer.values() if s for p in s.points})
    today = max((o.provenance.observed_at for o in live_rows)).date()
    levels = {1.0: "available", 0.5: "limited", 0.0: "unavailable"}
    out = []
    for day in days:
        if day >= today:
            continue
        day_end = datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=timezone.utc)
        rows = []
        for o in live_rows:
            prices = [p for p in (series_by_offer[o.id].points if series_by_offer[o.id] else []) if p.t <= day_end]
            if not prices:
                continue
            avail = [p for p in (avail_by_offer[o.id].points if avail_by_offer[o.id] else []) if p.t <= day_end]
            rows.append(replace(o, price_per_gpu_hour_usd=prices[-1].value, availability=levels.get(avail[-1].value, o.availability) if avail else o.availability,
                                provenance=prices[-1].provenance))
        if rows:
            out.append((day_end, rows))
    return out


def _grid(inputs: InferredInputs, region_ids=None, gpu_model_ids=None, *, with_history: bool) -> list[domain.PressureCell]:
    cells = pressure.compute_grid(offers=inputs.offers, as_of=inputs.as_of, region_ids=region_ids, gpu_model_ids=gpu_model_ids, **inputs.cell_kwargs)
    if not with_history:
        return cells
    out = []
    snapshots_by_model: dict[str, list] = {}
    for cell in cells:
        snaps = snapshots_by_model.setdefault(cell.gpu_model_id, inputs.snapshots(cell.gpu_model_id))
        history, basis = pressure.cell_history(cell.region_id, cell.gpu_model_id, snaps, **inputs.cell_kwargs)
        out.append(pressure.with_history(cell, history, basis))
    return out


@_snapshotted
def pressure_grid(db: Session | None, gpu: str | None, region: str | None) -> dict:
    data = _data()
    if gpu and gpu not in data.gpu_models:
        raise UnknownEntityError(entity="gpuModel", id=gpu)
    inputs = _inputs(db, data)
    regions = [r for r in region.split(",") if r] if region else None
    for rid in regions or []:
        if rid not in data.regions:
            raise UnknownEntityError(entity="region", id=rid)
    cells = _grid(inputs, regions, [gpu] if gpu else None, with_history=False)
    return {"as_of": inputs.as_of, "method": pressure.METHOD, "method_text": pressure.METHOD_TEXT,
            "bands": {"slack": "< 25", "balanced": "25 to 50", "tight": "50 to 75", "acute": ">= 75"},
            "cells": cells, "freshness": inputs.ledger.freshness(inputs.provenances())}


@_snapshotted
def pressure_cell(db: Session | None, region: str, gpu: str) -> dict:
    data = _data()
    if region not in data.regions:
        raise UnknownEntityError(entity="region", id=region)
    if gpu not in data.gpu_models:
        raise UnknownEntityError(entity="gpuModel", id=gpu)
    inputs = _inputs(db, data)
    cell = _grid(inputs, [region], [gpu], with_history=True)[0]
    provs = [p for f in cell.factors for p in f.evidence] or inputs.provenances()
    return {**cell.__dict__, "freshness": inputs.ledger.freshness(provs)}


@_snapshotted
def effective_supply_view(db: Session | None, region: str | None) -> dict:
    data = _data()
    inputs = _inputs(db, data, with_news=False)
    regions = [r for r in region.split(",") if r] if region else sorted(data.regions)
    for rid in regions:
        if rid not in data.regions:
            raise UnknownEntityError(entity="region", id=rid)
    rows = [supply_engine.effective_supply(rid, inputs.facilities, inputs.as_of) for rid in regions]
    return {"regions": rows, "freshness": inputs.ledger.freshness(f.provenance for f in inputs.facilities)}


def _exposures_by_region(data: SampleData, facilities: list[domain.Facility]) -> dict[str, list[domain.Exposure]]:
    return {rid: exposure_engine.trace(rid, None, companies=data.companies, facilities=facilities, regions=data.regions) for rid in data.regions}


DETECTION_SUFFIX = ":detected"


def _detection_ids(cells: list[domain.PressureCell], inputs: InferredInputs) -> list[str]:
    """Candidate anomaly ids whose current snapshot is a live read (its time is the read's), the only ones a
    persisted first detection can improve on: a sample snapshot is dated by the sample set already."""
    live = [c for c in cells if inputs.offer_kind_by_model.get(c.gpu_model_id) == "live" and c.uncertainty.coverage > 0]
    return [f"anomaly:{kind}:{c.region_id}:{c.gpu_model_id}" for c in live for kind in anomaly_engine.RULES]


def _first_detected(db: Session | None, ids: list[str]) -> dict[str, datetime]:
    """The earliest archived detection per anomaly id; empty without a database or on any read failure."""
    if db is None or not ids:
        return {}
    try:
        from sqlalchemy import func
        from product.models import Observation
        rows = (db.query(Observation.series_id, func.min(Observation.t))
                .filter(Observation.series_id.in_([i + DETECTION_SUFFIX for i in ids])).group_by(Observation.series_id).all())
    except Exception:
        logger.warning("anomaly first detections unavailable", exc_info=True)
        return {}
    out = {}
    for sid, t in rows:
        if t is not None:
            out[sid[: -len(DETECTION_SUFFIX)]] = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    return out


def _record_detections(db: Session | None, found: list[domain.Anomaly], known: dict[str, datetime], ids: list[str]) -> None:
    """Archives the first detection of each live-snapshot anomaly not yet on record, so its firstSeen is the same on
    every later read; never fails the read."""
    if db is None:
        return
    wanted = set(ids)
    rows = [domain.PriceObservation(series_id=a.id + DETECTION_SUFFIX, t=a.first_seen, value=1.0, unit="detected", provenance=a.provenance)
            for a in found if a.id in wanted and a.id not in known]
    if not rows:
        return
    try:
        archive.record(db, rows)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("anomaly first detections not recorded", exc_info=True)


def _anomalies(inputs: InferredInputs) -> tuple[list[domain.Anomaly], list[domain.PressureCell], dict]:
    cells = _grid(inputs, with_history=True)
    exposures = _exposures_by_region(inputs.data, inputs.facilities)
    ids = _detection_ids(cells, inputs)
    known = _first_detected(inputs.db, ids)
    found = anomaly_engine.detect(cells, offers=inputs.offers, gpu_models=inputs.data.gpu_models, regions=inputs.data.regions,
                                  facilities=inputs.facilities, series=inputs.series, exposures_by_region=exposures, first_detected=known)
    _record_detections(inputs.db, found, known, ids)
    _remember_anomalies(found, cells)
    return found, cells, exposures


# An anomaly id is its rule, region and model, so it never changes; but a rule can stop holding between two
# reads, and a link copied minutes earlier would then land on Not found (V4 F26). Each composition remembers
# every anomaly it found for a day, and a read of an id that no longer fires answers with that last state,
# marked resolved with the snapshot it last held at. Cache-backed and fail-open: per worker on the memory
# backend, so "where one exists" is the honest scope; nothing here is a record of truth.
LAST_SEEN_TTL_SECONDS = 24 * 3600


def _last_seen_key(anomaly_id: str) -> str:
    return f"flopx:anomaly-last-seen:v1:{anomaly_id}"


def _remember_anomalies(found: list[domain.Anomaly], cells: list[domain.PressureCell]) -> None:
    by_cell = {(c.region_id, c.gpu_model_id): c for c in cells}
    for a in found:
        cell = by_cell.get((a.region_id, a.gpu_model_id))
        payload = {"anomaly": a, "seen_at": anomaly_engine.snapshot_time(cell) if cell else a.first_seen}
        try:
            cache.delete(_last_seen_key(a.id))
            cache.get_or_set(_last_seen_key(a.id), lambda p=payload: p, ttl_seconds=LAST_SEEN_TTL_SECONDS, tags=None)
        except Exception:
            logger.warning("anomaly last-seen not remembered", exc_info=True)


def _resolved_anomaly(anomaly_id: str) -> domain.Anomaly | None:
    """The last state of an anomaly that no longer fires, marked resolved; None when none is remembered."""
    try:
        payload = cache.get_or_set(_last_seen_key(anomaly_id), lambda: None, ttl_seconds=60, tags=None)
    except Exception:
        return None
    if not payload:
        return None
    return replace(payload["anomaly"], status="resolved", last_seen=payload["seen_at"])


INFERRED_SNAPSHOT_TTL_SECONDS = 60
INFERRED_SNAPSHOT_KEY = "flopx:inferred-snapshot:v1"


def _inferred_snapshot(db: Session | None) -> dict:
    """The composed anomaly state for one snapshot window (60 s, TTL-only: it reads upstreams and the
    archive, not tables a write would evict): the anomalies, the scored cells, each anomaly's signal
    chain, the series the chains read, the provenances and the unavailable sources. Every anomaly and
    signal read in the window answers from the same composition, so a list and its detail page agree,
    and a warm read costs a copy instead of a recomposition (V6 #12)."""
    def compose(session: Session | None = db) -> dict:
        inputs = _inputs(session, _data())
        found, cells, _ = _anomalies(inputs)
        return {"found": found, "cells": cells, "anomaly_signals": [signal_chains.from_anomaly(a, inputs.series) for a in found],
                "series": inputs.series, "facilities": inputs.facilities, "as_of": inputs.as_of,
                "provenances": inputs.provenances(), "unavailable": list(inputs.ledger.unavailable)}
    if refresher.running():
        # B-PERF: the warm composition, recomposed in the background (no deep copy per read).
        return refresher.composed(INFERRED_SNAPSHOT_KEY, compose, db)
    return cache.get_or_set(INFERRED_SNAPSHOT_KEY, compose, ttl_seconds=INFERRED_SNAPSHOT_TTL_SECONDS, tags=None)


def _snapshot_freshness(snap: dict) -> Freshness:
    return Freshness.of(snap["provenances"], unavailable=snap["unavailable"])


def anomalies_view(db: Session | None, region: str | None, gpu: str | None, kind: str | None) -> dict:
    snap = _inferred_snapshot(db)
    found, cells = snap["found"], snap["cells"]
    if region:
        found = [a for a in found if a.region_id in region.split(",")]
    if gpu:
        found = [a for a in found if a.gpu_model_id == gpu]
    if kind:
        found = [a for a in found if a.type in kind.split(",")]
    return {"anomalies": found, "cells_scanned": sum(1 for c in cells if c.uncertainty.coverage > 0), "rules": anomaly_engine.RULES,
            "freshness": _snapshot_freshness(snap)}


def anomaly_detail(db: Session | None, anomaly_id: str) -> dict:
    snap = _inferred_snapshot(db)
    index = next((i for i, a in enumerate(snap["found"]) if a.id == anomaly_id), None)
    if index is None:
        anomaly = _resolved_anomaly(anomaly_id)
        if anomaly is None:
            # The id may name a cell whose live inputs did not answer this window: say which did not.
            raise UnknownEntityError(entity="anomaly", id=anomaly_id, unavailable=snap["unavailable"])
        signal = signal_chains.from_anomaly(anomaly, snap["series"])
    else:
        anomaly = snap["found"][index]
        signal = snap["anomaly_signals"][index]
    # The current cell either way: for a resolved anomaly it shows why the rule no longer holds.
    cell = next((c for c in snap["cells"] if c.region_id == anomaly.region_id and c.gpu_model_id == anomaly.gpu_model_id), None)
    supply = supply_engine.effective_supply(anomaly.region_id, snap["facilities"], snap["as_of"])
    return {**anomaly.__dict__, "signal": signal, "cell": cell, "effective_supply": supply, "freshness": _snapshot_freshness(snap)}


@_snapshotted
def exposure_view(db: Session | None, region: str, gpu: str | None) -> dict:
    data = _data()
    if region not in data.regions:
        raise UnknownEntityError(entity="region", id=region)
    if gpu and gpu not in data.gpu_models:
        raise UnknownEntityError(entity="gpuModel", id=gpu)
    ledger = Ledger()
    facilities = [*data.facilities, *ledger.take(_gather(_facility_calls()))]
    filings = ledger.take(_gather([(lambda t=t: sec.fetch_filings(sec.CIKS[t], t)) for t in FEED_FILING_TICKERS]))
    rows = exposure_engine.trace(region, gpu, companies=data.companies, facilities=facilities, regions=data.regions, filings=filings)
    provs = [e.provenance for e in rows] + [f.provenance for f in facilities if f.region_id == region]
    return {"region_id": region, "gpu_model_id": gpu, "exposures": rows, "freshness": ledger.freshness(provs)}


def exposure_view_for(db: Session | None, user: User | None, region: str, gpu: str | None) -> dict:
    """N-XRAY: the user-independent exposure answer with the signed-in account's paper positions overlaid per
    row (the caching rule's skeleton-plus-overlay: the shared composition is never mutated, and nothing per
    user enters it). Anonymous readers, and accounts with no paper desk yet, get `position: None` on every row;
    no account row is created by a read. Positions are valued at the sample close (`allow_live=False`) so the
    overlay never fans out to a price source; a ticker with no close keeps `market_value_usd: None`."""
    view = exposure_view(db, region, gpu)
    if user is None or db is None:
        return view
    account = db.query(PaperAccount).filter(PaperAccount.user_id == user.id).first()
    if account is None:
        return view
    held = {p.ticker: p for p in broker.positions(db, account)}
    if not held:
        return view
    lookup = price_lookup_for(_data(), allow_live=False)
    equity_usd = broker.usd(broker._equity_minor(db, account, lookup))
    rows = []
    for e in view["exposures"]:
        p = held.get(e.ticker)
        if p is None:
            rows.append(e)
            continue
        qty, avg = float(p.quantity), float(p.avg_cost)
        last = lookup(e.ticker)
        value = round(qty * float(last.value), 2) if last is not None else None
        weight = round(value / equity_usd, 4) if (value is not None and equity_usd) else None
        position = domain.ExposurePosition(quantity=qty, side="long" if qty >= 0 else "short", avg_cost_usd=avg,
                                           market_value_usd=value, weight=weight)
        rows.append(replace(e, position=position))
    return {**view, "exposures": rows}


def scenario_infra(db: Session | None, payload) -> dict:
    data = _data()
    if payload.region_id not in data.regions:
        raise UnknownEntityError(entity="region", id=payload.region_id)
    gpu = payload.gpu_model_id or EVALUATION_FEATURE_MODEL
    if gpu not in data.gpu_models:
        raise UnknownEntityError(entity="gpuModel", id=gpu)
    inputs = _inputs(db, data)
    assumptions = [a.model_dump(by_alias=True, exclude_none=True) for a in payload.assumptions]
    result = scenarios.project_infra(payload.region_id, gpu, payload.horizon_days, assumptions, offers=inputs.offers, companies=data.companies,
                                     as_of=inputs.as_of, **inputs.cell_kwargs)
    prov = derived("scenario:infra-v1", inputs.provenances(), observed_at=inputs.as_of, note="conditional on stated assumptions; demonstrative elasticities")
    return {**result.__dict__, "provenance": prov, "freshness": inputs.ledger.freshness(inputs.provenances())}


# -- evaluation ---------------------------------------------------------------------------------

def _top_documented_region(data: SampleData, ticker: str) -> str | None:
    mw: dict[str, float] = {}
    for f in data.facilities:
        if ticker in f.owner_company_ids:
            mw[f.region_id] = mw.get(f.region_id, 0.0) + (f.capacity_mw or 0.0)
    return max(mw, key=mw.get) if mw else None


def _known_by(prov: Provenance, t: datetime, last_close: datetime | None) -> bool:
    """An evaluation input is either authored sample or something observed at or before the last close it is scored
    against; anything later cannot reach a point-in-time feature and is left out rather than carried along."""
    return prov.kind == "sample" or (last_close is not None and t <= last_close)


def _evaluation_snapshots(inputs: InferredInputs, data: SampleData, last_close: datetime | None = None) -> list[tuple[datetime, list[domain.GpuOffer]]]:
    """Offer snapshots for the evaluation's pressure feature: the two sample snapshots, then any live or archived
    snapshot dated after the last of them and at or before the last close (R1-9). The route-handler tier has no
    archive and evaluates the sample bag, so the sample dates always read the sample snapshots in both tiers; a
    snapshot from the archive joins only where it can actually reach a scored close, and the evaluation's input
    kinds then name it."""
    gid = EVALUATION_FEATURE_MODEL
    previous_at = max(o.provenance.observed_at for o in data.offers_previous)
    current_at = max(o.provenance.observed_at for o in data.offers)
    sample = [(previous_at, [o for o in data.offers_previous if o.gpu_model_id == gid]), (current_at, [o for o in data.offers if o.gpu_model_id == gid])]
    if inputs.offer_kind_by_model.get(gid) == "sample" or last_close is None:
        return sample
    return sample + [(at, rows) for at, rows in inputs.snapshots(gid) if current_at < at <= last_close]


def _evaluation_cell_kwargs(inputs: InferredInputs, data: SampleData, last_close: datetime | None) -> dict:
    """The pressure feature's inputs, the same set in both tiers (R1-9): the sample bag's series (extended by the
    archive only where a point is known by the last close), the sample facilities and markets plus any observed row
    known by then, and the sample feed. A live FCPI index or a live-only power series never enters: it holds nothing
    dated inside the scored window, and in the route-handler tier the live index's history is the sample series
    rescaled to today's median, which would be look-ahead."""
    series = {}
    for sid in data.series:
        s = inputs.series.get(sid)
        points = [p for p in (s.points if s else []) if _known_by(p.provenance, p.t, last_close)]
        if points:
            series[sid] = domain.Series(id=sid, unit=s.unit, points=points)
    return dict(gpu_models=data.gpu_models, regions=data.regions, series=series,
                facilities=[f for f in inputs.facilities if _known_by(f.provenance, f.provenance.observed_at, last_close)],
                markets=[m for m in inputs.markets if _known_by(m.provenance, m.provenance.observed_at, last_close)],
                feed=list(data.feed))


def _observed_closes_statement(result, closes: domain.Series) -> dict:
    """What an evaluation on observed closes says about itself: the closes' source and last date in its feature
    sources, and a reliability note that names the data instead of the authored-sample caveat. Whether a compute
    feature reached the fit is read off the result, never assumed."""
    last = closes.points[-1]
    source, through = last.provenance.source, last.t.date().isoformat()
    row = licences.policy(source) or {}
    coverage = ("no compute feature covers these dates, so only the baseline is scored on them"
                if not result.features_available else f"compute features used: {', '.join(result.features_available)}")
    return {
        "feature_sources": {**result.feature_sources, "closes": f"{source}: {row.get('name', source)}, scored through {through}; the authored sample series is not used for this ticker"},
        "reliability_note": f"out-of-sample on observed closes: scored through {through} on {source} closes ({row.get('name', source)}); {coverage}",
    }


def _evaluate_ticker_uncached(db: Session | None, ticker: str) -> dict:
    """The composition both tiers mirror (engine/terminal.ts `evaluateTickerView`, pinned by the
    evaluation_terminal drift fixture): same closes, same feature series and fallbacks, same sample feed.
    With real closes in the archive the sample fixture no longer applies: the evaluation runs on them."""
    data = _data()
    inputs = _inputs(db, data, with_news=False)
    # Real closes where the archive holds them (the WRDS CRSP extract, decision G13): the evaluation then runs on
    # observed history through its last real date and never on the authored 2026 series for that ticker.
    observed = _observed_series(db, f"equity:{ticker}:close")
    closes = observed if observed is not None else inputs.series[f"equity:{ticker}:close"]
    last_close = closes.points[-1].t if closes.points else None
    cell_kwargs = _evaluation_cell_kwargs(inputs, data, last_close)
    feature_series = cell_kwargs["series"]
    snapshots = _evaluation_snapshots(inputs, data, last_close)
    region = _top_documented_region(data, ticker)
    price, price_feature, price_source = evaluation.select_price_feature(
        feature_series.get(f"index:fcpi:{EVALUATION_FEATURE_MODEL}"), feature_series.get(f"gpu:{EVALUATION_FEATURE_MODEL}:on-demand:global"), closes.points)
    sources = {price_feature: price_source, evaluation.PRESSURE_FEATURE: f"pressure:{region}:{EVALUATION_FEATURE_MODEL}" if region else "none (no documented region)",
               evaluation.ENERGY_FEATURE: "none"}
    pressure_scores = energy_s = None
    if region:
        dates = [p.t for p in closes.points]
        pressure_scores = evaluation.pressure_score_series(region, EVALUATION_FEATURE_MODEL, dates, snapshots, **cell_kwargs)
        zone = data.regions[region].grid_zone
        energy_s = feature_series.get(f"energy:{zone}:day-ahead")
        if energy_s is not None:
            sources[evaluation.ENERGY_FEATURE] = energy_s.id
    result, panel = evaluation.evaluate_ticker_with_panel(ticker, closes, fcpi=price, pressure_scores=pressure_scores, energy=energy_s,
                                                          feed=cell_kwargs["feed"], feature_sources=sources, price_feature=price_feature)
    # A snapshot from the archive or the live read joins only when it reaches a scored close (never today); when it
    # does, the evaluation says so in its input kinds rather than claiming a sample-only basis (R1-9).
    extra_kinds = {effective_kind(o.provenance) for at, rows in (snapshots[2:] if region else []) for o in rows}
    if extra_kinds - set(result.input_kinds):
        result = replace(result, input_kinds=sorted(set(result.input_kinds) | extra_kinds))
    if observed is not None:
        result = replace(result, **_observed_closes_statement(result, closes))
    # Freshness names only what the harness read (V6 #7): the closes, the price feature, the power series and the
    # offer snapshots behind the pressure feature; and every source it asked that did not answer (V6 #8).
    snapshot_provs = [o.provenance for at, rows in (snapshots if region else []) if last_close and at <= last_close for o in rows]
    # A feature series with no value at any scored origin (the authored compute series against real 2018-2025
    # closes) was dropped before any fit and is not named as something the harness read.
    absent = {name for name, why in result.features_dropped.items() if why == "no data for every origin"}
    read = [closes] + [s for name, s in ((price_feature, price), (evaluation.ENERGY_FEATURE, energy_s)) if name not in absent]
    used = [s.points[-1].provenance for s in read if s is not None and s.points] + snapshot_provs
    # The panel (every scored forecast with its exit date) and the history coverage ride beside the evaluation so the
    # view can rank and trade forecasts across tickers without a second pass; neither changes the evaluation itself.
    return {"evaluation": result, "provenances": used, "unavailable": list(inputs.ledger.unavailable),
            "panel": panel, "history": evaluation.history_coverage(result, closes)}


# The harness scores closes up to the last close, never today: no live answer changes it (age-only recompose).
@_snapshotted(ttl=EVALUATION_CACHE_TTL_SECONDS, follows_upstream=False)
def evaluation_view(db: Session | None, target: str | None, horizon: str | None) -> dict:
    data = _data()
    tickers = [t.upper() for t in target.split(",")] if target else list(data.companies)
    for t in tickers:
        if t not in data.companies:
            raise UnknownEntityError(entity="company", id=t)
    horizons = list(evaluation.HORIZONS)
    if horizon:
        wanted = {int(h.rstrip("d")) for h in horizon.split(",")}
        horizons = [h for h in horizons if h in wanted]
        if not horizons:
            raise UnknownEntityError(entity="horizon", id=horizon, allowed=[f"{h}d" for h in evaluation.HORIZONS])
    rows, provs, missing, panels, coverage = [], [], [], {}, []
    for t in tickers:
        # The harness is deterministic over the data on hand and costs about half a second per ticker, so the
        # result is cached as plain data for ten minutes (TTL-only: it reads files and the archive, not tables):
        cached = cache.get_or_set(f"flopx:evaluation:v5:{t}", lambda t=t: _evaluate_ticker_uncached(db, t), ttl_seconds=EVALUATION_CACHE_TTL_SECONDS, tags=None)
        ev = cached["evaluation"]
        rows.append(replace(ev, horizons=[h for h in ev.horizons if h.horizon_days in horizons]))
        provs.extend(cached["provenances"])
        missing.extend(u for u in cached.get("unavailable", []) if u not in missing)
        panels[t] = {key: forecasts for key, forecasts in cached["panel"].items() if key[0] in horizons}
        coverage.append(cached["history"])
    # Across the tickers asked for: rank skill (IC) and the sign-of-forecast Sharpe, research numbers that rank and
    # trade the same forecasts the verdicts score; they change no verdict and say so in their provenance.
    wanted = tuple(h for h in evaluation.HORIZONS if h in horizons)
    return {"target": ",".join(tickers), "horizons": horizons, "evaluations": rows, "method_text": evaluation.__doc__.split("\n\n")[1].strip(),
            "cross_section": evaluation.cross_section(panels, wanted), "tradable": evaluation.tradable(panels, wanted),
            "history_coverage": coverage, "freshness": freshness(provs, missing)}


def _backtest_bag(spec: strategies.StrategySpec, variant: str, data: SampleData, real: SampleData, observed: dict[str, domain.Series]) -> SampleData:
    """Which series bag a backtest reads. The price-only variant walks the universe's own closes, so it runs on the
    observed ones wherever the archive holds them. The compute variant walks the compute index's dates and needs a
    close at each of them: where the observed closes end before that index begins (the WRDS extract ends 2025-12-31,
    the authored index starts in 2026) it keeps the authored closes, labelled sample as before, rather than fill
    2026 dates with a 2025 price."""
    if variant == strategies.VARIANT_PRICE_ONLY:
        return real
    index = data.series[strategies.INDEX_SERIES]
    start = index.points[min(61, len(index.points) - 1)].t
    universe = {f"equity:{t}:close" for t in spec.universe}
    covering = [s for sid, s in observed.items() if sid in universe and s.points and s.points[-1].t >= start]
    return real if covering and len(covering) == len(universe & set(observed)) else data


# The harness scores closes up to the last close, never today: no live answer changes it (age-only recompose).
@_snapshotted(ttl=EVALUATION_CACHE_TTL_SECONDS, follows_upstream=False)
def evaluation_portfolio(db: Session | None) -> dict:
    data = _data()
    observed = _observed_equity(db, data)
    real = replace(data, series={**data.series, **observed}) if observed else data
    rows, provs = [], []
    for spec in strategies.STRATEGIES.values():
        for variant in strategies.VARIANTS:
            bt = strategies.backtest(spec, _backtest_bag(spec, variant, data, real, observed), variant=variant, cost_bps=strategies.COST_BPS_EVALUATION)
            provs.append(bt.provenance)
            rows.append({"strategy_id": spec.id, "variant": variant, "rule": spec.rule if variant == strategies.VARIANT_COMPUTE else spec.price_only_rule,
                         **{k: v for k, v in bt.__dict__.items() if k not in ("decisions", "equity_curve", "variant", "statement_class")}})
    index = data.series[strategies.INDEX_SERIES]
    start, end = index.points[61].t, index.points[-1].t
    bench = strategies.equal_weight_benchmark(data, list(data.companies), start, end)
    kinds = sorted({k for p in provs for k in p.input_kinds})
    notes = ["backtests with 5 bps charged per fill; the price-only variant replaces the compute feature with the asset's own momentum"]
    real_rows = [r for r in rows if effective_kind(r["provenance"]) != "sample"]
    if real_rows:
        sources = sorted({r["provenance"].source for r in real_rows} - {f"backtest:{r['strategy_id']}:{r['variant']}" for r in real_rows})
        source_names = sorted({s.points[-1].provenance.source for s in observed.values()})
        through = max(r["end"] for r in real_rows).date().isoformat()
        notes.append(f"{len(real_rows)} price-only rows run on observed closes ({', '.join(source_names)}) through {through}; "
                     f"the compute rows and the benchmark run on the authored sample window ({start.date().isoformat()} to {end.date().isoformat()}) "
                     f"because no compute index covers the observed closes' dates; no realised profit is claimed")
        del sources
    else:
        notes.append("sample closes: this tests the rules on authored data, not the market; no realised profit is claimed")
    return {"strategies": rows, "benchmark": bench, "cost_bps": strategies.COST_BPS_EVALUATION, "input_kinds": kinds or ["sample"], "notes": notes,
            "freshness": freshness([index.points[-1].provenance, *[s.points[-1].provenance for s in observed.values()]])}


# -- signals and the decision ledger ------------------------------------------------------------------

def _orders_by_id(db: Session, account: PaperAccount | None) -> dict[str, PaperOrder]:
    if account is None:
        return {}
    return {str(o.id): o for o in broker.list_orders(db, account, limit=10_000)}


def signals_view(db: Session, user: User | None, kind: str | None) -> dict:
    snap = _inferred_snapshot(db)
    rows = list(snap["anomaly_signals"])
    if user is not None:
        account = broker.get_or_create_account(db, user.id)
        decisions = strategies.forward_decisions(db, None, account, limit=500)
        rows += signal_chains.from_decisions(decisions, snap["series"], _orders_by_id(db, account), strategies=strategies.STRATEGIES)
    if kind:
        rows = [s for s in rows if s.kind in kind.split(",")]
    rows.sort(key=lambda s: s.at, reverse=True)
    return {"signals": rows, "freshness": _snapshot_freshness(snap)}


def signal_detail(db: Session, user: User | None, signal_id: str) -> dict:
    snap = _inferred_snapshot(db)
    fresh = _snapshot_freshness(snap)
    if signal_id.startswith("signal:anomaly:"):
        index = next((i for i, a in enumerate(snap["found"]) if f"signal:anomaly:{a.id}" == signal_id), None)
        if index is None:
            resolved = _resolved_anomaly(signal_id[len("signal:anomaly:"):])
            if resolved is None:
                raise UnknownEntityError(entity="signal", id=signal_id, unavailable=snap["unavailable"])
            return {**signal_chains.from_anomaly(resolved, snap["series"]).__dict__, "anomaly": resolved, "freshness": fresh}
        return {**snap["anomaly_signals"][index].__dict__, "anomaly": snap["found"][index], "freshness": fresh}
    if user is None:
        # 401, not 404: the record may well exist in the reader's account, and a 401 is what makes
        # the client refresh its session and retry (falsifier V3 F13).
        raise SignInRequiredError(entity="signal", id=signal_id, note="strategy signals belong to a signed-in account")
    account = broker.get_or_create_account(db, user.id)
    rows = [r for r in strategies.forward_decisions(db, None, account, limit=500) if r.signal_id == signal_id]
    if not rows:
        raise UnknownEntityError(entity="signal", id=signal_id)
    signal = signal_chains.from_decisions(rows, snap["series"], _orders_by_id(db, account), strategies=strategies.STRATEGIES)[0]
    return {**signal.__dict__, "anomaly": None, "freshness": fresh}


def _ledger_row(row: StrategyDecision) -> dict:
    return _decision_row(row)


def decision_accept(db: Session, strategy_id: str, decision_id: int, user: User) -> dict:
    data = _data()
    spec = strategies.STRATEGIES.get(strategy_id)
    if spec is None:
        raise UnknownEntityError(entity="strategy", id=strategy_id)
    account = broker.get_or_create_account(db, user.id)
    row = db.query(StrategyDecision).filter(StrategyDecision.id == decision_id, StrategyDecision.account_id == account.id).first()
    if row is None:
        raise UnknownEntityError(entity="decision", id=decision_id)
    row, order = strategies.accept_decision(db, spec, account, row, price_lookup_for(data))
    return {"strategy_id": spec.id, "decision": _ledger_row(row), "order": broker.order_view(order) if order else None}


# -- fund ----------------------------------------------------------------------------------------------

def fund_summary(db: Session, user: User) -> dict:
    data = _data()
    account = broker.get_or_create_account(db, user.id)
    series = _equity_series(db, data)
    clusters_prov, clusters = fund_engine.load_clusters()
    strategies.expire_proposals(db, account)
    decisions = strategies.forward_decisions(db, None, account, limit=500)
    payload = fund_engine.summary(db, account, series=series, price_lookup=price_lookup_for(data), clusters=clusters, clusters_provenance=clusters_prov,
                                  decisions=decisions, tickers=list(data.companies), as_of=utcnow())
    provs = payload.pop("provenances")
    return {**payload, "freshness": freshness(provs + [clusters_prov])}


# -- infrastructure reads --------------------------------------------------------------------------------

@_snapshotted
def infra_energy(db: Session | None) -> dict:
    data = _data()
    inputs = _inputs(db, data, with_news=False)
    zones, missing = infra_reads.energy(data.regions, inputs.series, inputs.as_of)
    return {"zones": zones, "missing": missing, "freshness": inputs.ledger.freshness([z.provenance for z in zones])}


@_snapshotted
def infra_projects(status: str | None, region: str | None) -> dict:
    data = _data()
    ledger = Ledger()
    rows = [*data.facilities, *ledger.take(_gather(_facility_calls()))]
    projects = infra_reads.projects(rows, data.companies, utcnow(), status=status, region=region)
    return {"projects": projects, "freshness": ledger.freshness(p.facility.provenance for p in projects)}


@_snapshotted
def infra_grid(db: Session | None) -> dict:
    data = _data()
    inputs = _inputs(db, data, with_news=False)
    rows = infra_reads.grid(data.regions, inputs.series, inputs.as_of)
    return {"regions": rows, "constraint_rule": f"power-stress factor normalised >= {infra_reads.CONSTRAINT_THRESHOLD}",
            "freshness": inputs.ledger.freshness([r.provenance for r in rows if r.provenance])}


# -- the composed brief ---------------------------------------------------------------------------------------

def _brief(db: Session | None, target: str) -> dict:
    data = _data()
    kind, _, ident = target.partition(":")
    inputs = _inputs(db, data)
    found, cells, exposures = _anomalies(inputs)
    latest: list[tuple[str, domain.PriceObservation]] = []
    evaluation_rows: list[dict] = []
    supply = None
    if kind == "region":
        region = data.regions[ident]
        name = region.name
        view_cells = [c for c in cells if c.region_id == ident]
        view_anoms = [a for a in found if a.region_id == ident]
        items = [i for i in inputs.feed if ident in i.geography.region_ids]
        markets = [m for m in inputs.markets if ident in m.linked_entities.region_ids]
        supply = supply_engine.effective_supply(ident, inputs.facilities, inputs.as_of)
        zone_s = inputs.series.get(f"energy:{region.grid_zone}:day-ahead")
        if zone_s and zone_s.points:
            latest.append((f"{region.grid_zone} day-ahead power", zone_s.points[-1]))
        exposure_rows = exposures.get(ident, [])
    elif kind == "ticker":
        company = data.companies[ident]
        name = f"{company.name} ({ident})"
        region = _top_documented_region(data, ident)
        view_cells = [c for c in cells if region and c.region_id == region]
        view_anoms = [a for a in found if any(e.ticker == ident and e.documented for e in a.linked_companies)]
        items = [i for i in inputs.feed if ident in i.entities.tickers]
        markets = [m for m in inputs.markets if ident in m.linked_entities.tickers]
        close = last_point(inputs.series[f"equity:{ident}:close"])
        if close:
            latest.append((f"{ident} close", close))
        try:
            cached = cache.get_or_set(f"flopx:evaluation:v5:{ident}", lambda: _evaluate_ticker_uncached(db, ident), ttl_seconds=EVALUATION_CACHE_TTL_SECONDS, tags=None)
            for h in cached["evaluation"].horizons:
                aug = next((m for m in h.models if m.model == "augmented"), None)
                evaluation_rows.append({"ticker": ident, "horizonDays": h.horizon_days, "verdict": h.verdict, "n": h.origins,
                                        "skill": aug.skill_vs_baseline if aug else None, "inputKinds": cached["evaluation"].input_kinds})
        except Exception:
            logger.warning("evaluation unavailable for the brief", exc_info=True)
        exposure_rows = [e for e in exposures.get(region, []) if e.ticker == ident] if region else []
    else:
        model = data.gpu_models[ident]
        name = f"{model.vendor} {ident}"
        view_cells = [c for c in cells if c.gpu_model_id == ident]
        view_anoms = [a for a in found if a.gpu_model_id == ident]
        items = [i for i in inputs.feed if ident in i.entities.gpu_model_ids]
        markets = [m for m in inputs.markets if pressure.price_up_probability(m, ident, inputs.as_of)[0] is not None]
        ix = inputs.series.get(f"index:fcpi:{ident}") or inputs.series.get(f"gpu:{ident}:on-demand:global")
        if ix and ix.points:
            latest.append((f"{ident} comparable price", ix.points[-1]))
        exposure_rows = []
    brief = assistant.compose(target_kind=kind, target_id=ident, target_name=name, cells=view_cells, anomalies=view_anoms, supply=supply,
                              feed=items[:assistant.MAX_FEED], markets=markets, latest=latest, evaluation=evaluation_rows, exposures=exposure_rows)
    from infrastructure.settings import settings
    brief = assistant.draft(brief, getattr(settings, "ANTHROPIC_API_KEY", None))
    return {**brief.__dict__, "freshness": inputs.ledger.freshness(inputs.provenances())}
