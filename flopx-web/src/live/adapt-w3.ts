// W3 adapters: energy, news and prediction markets.
//
// Each adapt* function is pure: backend payload in, the design's own types out. Each apply*
// function writes the result into the design's data exports in place (same array objects, so
// every importer sees it) and leaves re-rendering to the boot loader's bump. Nothing here invents
// a number: a design field the backend does not carry is either kept and marked illustrative, or
// set to null so the page shows a dash.
//
// Region ids: the backend speaks hub ids ('eu-ie'), the design speaks its own ('ie-dub'). Items
// keep a design id when the design (or a live REGIONS list) has the mapped id, otherwise the
// backend id, which core.regionById resolves to a named stub instead of crashing.
import type { Freshness } from '@/data/core'
import { REGIONS } from '@/data/core'
import { NEWS, NEWS_FEED_SUMMARY, PRED, secByTicker, type NewsItem, type NewsType, type PredMarket } from '@/data/finance'
import { ENERGY, type EnergyMarket } from '@/data/infra'
import type { LiveModule } from './boot'
import { joinToNow, placeOnAxis } from './dates'
import { fmtAsOf, freshOf } from './provenance'
import { setLiveTotals, type MarketTotals } from './store'
import type { FreshnessBlock, Provenance, SeriesPoint } from './types'

// ---------- wire types (only the fields read here; source of truth /openapi.json) ----------

export interface EnergyZoneWire {
  zone: string
  seriesId: string
  unit: string
  latest: number | null
  latestAt: string | null
  median30D: number | null
  changePct30D: number | null
  points: SeriesPoint[]
  regions: string[]
  provenance: Provenance
  statementClass?: string
}

export interface InfraEnergyResponse {
  zones: EnergyZoneWire[]
  missing: { zone: string; regions: string[]; reason: string }[]
  freshness: FreshnessBlock
}

export interface FeedItemWire {
  id: string
  kind: string
  headline: string
  whyItMatters: string
  publishedAt: string
  observedAt?: string
  severity: number
  classification: 'verified' | 'interpretation' | 'prediction' | string
  provenance: Provenance
  geography: { regionIds: string[] }
  entities: { tickers: string[]; facilityIds: string[]; gpuModelIds: string[]; marketIds: string[] }
  evidence: { label: string; href: string; kind: string }[]
}

export interface FeedResponse {
  items: FeedItemWire[]
  freshness: FreshnessBlock
}

export interface MarketWire {
  id: string
  venue: string
  question: string
  outcomes: { label: string; price: number | null; impliedProbability: number | null }[]
  lastUpdated: string | null
  provenance: Provenance
  resolutionRules: string
  liquidity: number | null
  volume24H: number | null
  history: SeriesPoint[]
  linkedEntities: { tickers: string[]; regionIds: string[]; facilityIds: string[]; gpuModelIds: string[] }
}

export interface MarketsResponse {
  markets: MarketWire[]
  freshness: FreshnessBlock
}

// ---------- shared helpers ----------

/** Backend hub id -> the design's region id for the same place. */
export const BACKEND_TO_DESIGN_REGION: Record<string, string> = {
  'us-tx': 'us-tx',
  'us-va': 'us-va',
  'us-or': 'us-or',
  'us-az': 'us-az',
  'eu-ie': 'ie-dub',
  'eu-no': 'no-nordics',
  'eu-de': 'de-fra',
  'eu-nl': 'nl-ams',
  'eu-uk': 'uk-lon',
  'ap-sg': 'sg',
  'ap-jp': 'jp-tyo',
  'ap-au': 'au-syd',
  'ap-in': 'in-mum',
  'ca-qc': 'ca-qc',
}

/** Map a backend region id to the id the current REGIONS list knows, else keep the backend id. */
export function mapRegion(id: string, known: (id: string) => boolean): string {
  if (known(id)) return id
  const d = BACKEND_TO_DESIGN_REGION[id]
  return d && known(d) ? d : id
}

const knownRegion = (id: string) => REGIONS.some((r) => r.id === id)

const DAY = 86_400_000

/** "18d", the design's `ago` format (whole days, never negative). */
export function agoOf(iso: string, now: Date): string {
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return '0d'
  return `${Math.max(0, Math.floor((now.getTime() - t) / DAY))}d`
}

