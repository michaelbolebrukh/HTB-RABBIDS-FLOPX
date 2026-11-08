// What the tour cards say, composed from the live store at render time. Every figure here comes
// from a design export that the live adapters fill in place (src/live/**) and carries the freshness
// those adapters set; a figure the backend did not supply is reported as missing, never invented.
// These are plain functions over the exports, so a card re-reads them on every live bump.
import { useEffect, useState } from 'react'
import { ANOMALY_CARDS, GLOBAL_CPI, GPUS, NOW_IDX, OFFERS, PRESSURE_MODEL, REGIONS, type Freshness, type GpuLive, type Region, type Truth } from '@/data/core'
import { NEWS, NEWS_FEED_SUMMARY, PRED } from '@/data/finance'
import { ENERGY } from '@/data/infra'
import { AGENT_TOOLS, FUND_TESTS, SNAPSHOT } from '@/data/handoff'
import { W2_LIVE } from '@/live/adapt-w2'
import { fetchForecast, type ForecastOutcome } from '@/live/adapt-w5'
import { factorLabel, plainRegionName } from '@/live/adapt-w1'
import { fmtDay, fmtObserved } from '@/live/provenance'
import { allDomains, domain, type DomainState } from '@/live/store'
import { liveRegionName } from '@/live/regions'
import * as liveStore from '@/live/store'
import { getSources, type MarketTotals, type OfferTotals } from './sources'

/** One figure with its badge. `num` is the raw number for a count-up; `value` is the formatted text. */
export interface Fact {
  label: string
  value: string
  num?: number
  unit?: string
  fresh: Freshness
  freshLabel?: string
  truth?: Truth
  note?: string
}

export const SNAPSHOT_FRESH: Pick<Fact, 'fresh' | 'freshLabel'> = { fresh: 'historical', freshLabel: SNAPSHOT.short }

const median = (xs: number[]) => {
  if (!xs.length) return null
  const s = [...xs].sort((a, b) => a - b)
  const m = Math.floor(s.length / 2)
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2
}

// ---------- feed status ----------

export function feedStatus(): { state: DomainState; live: number; failed: number; pending: number; total: number } {
  const all = Object.values(allDomains())
  return { state: domain('gpu'), live: all.filter((d) => d.status === 'live').length, failed: all.filter((d) => d.status === 'failed').length, pending: all.filter((d) => d.status === 'pending').length, total: all.length }
}

// ---------- retrieval counters ----------

// The design arrays the adapters fill (OFFERS, PRED) keep the design's GPU, region and slot sets, so
// counting them under-reports what the backend sent (WF4 #4). The counters therefore read the raw
// payload totals: from the live store's LIVE_TOTALS (src/live/store.ts, written by the offers and
// markets adapters; read defensively because that module belongs to another workstream), else from
// the tour's own read of the same payloads (sources.ts), which also supplies the badge. Never the arrays.
// Both field spellings the other workstream has used are accepted (`count` or `live` for the live row count).
type StoreTotals = {
  liveOffers?: { count: number; providers: number; regions: number; models: number; total?: number; freshness?: Freshness } | null
  marketTotals?: { count?: number; live?: number; venues: number; total?: number; venueNames?: string[]; freshness?: Freshness } | null
}
const storeTotals = (): StoreTotals => ((liveStore as unknown as Record<string, unknown>).LIVE_TOTALS as StoreTotals | undefined) ?? {}
function storeOfferTotals(mine: OfferTotals | null): OfferTotals | null {
  const t = storeTotals().liveOffers ?? null
  if (!t || !Number.isFinite(t.count)) return mine
  return { count: t.count, providers: t.providers, regions: t.regions, models: t.models, total: t.total ?? mine?.total ?? t.count, freshness: t.freshness ?? mine?.freshness ?? (t.count ? 'live' : 'unavailable') }
}
function storeMarketTotals(mine: MarketTotals | null): MarketTotals | null {
  const t = storeTotals().marketTotals ?? null
  const count = t?.count ?? t?.live
  if (!t || count == null || !Number.isFinite(count)) return mine
  return { count, venues: t.venues, venueNames: t.venueNames ?? mine?.venueNames ?? [], total: t.total ?? mine?.total ?? count, freshness: t.freshness ?? mine?.freshness ?? (count ? 'live' : 'unavailable') }
}