const nowOf = (f: FreshnessBlock | undefined) => {
  const d = f?.generatedAt ? new Date(f.generatedAt) : new Date()
  return Number.isNaN(d.getTime()) ? new Date() : d
}

// ---------- energy ----------

/** Backend energy zone -> design ENERGY row id. Zones the design has no row for are ignored. */
export const ZONE_TO_ENERGY: Record<string, string> = {
  ERCOT: 'ercot',
  PJM: 'pjm',
  BPA: 'bpa',
  EirGrid: 'sem',
  'DE-LU': 'de',
  NO1: 'nordpool',
  EMA: 'sg',
  TEPCO: 'jepx',
  'GB': 'gb',
  'NL': 'nl',
}

const currencyOf = (unit: string) => (unit.toUpperCase().startsWith('EUR') ? 'EUR' : 'USD')

/**
 * One backend zone applied to its design row.
 * - priceNow, chg30 and the price series come from the backend.
 * - The month-axis series follows the README placement rule (last point per calendar month;
 *   months with no backend point are flagged not-real in `real`) and then G14: those months keep
 *   the design's shape, multiplied so the curve joins the backend months, and `scaled` says so for
 *   the page's badge.
 * - Fields the backend does not carry for a zone (scarcity hours, reserve margin, queue) become
 *   null on an observed row, and the constraint line states the source instead of a mock claim.
 */
export function adaptEnergyZone(z: EnergyZoneWire, row: EnergyMarket, now: Date, seriesPoints?: SeriesPoint[]): EnergyMarket {
  const tag = freshOf(z.provenance)
  const observed = tag.freshness !== 'illustrative'
  const pts = (seriesPoints && seriesPoints.length ? seriesPoints : z.points).map((p) => ({ t: p.t, value: p.value }))
  // G14: backend months replace the design's; the design's other months are multiplied so the
  // curve joins them (src/live/README.md).
  const placed = joinToNow(placeOnAxis(row.price, pts, now), row.price, 'multiply')
  const priceNow = z.latest ?? row.priceNow
  const src = z.provenance.source
  const when = z.latestAt ? fmtAsOf(z.latestAt) : ''
  return {
    ...row,
    currency: currencyOf(z.unit),
    price: placed.values,
    real: placed.real,
    scaled: placed.scaled,
    priceNow,
    chg30: z.changePct30D ?? (observed ? NaN : row.chg30),
    scarcityHours: observed ? null : row.scarcityHours,
    reserveMargin: observed ? null : row.reserveMargin,
    queueGW: observed ? null : row.queueGW,
    constraint: observed
      ? `Latest day-ahead ${z.zone} price from ${src}${when ? ` at ${when}` : ''}${z.provenance.licence ? ` (${z.provenance.licence})` : ''}. FLOPX does not track grid constraints for this zone yet.`
      : row.constraint,
    freshness: tag.freshness,
    sourceNote: z.provenance.note ?? `${src}, ${z.unit}`,
  }
}

/** All zones applied to a copy of the design rows; rows with no zone stay as they are (illustrative). */
export function adaptEnergy(resp: InfraEnergyResponse, rows: EnergyMarket[], series: Record<string, SeriesPoint[]> = {}): EnergyMarket[] {
  const now = nowOf(resp.freshness)
  const byRow = new Map<string, EnergyZoneWire>()
  for (const z of resp.zones ?? []) {
    const id = ZONE_TO_ENERGY[z.zone]
    if (id) byRow.set(id, z)
  }
  return rows.map((r) => {
    const z = byRow.get(r.id)
    return z ? adaptEnergyZone(z, r, now, series[z.seriesId]) : r
  })
}

export function applyEnergy(rows: EnergyMarket[]): void {
  rows.forEach((r) => {
    const i = ENERGY.findIndex((e) => e.id === r.id)
    if (i >= 0) ENERGY[i] = r
  })
}

// ---------- news feed ----------

const SOURCE_LABEL: Record<string, string> = {
  'sec-edgar': 'SEC EDGAR filing',
  'hacker-news': 'Hacker News discussion',
  'rss:nvidia': 'NVIDIA newsroom',
  'rss:coreweave': 'CoreWeave newsroom',
  'rss:corescientific': 'Core Scientific newsroom',
}