/** Live offers as the payload counted them; `pending` while neither the store nor the tour has the payload yet. */
export function liveOffers(): { totals: OfferTotals | null; pending: boolean } {
  const src = getSources()
  const totals = storeOfferTotals(src.totals.offers)
  return { totals, pending: !totals && src.status !== 'failed' && src.status !== 'ready' }
}

export function liveRegions(): Region[] {
  return REGIONS.filter((r) => r.live?.pressure).sort((a, b) => b.live!.pressure!.score - a.live!.pressure!.score)
}

export function feedFacts() {
  const items = NEWS.filter((n) => n.freshness && n.freshness !== 'illustrative')
  return { live: NEWS_FEED_SUMMARY.live, headlines: NEWS_FEED_SUMMARY.live ? NEWS_FEED_SUMMARY.headlines : items.length, days: NEWS_FEED_SUMMARY.days, sources: NEWS_FEED_SUMMARY.sources, asOf: NEWS_FEED_SUMMARY.asOf, first: items[0] }
}

/** Live markets as the payload counted them (count and venues); `first` is the first live card the page shows. */
export function marketFacts(): { totals: MarketTotals | null; pending: boolean; first: (typeof PRED)[number] | undefined } {
  const live = PRED.filter((p) => p.freshness && p.freshness !== 'illustrative')
  const src = getSources()
  const totals = storeMarketTotals(src.totals.markets)
  return { totals, pending: !totals && src.status !== 'failed' && src.status !== 'ready', first: live[0] }
}

export function energyFacts() {
  const live = ENERGY.filter((e) => e.freshness !== 'illustrative')
  return { count: live.length, first: live[0], live, total: ENERGY.length }
}

export function companyFacts() {
  const all = Object.values(W2_LIVE.companies)
  const observed = all.filter((c) => c.close && c.close.fresh.freshness !== 'illustrative')
  const withFilings = all.filter((c) => c.revGrowth)
  return { count: all.length, observedCloses: observed.length, filings: withFilings.length, first: withFilings[0] }
}

/** The retrieval counters, each with its badge; `num` null when the feed has not supplied it. */
export function retrievalCounters(): Fact[] {
  const o = liveOffers()
  const r = liveRegions()
  const f = feedFacts()
  const m = marketFacts()
  const e = energyFacts()
  const c = companyFacts()
  const unavailable = (label: string, note: string): Fact => ({ label, value: 'n/a', fresh: 'unavailable', note })
  const counting = (label: string): Fact => ({ label, value: '…', fresh: 'delayed', freshLabel: 'Counting', note: 'reading the payload' })
  const ot = o.totals
  const mt = m.totals
  return [
    ot?.count ? { label: 'GPU asks observed', value: ot.count.toLocaleString('en-GB'), num: ot.count, fresh: ot.freshness, note: `${ot.providers} providers · ${ot.regions} regions · ${ot.models} GPU models · counted in /flopx/gpu/offers` } : o.pending ? counting('GPU asks observed') : unavailable('GPU asks observed', ot ? `payload carried ${ot.total} offers, none live` : 'offers payload not read'),
    r.length ? { label: 'Regions scored', value: String(r.length), num: r.length, fresh: r[0].live!.pressure!.freshness, note: PRESSURE_MODEL.method ?? 'cpx' } : unavailable('Regions scored', 'pressure feed not loaded'),
    f.headlines ? { label: 'Feed items', value: String(f.headlines), num: f.headlines, fresh: 'live', note: f.live ? `${f.sources.length} sources · ${f.days} days` : 'live items' } : unavailable('Feed items', 'news feed not loaded'),
    mt?.count ? { label: 'Prediction markets', value: String(mt.count), num: mt.count, fresh: mt.freshness, note: `${mt.venues} venue${mt.venues === 1 ? '' : 's'}${mt.venueNames.length ? ` (${mt.venueNames.map(venueLabel).join(', ')})` : ''} · counted in /flopx/markets` } : m.pending ? counting('Prediction markets') : unavailable('Prediction markets', mt ? `payload carried ${mt.total} markets, none live` : 'markets payload not read'),
    e.count ? { label: 'Power zones', value: String(e.count), num: e.count, fresh: e.first?.freshness ?? 'live', note: `of ${e.total} design markets` } : unavailable('Power zones', 'energy feed not loaded'),
    c.count ? { label: 'Companies', value: String(c.count), num: c.count, fresh: c.observedCloses ? 'historical' : 'illustrative', note: c.observedCloses ? `${c.observedCloses} with observed closes · ${c.filings} with filings` : `${c.filings} with filings · closes are sample data` } : unavailable('Companies', 'companies feed not loaded'),
  ]
}

// ---------- analysis: three transformations ----------

export interface Transform {
  id: string
  from: Fact
  to: Fact
  how: string
}

/** Raw ask -> comparable price; filing -> fundamental; market -> probability. Each only from live data. */
export function transforms(): Transform[] {
  const out: Transform[] = []
  const h100 = GPUS.find((g) => g.id === 'h100')
  const raw = OFFERS.filter((o) => o.live && o.gpu === 'h100' && o.contract === 'On-demand').map((o) => o.price)
  const rawMed = median(raw)
  if (h100?.live && rawMed != null) {
    out.push({
      id: 'ask',
      from: { label: 'Raw H100 asks (median of listed prices)', value: `$${rawMed.toFixed(2)}`, num: rawMed, unit: '/GPU-hr', fresh: 'live', truth: 'obs', note: `${raw.length} live on-demand asks, as listed` },
      to: { label: 'FLOPX H100 index', value: `$${h100.live.current.toFixed(2)}`, num: h100.live.current, unit: '/GPU-hr', fresh: h100.live.freshness, freshLabel: h100.live.freshLabel, truth: 'obs', note: `observed ${h100.live.observed} · ${h100.live.coverage.offerCount} asks in basket` },
      how: h100.live.methodology || 'median of adjusted asks',
    })
  }
  const c = companyFacts().first
  if (c?.revGrowth) {
    const rg = c.revGrowth
    out.push({
      id: 'filing',
      // Filing dates are date-only facts: never shown with a 00:00 clock (WF4 #15).
      from: { label: `${c.ticker} ${rg.latest.form} filed ${fmtDay(rg.latest.filedAt)}`, value: `revenue ${fmtBig(rg.latest.value)}`, fresh: rg.fresh.freshness, freshLabel: rg.fresh.label, truth: 'obs', note: `period to ${fmtDay(rg.latest.periodEnd)}` },
      to: { label: `${c.ticker} revenue growth, year on year`, value: `${rg.pct >= 0 ? '+' : ''}${rg.pct.toFixed(1)}%`, num: rg.pct, fresh: rg.fresh.freshness, freshLabel: rg.fresh.label, truth: 'inf', note: `vs ${fmtBig(rg.prior.value)} a year earlier` },
      how: 'same period a year earlier, from the filing itself',
    })
  }
  const m = marketFacts().first
  if (m) {
    out.push({
      id: 'market',
      from: { label: `${m.venue} YES price`, value: `${(m.prob * 100).toFixed(0)}¢`, num: m.prob * 100, fresh: m.freshness ?? 'live', truth: 'obs', note: m.updated ? `updated ${m.updated}` : undefined },
      to: { label: 'Implied probability', value: `${Math.round(m.prob * 100)}%`, num: Math.round(m.prob * 100), fresh: m.freshness ?? 'live', truth: 'fct', note: m.question },
      how: 'price of the YES contract, read as a probability; thin books are flagged',
    })
  }
  return out
}