export function sourceLabel(source: string): string {
  if (SOURCE_LABEL[source]) return SOURCE_LABEL[source]
  if (source.startsWith('rss:')) return `${source.slice(4)} newsroom`
  return source
}

/** The design's five event types; company and filing items are Corporate, discussion is News. */
export function newsTypeOf(it: FeedItemWire): NewsType {
  if (it.kind === 'policy') return 'Regulatory'
  if (it.kind === 'prediction-reprice') return 'Prediction market'
  if (it.provenance.source === 'hacker-news') return 'News'
  if (it.provenance.source === 'sec-edgar' || it.provenance.source.startsWith('rss:')) return 'Corporate'
  return 'News'
}

export function impactOf(severity: number): NewsItem['impact'] {
  return severity >= 4 ? 'high' : severity >= 3 ? 'medium' : 'low'
}

/** Verification follows the source, never the headline: a filing is a filing, a company feed is one source. */
export function verificationOf(it: FeedItemWire): NewsItem['verification'] {
  if (it.provenance.source === 'sec-edgar') return 'Confirmed by filing'
  if (it.classification === 'verified' && it.provenance.source.startsWith('rss:')) return 'Single source'
  return 'Unverified'
}

export interface FeedOpts {
  now?: Date
  knownRegion?: (id: string) => boolean
  knownTicker?: (t: string) => boolean
}

/**
 * Backend feed -> design NewsItems. Authored sample items (provenance 'sample') are dropped: the
 * News page shows every card as an observed event, so sample events must not appear there.
 */
export function adaptFeed(resp: FeedResponse, opts: FeedOpts = {}): NewsItem[] {
  const now = opts.now ?? nowOf(resp.freshness)
  const kr = opts.knownRegion ?? knownRegion
  const kt = opts.knownTicker ?? ((t: string) => !!secByTicker(t))
  return (resp.items ?? [])
    .filter((it) => freshOf(it.provenance).freshness !== 'illustrative')
    .map((it) => ({
      id: `feed:${it.id}`,
      type: newsTypeOf(it),
      title: it.headline,
      source: sourceLabel(it.provenance.source),
      time: fmtAsOf(it.publishedAt),
      ago: agoOf(it.publishedAt, now),
      summary: it.whyItMatters,
      regions: Array.from(new Set((it.geography?.regionIds ?? []).map((r) => mapRegion(r, kr)))),
      tickers: (it.entities?.tickers ?? []).filter(kt),
      impact: impactOf(it.severity),
      verification: verificationOf(it),
      url: it.provenance.sourceUrl ?? it.evidence?.[0]?.href,
      freshness: freshOf(it.provenance).freshness,
    }))
    .sort((a, b) => parseInt(a.ago, 10) - parseInt(b.ago, 10))
}

export function applyFeed(items: NewsItem[], summary?: typeof NEWS_FEED_SUMMARY): void {
  if (!items.length) return
  NEWS.splice(0, NEWS.length, ...items)
  if (summary) Object.assign(NEWS_FEED_SUMMARY, summary)
}

/** Headline count, day span, sources and most-mentioned tickers of the adapted feed. */
export function summariseFeed(items: NewsItem[], resp: FeedResponse): typeof NEWS_FEED_SUMMARY {
  const now = nowOf(resp.freshness)
  const oldest = Math.max(...items.map((n) => parseInt(n.ago, 10)), 0)
  const counts = new Map<string, number>()
  items.forEach((n) => n.tickers.forEach((t) => counts.set(t, (counts.get(t) ?? 0) + 1)))
  return {
    live: items.length > 0,
    headlines: items.length,
    days: oldest + 1,
    sources: Array.from(new Set(items.map((n) => n.source))),
    themes: [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 5).map(([t]) => t),
    asOf: fmtAsOf(now),
  }
}

// ---------- prediction markets ----------

const VENUE_LABEL: Record<string, string> = { polymarket: 'Polymarket', kalshi: 'Kalshi' }

/** The YES price; Kalshi labels it "Yes (Above 93,000 MW)". */
export function yesPrice(m: MarketWire): number | null {
  const y = m.outcomes.find((o) => /^yes\b/i.test(o.label))
  const v = y?.price ?? y?.impliedProbability
  return v == null || !Number.isFinite(v) ? null : v
}