const venueLabel = (v: string) => ({ polymarket: 'Polymarket', kalshi: 'Kalshi' } as Record<string, string>)[v] ?? v

function fmtBig(v: number): string {
  const a = Math.abs(v)
  return a >= 1e9 ? `$${(v / 1e9).toFixed(2)}bn` : a >= 1e6 ? `$${(v / 1e6).toFixed(1)}m` : `$${v.toLocaleString('en-GB')}`
}

// ---------- modelling: the X-Ray's factors ----------

export interface FactorRow {
  id: string
  label: string
  weight: number
  contribution: number | null
  note: string
  fresh: Freshness
}

/** The design id of the fallback region when no scored cell carries factor data: US East and Midwest (Ashburn hub). */
export const FALLBACK_REGION_ID = 'us-va'

/**
 * The region the modelling and decision beats stand on: the scored cell with the most factors
 * carrying data (ties broken by score), never one whose six factors all read "no data" (N-TOUR2 #2).
 * `why` is the one line the card prints. Falls back to US East and Midwest when nothing has data.
 */
export function pickRegion(regions: Region[] = liveRegions()): { region: Region | null; why: string; withData: number; scored: number } {
  const withData = (r: Region) => r.live?.pressure?.factors.filter((f) => f.contribution != null).length ?? 0
  const scored = regions.length
  const ranked = regions.filter((r) => withData(r) > 0).sort((a, b) => withData(b) - withData(a) || b.live!.pressure!.score - a.live!.pressure!.score)
  const top = ranked[0]
  if (top) {
    const k = withData(top)
    const total = top.live!.pressure!.factorsTotal || top.live!.pressure!.factors.length || 6
    const tied = ranked.filter((r) => withData(r) === k).length
    return { region: top, withData: k, scored, why: `${k} of ${total} factors carry data, the most of the ${scored} scored regions${tied > 1 ? ` (${tied} tie; highest score wins)` : ''}` }
  }
  const fb = regions.find((r) => r.live?.backendId === FALLBACK_REGION_ID || r.id === FALLBACK_REGION_ID) ?? REGIONS.find((r) => r.id === FALLBACK_REGION_ID) ?? null
  return { region: fb, withData: 0, scored, why: scored ? `none of the ${scored} scored regions has a factor with data tonight; US East and Midwest is the stated fallback` : 'the pressure feed has not landed; US East and Midwest is the stated fallback' }
}

/** The region the tours stand on (see `pickRegion`): most factors with data, else US East and Midwest. */
export function topRegion(): Region | null {
  return pickRegion().region
}

/** The cpx-v1 cell behind a region's headline score: six factors, weights and contributions, as the backend states them. */
export function factorsOf(r: Region | null): { region: Region; name: string; score: number; band: string; label: string; fresh: Freshness; coverage: number; realShare: number; factorsWithData: number; factorsTotal: number; factors: FactorRow[]; asOf: string } | null {
  const p = r?.live?.pressure
  if (!r || !p) return null
  return {
    region: r,
    name: r.live?.backendId ? liveRegionName(r.live.backendId) : r.name,
    score: p.score,
    band: p.band,
    label: p.label,
    fresh: p.freshness,
    coverage: p.coverage,
    // The backend's `coverage` counts factors with data, not real inputs; realness is 1 - sampleShare (WF4 #7).
    realShare: Number.isFinite(p.sampleShare) ? Math.max(0, Math.min(1, 1 - p.sampleShare)) : 0,
    factorsWithData: p.factorsWithData,
    factorsTotal: p.factorsTotal || p.factors.length,
    asOf: fmtObserved(r.live!.asOf),
    factors: p.factors.map((f) => ({ id: f.factor, label: factorLabel(f.factor), weight: f.weight, contribution: f.contribution, note: f.note, fresh: f.freshness })),
  }
}

/**
 * The badge a mean of region cells deserves: the cells' own freshness, and illustrative when most of
 * their factor inputs are sample data (any cell with sampleShare over 0.5), never a hardcoded live (WF4 #8).
 */
export function pressureMeanFreshness(regions: Region[]): { fresh: Freshness; label?: string; sampleFed: number; exploratory: number } {
  const cells = regions.map((r) => r.live!.pressure!)
  const sampleFed = cells.filter((c) => c.sampleShare > 0.5).length
  const exploratory = cells.filter((c) => c.label === 'exploratory').length
  if (!cells.length) return { fresh: 'unavailable', sampleFed, exploratory }
  if (cells.some((c) => c.freshness === 'illustrative') || sampleFed > 0) return { fresh: 'illustrative', sampleFed, exploratory }
  if (cells.every((c) => c.freshness === 'live')) return { fresh: 'live', label: 'Live (derived)', sampleFed, exploratory }
  return { fresh: 'historical', sampleFed, exploratory }
}

export function globalPressure(): Fact {
  const v = GLOBAL_CPI[NOW_IDX]
  const regions = liveRegions()
  if (PRESSURE_MODEL.globalReal && regions.length) {
    const f = pressureMeanFreshness(regions)
    const why = f.sampleFed ? `${f.sampleFed} of ${regions.length} cells mostly sample-fed` : `${f.exploratory} of ${regions.length} exploratory`
    return { label: 'Compute pressure, mean of FLOPX regions', value: v.toFixed(1), num: v, fresh: f.fresh, freshLabel: f.label, truth: 'inf', note: `${PRESSURE_MODEL.method} · ${PRESSURE_MODEL.regions} regions · H100 cells · ${why}` }
  }
  return { label: 'Compute pressure (design value)', value: v.toFixed(1), num: v, fresh: 'illustrative', truth: 'inf', note: 'pressure feed not loaded' }
}

export function anomalyFacts() {
  return { live: PRESSURE_MODEL.anomaliesLive, cards: ANOMALY_CARDS, first: ANOMALY_CARDS[0] }
}

// ---------- estimation and honesty ----------

/** The backend evaluation harness: per horizon, how many tickers the compute-augmented model improves, is inconclusive for, or worsens. */
export function evaluationFacts() {
  const e = W2_LIVE.evaluation
  if (!e) return null
  const h = e.augmented['1M']
  // The badge follows the payload's price basis (src/live/evalBasis.ts), the same source Eval.tsx uses (WF4 #5).
  return { tickers: e.tickers.length, improves: h?.improves ?? 0, inconclusive: h?.inconclusive ?? 0, worsens: h?.worsens ?? 0, insufficient: h?.['insufficient-sample'] ?? 0, fresh: e.fresh, basis: e.priceBasis, reliability: e.reliabilityNote, generatedAt: fmtObserved(e.generatedAt), byHorizon: e.augmented }
}

export const fundTests = () => ({ edges: FUND_TESTS.edges, tests: FUND_TESTS.tests, tickers: FUND_TESTS.tickers, exploratory: FUND_TESTS.exploratoryOrders, ...SNAPSHOT_FRESH })

/** The H100 index forecast as the backend answers today: usually a refusal naming the history it has against what it needs. */
export function useForecastOutcome(model = 'h100-sxm-80gb'): ForecastOutcome | null {
  const [o, setO] = useState<ForecastOutcome | null>(null)
  useEffect(() => {
    let alive = true
    void fetchForecast(model).then((r) => { if (alive) setO(r) })
    return () => { alive = false }
  }, [model])
  return o
}