const MONTHS: Record<string, string> = { january: 'Jan', february: 'Feb', march: 'Mar', april: 'Apr', may: 'May', june: 'Jun', july: 'Jul', august: 'Aug', september: 'Sep', october: 'Oct', november: 'Nov', december: 'Dec' }

/** Close date as stated in the question ("on December 31, 2026" -> "31 Dec 2026"); null when the question states none. */
export function closesOf(question: string): string | null {
  const m = question.match(/\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2}),\s*(20\d\d)/i)
  if (m) return `${m[2].padStart(2, '0')} ${MONTHS[m[1].toLowerCase()]} ${m[3]}`
  const y = question.match(/\b(?:during|for|in)\s+(20\d\d)\b/i)
  return y ? `31 Dec ${y[1]}` : null
}

/** Question with the numbers stripped: markets on one ladder share a family. */
export const familyOf = (q: string) => q.replace(/\$?[\d.,]+/g, '#').replace(/\b(at least|between|less than|above|below|hit)\b.*?(on|by|for|during)\b/i, '$2').toLowerCase()

export interface MarketOpts extends FeedOpts {
  /** How many cards to show (the design shows four). */
  limit?: number
  /** Markets last updated longer ago than this are skipped as stale. */
  maxAgeDays?: number
  /** Below this depth (USD) a card carries the design's thin-book caution. */
  thinBookUsd?: number
}

/**
 * Backend markets -> the design's prediction-market cards. Only live venues are shown; the card
 * set is the most-traded market of each question family, so a single price ladder does not fill
 * every slot. A market with no history gets an empty spark and a null 7-day change (shown as
 * "no 7d history"), never an invented line.
 */
export function adaptMarkets(resp: MarketsResponse, opts: MarketOpts = {}): PredMarket[] {
  const now = opts.now ?? nowOf(resp.freshness)
  const kr = opts.knownRegion ?? knownRegion
  const kt = opts.knownTicker ?? ((t: string) => !!secByTicker(t))
  const limit = opts.limit ?? 4
  const maxAge = (opts.maxAgeDays ?? 14) * DAY
  const thin = opts.thinBookUsd ?? 5000
  const live = (resp.markets ?? []).filter((m) => {
    if (freshOf(m.provenance).freshness === 'illustrative') return false
    if (yesPrice(m) == null) return false
    const t = m.lastUpdated ? Date.parse(m.lastUpdated) : NaN
    return Number.isFinite(t) && now.getTime() - t <= maxAge
  })
  const score = (m: MarketWire) => (m.volume24H ?? 0) * 1e6 + (m.liquidity ?? 0)
  const best = new Map<string, MarketWire>()
  for (const m of live) {
    const k = `${m.venue}|${familyOf(m.question)}`
    const cur = best.get(k)
    if (!cur || score(m) > score(cur)) best.set(k, m)
  }
  // Interleave venues so one venue's ladders do not take every card.
  const ranked = [...best.values()].sort((a, b) => score(b) - score(a))
  const byVenue = new Map<string, MarketWire[]>()
  ranked.forEach((m) => byVenue.set(m.venue, [...(byVenue.get(m.venue) ?? []), m]))
  const picked: MarketWire[] = []
  while (picked.length < limit && [...byVenue.values()].some((l) => l.length)) {
    for (const l of byVenue.values()) if (l.length && picked.length < limit) picked.push(l.shift()!)
  }
  return picked.map((m) => {
    const prob = yesPrice(m)!
    const hist = [...(m.history ?? [])].sort((a, b) => Date.parse(a.t) - Date.parse(b.t)).map((p) => p.value)
    const weekAgo = [...(m.history ?? [])].filter((p) => Date.parse(p.t) <= now.getTime() - 7 * DAY).sort((a, b) => Date.parse(b.t) - Date.parse(a.t))[0]
    const liq = m.liquidity ?? null
    const caution = liq != null && liq < thin ? `Thin liquidity: $${(liq / 1000).toFixed(1)}k depth. A single trader could move this price.` : undefined
    return {
      id: `pm:${m.id}`,
      question: m.question,
      venue: VENUE_LABEL[m.venue] ?? m.venue,
      prob,
      probHist: hist,
      chg7: weekAgo ? Math.round((prob - weekAgo.value) * 100) : null,
      volume: m.volume24H ?? null,
      liquidity: liq ?? 0,
      spread: null,
      closes: closesOf(m.question) ?? 'not stated',
      regions: Array.from(new Set((m.linkedEntities?.regionIds ?? []).map((r) => mapRegion(r, kr)))),
      tickers: (m.linkedEntities?.tickers ?? []).filter(kt),
      resolution: m.resolutionRules,
      caution,
      url: m.provenance.sourceUrl ?? undefined,
      freshness: freshOf(m.provenance).freshness,
      updated: m.lastUpdated ? fmtAsOf(m.lastUpdated) : undefined,
    }
  })
}