export function forecastFacts(o: ForecastOutcome | null): { state: 'pending' | 'refused' | 'ok' | 'failed'; headline: string; detail: string; have: number; need: number; fresh: Freshness } {
  if (!o) return { state: 'pending', headline: 'Asking FLOPX for a 30-day H100 forecast', detail: 'POST /flopx/forecast', have: 0, need: 60, fresh: 'unavailable' }
  if (o.kind === 'refused') {
    const have = o.detail.history_points ?? 0
    const need = o.detail.required_points ?? 2 * (o.detail.horizon ?? 30)
    return { state: 'refused', headline: `Refused: ${have} of ${need} daily points of history`, detail: o.detail.reason ?? o.detail.code, have, need, fresh: 'unavailable' }
  }
  if (o.kind === 'ok') {
    const d = o.data
    const b = d.baselines.find((x) => x.method === d.chosen) ?? d.baselines[0]
    const last = b?.points[b.points.length - 1]
    return { state: 'ok', headline: `${d.horizon}-day forecast, ${d.chosen}${d.exploratory ? ' (exploratory)' : ''}`, detail: last ? `mean ${last.mean.toFixed(2)}, band ${last.lower.toFixed(2)} to ${last.upper.toFixed(2)} at ${Math.round(d.level * 100)}%` : d.caveats.join(' '), have: d.historyPoints, need: 2 * d.horizon, fresh: d.exploratory ? 'experimental' : 'live' }
  }
  return { state: 'failed', headline: 'Forecast endpoint did not answer', detail: o.error, have: 0, need: 60, fresh: 'unavailable' }
}

// ---------- decision ----------

export const agentToolCount = () => AGENT_TOOLS.length

// ---------- review: what is real today ----------

export interface RealityRow { area: string; status: 'real' | 'partly' | 'illustrative'; note: string; fresh: Freshness }

export function realityRows(): RealityRow[] {
  const d = allDomains()
  const st = (id: string) => d[id]?.status ?? 'pending'
  const o = liveOffers()
  const r = liveRegions()
  const h100 = GPUS.find((g) => g.id === 'h100')?.live
  const f = feedFacts()
  const m = marketFacts()
  const e = energyFacts()
  const c = companyFacts()
  const ev = evaluationFacts()
  const ot = o.totals
  const mt = m.totals
  const rows: RealityRow[] = [
    ot?.count ? { area: 'GPU asks', status: 'real', note: `${ot.count} live asks in the offers payload (${ot.providers} providers, ${ot.regions} regions, ${ot.models} GPU models)`, fresh: ot.freshness } : { area: 'GPU asks', status: 'illustrative', note: st('gpu') === 'failed' ? 'offers feed failed; design asks shown' : o.pending ? 'offers payload being counted' : 'no live offers in the payload', fresh: 'unavailable' },
    // The index is its own row with its own badge: the gpu/index payload can be derived from sample
    // inputs (reads Illustrative, as the ticker shows it) while the offers beside it are live (WF5 #7).
    indexRow(h100),
    r.length ? { area: 'Compute pressure', status: 'partly', note: `${r.length} regions scored by ${PRESSURE_MODEL.method}; ${r.filter((x) => x.live!.pressure!.label === 'exploratory').length} exploratory because most factor inputs are sample data; history before this month is the design's`, fresh: pressureMeanFreshness(r).fresh } : { area: 'Compute pressure', status: 'illustrative', note: 'pressure feed not loaded', fresh: 'unavailable' },
    e.count ? { area: 'Power prices', status: e.count < e.total ? 'partly' : 'real', note: `${e.count} of ${e.total} zones live (${e.live.map((z) => z.name).join(', ')}); the rest${e.live.some((z) => z.id === 'ercot') ? '' : ', ERCOT included,'} are design values`, fresh: e.first?.freshness ?? 'live' } : { area: 'Power prices', status: 'illustrative', note: 'energy feed not loaded', fresh: 'unavailable' },
    f.headlines ? { area: 'News feed', status: 'real', note: `${f.headlines} items from ${f.sources.length} sources over ${f.days} days`, fresh: 'live' } : { area: 'News feed', status: 'illustrative', note: 'feed not loaded; authored items shown', fresh: 'unavailable' },
    mt?.count ? { area: 'Prediction markets', status: 'real', note: `${mt.count} live markets from ${mt.venues} venue${mt.venues === 1 ? '' : 's'}${mt.venueNames.length ? ` (${mt.venueNames.map(venueLabel).join(', ')})` : ''}, counted in the payload`, fresh: mt.freshness } : { area: 'Prediction markets', status: 'illustrative', note: m.pending ? 'markets payload being counted' : 'no live markets in the payload', fresh: 'unavailable' },
    c.count ? { area: 'Companies and filings', status: c.observedCloses ? 'partly' : 'partly', note: `${c.filings} with SEC EDGAR revenue; closes ${c.observedCloses ? `observed for ${c.observedCloses}` : 'are sample data (Yahoo rate-limited)'}`, fresh: c.observedCloses ? 'historical' : 'illustrative' } : { area: 'Companies and filings', status: 'illustrative', note: 'companies feed not loaded', fresh: 'unavailable' },
    { area: 'Evaluation harness', status: ev ? (ev.basis.kind === 'sample' ? 'partly' : 'real') : 'illustrative', note: ev ? `${ev.tickers} tickers ${ev.basis.caption}; the verdicts are the harness's own outputs` : 'evaluation not loaded', fresh: ev ? ev.basis.freshness : 'unavailable' },
    { area: 'Fund, paper orders, proposals', status: 'illustrative', note: 'the CPX-1 portfolio, its signals and proposals are the design’s worked example; execution is simulated', fresh: 'simulated' },
    { area: 'Intelligence market', status: 'real', note: 'OpenRouter snapshot from the 7 Nov engine build', fresh: 'historical' },
  ]
  return rows
}

/** The H100 index's review row, badged from the index payload's own provenance, never from the offers count beside it. */
export function indexRow(h100: GpuLive | undefined | null): RealityRow {
  if (!h100) return { area: 'H100 index', status: 'illustrative', note: 'gpu/index not loaded; the design series shows', fresh: 'unavailable' }
  const points = `$${h100.current.toFixed(2)}, ${h100.historyPoints} history point${h100.historyPoints === 1 ? '' : 's'}`
  if (h100.freshness === 'illustrative') return { area: 'H100 index', status: 'illustrative', note: `${points} · derived from sample inputs by the backend, so it reads illustrative here as on the ticker`, fresh: 'illustrative' }
  if (h100.freshness === 'live') return { area: 'H100 index', status: 'real', note: `${points} · observed ${h100.observed}`, fresh: 'live' }
  return { area: 'H100 index', status: 'partly', note: `${points} · ${h100.freshLabel ?? h100.freshness}`, fresh: h100.freshness }
}

export function nextSteps(): string[] {
  const steps: string[] = []
  const h100 = GPUS.find((g) => g.id === 'h100')?.live
  if (h100 && h100.historyPoints < 60) steps.push(`Accumulate ${60 - h100.historyPoints} more daily index points so the forecast and the timing gate can open`)
  if (!energyFacts().count || energyFacts().count < ENERGY.length) steps.push('Add ERCOT and PJM day-ahead history from a public source (EIA) so power stress reads real in Texas and Virginia')
  const ev = evaluationFacts()
  if (!ev || ev.basis.kind === 'sample') steps.push('Replace sample closes with observed ones (WRDS CRSP, then a licensed daily feed) so the evaluation scores the market, not a sample')
  else if (ev.basis.kind === 'archived') steps.push(`Extend the ${ev.basis.short} past the archive's last date so the evaluation scores the current market`)
  steps.push('Replace design regions that FLOPX does not cover with scored cells, or hide them')
  return steps
}

export { plainRegionName }