export function applyMarkets(items: PredMarket[]): void {
  if (!items.length) return
  PRED.splice(0, PRED.length, ...items)
}

/**
 * Payload totals: every non-sample market row and its distinct venues, before the card selection
 * above narrows the list to four. Read by the tour's retrieval counters (WF4 #4), which otherwise
 * counted the four design cards and one venue against a payload of 90 live rows from two venues.
 */
export function marketTotals(resp: MarketsResponse): MarketTotals {
  const rows = resp.markets ?? []
  const live = rows.filter((m) => freshOf(m.provenance).freshness !== 'illustrative')
  const venueNames = [...new Set(live.map((m) => m.venue))].sort()
  const kinds = new Set(live.map((m) => m.provenance.kind))
  return { live: live.length, count: live.length, venues: venueNames.length, venueNames, total: rows.length, freshness: !live.length ? 'unavailable' : kinds.has('live') ? 'live' : 'historical' }
}

/** Live binding of the market totals (the same object as LIVE_TOTALS.marketTotals), null until the payload lands. */
export let MARKET_TOTALS: MarketTotals | null = null

// ---------- boot modules ----------


/** The energy series the backend carries are long enough to place on the month axis. */
const ENERGY_SERIES_TIMEOUT_MS = 9_000

export const energyModule: LiveModule = {
  id: 'energy',
  load: async (get, apply) => {
    const r = await get<InfraEnergyResponse>('/flopx/infra/energy')
    if (!r.ok) throw new Error(r.error)
    applyEnergy(adaptEnergy(r.data, ENERGY))
    apply()
    // Longer per-zone history (when available) refines the month-axis series.
    const series: Record<string, SeriesPoint[]> = {}
    await Promise.all(
      r.data.zones.map(async (z) => {
        const s = await get<{ points: SeriesPoint[] }>(`/flopx/gpu/series?id=${encodeURIComponent(z.seriesId)}`, { timeoutMs: ENERGY_SERIES_TIMEOUT_MS })
        if (s.ok && s.data.points?.length) series[z.seriesId] = s.data.points
      }),
    )
    if (Object.keys(series).length) applyEnergy(adaptEnergy(r.data, ENERGY, series))
    return true
  },
}

export const feedModule: LiveModule = {
  id: 'news',
  load: async (get) => {
    const r = await get<FeedResponse>('/flopx/feed')
    if (!r.ok) throw new Error(r.error)
    const items = adaptFeed(r.data)
    applyFeed(items, summariseFeed(items, r.data))
    return items.length > 0
  },
}

export const marketsModule: LiveModule = {
  id: 'markets',
  load: async (get) => {
    const r = await get<MarketsResponse>('/flopx/markets')
    if (!r.ok) throw new Error(r.error)
    MARKET_TOTALS = marketTotals(r.data)
    setLiveTotals({ marketTotals: MARKET_TOTALS })
    const items = adaptMarkets(r.data)
    applyMarkets(items)
    return items.length > 0
  },
}

/** Register these with the boot loader (main.tsx). */
export const w3Modules: LiveModule[] = [energyModule, feedModule, marketsModule]

/** Freshness of the whole news list, for the page's section badges. */
export const listFreshness = (xs: { freshness?: Freshness }[]): Freshness => (xs.some((x) => (x.freshness ?? 'illustrative') === 'illustrative') ? 'illustrative' : 'live')
