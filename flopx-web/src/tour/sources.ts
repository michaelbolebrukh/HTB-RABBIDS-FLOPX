// The sources behind the picture, read from the backend's own freshness blocks. Each FLOPX payload
// carries `freshness.sourceDetails` (source id, provenance kind, last observation, licence) and
// `freshness.unavailable`. The tour merges those across the reads it cares about into one list and
// groups them into the six source families the pitch talks about. Nothing here is authored: a
// family with no live source says so.
import { useSyncExternalStore } from 'react'
import { fetchJson } from '@/live/client'
import { freshOf } from '@/live/provenance'
import type { Freshness } from '@/data/core'
import type { FreshnessBlock, OfferWire, ProvenanceKind } from '@/live/types'

export type Family = 'asks' | 'power' | 'filings' | 'markets' | 'news' | 'facilities'

export const FAMILIES: { id: Family; label: string; what: string }[] = [
  { id: 'asks', label: 'GPU asks', what: 'Advertised rental prices and availability' },
  { id: 'power', label: 'Power', what: 'Day-ahead electricity prices by grid zone' },
  { id: 'filings', label: 'Filings', what: 'Company reports and registries' },
  { id: 'markets', label: 'Markets', what: 'Prediction markets and equity closes' },
  { id: 'news', label: 'News', what: 'Press releases and community feeds' },
  { id: 'facilities', label: 'Facilities', what: 'Data-centre records and sample sets' },
]

/** Source id -> family. Unknown ids land in 'facilities' (records), so nothing is dropped silently. */
export function familyOf(id: string): Family {
  const s = id.toLowerCase()
  if (/^(vast|runpod|akash|brev|lambda|fcpi|gpu)/.test(s)) return 'asks'
  if (/^(energy-charts|elexon|entsoe|eia|ercot|pjm|power)/.test(s)) return 'power'
  if (/^(sec-edgar|compustat|wrds|crsp|edgar|filings?)/.test(s)) return 'filings'
  if (/^(polymarket|kalshi|yahoo|equity|markets?)/.test(s)) return 'markets'
  if (/^(hacker-news|rss|gdelt|news|feed)/.test(s)) return 'news'
  return 'facilities'
}

/** Human label for a backend source id: "rss:nvidia" -> "NVIDIA press feed". */
export function sourceLabel(id: string): string {
  const named: Record<string, string> = {
    'vast-ai': 'Vast.ai asks', runpod: 'RunPod list prices', akash: 'Akash console', 'energy-charts': 'Energy-Charts (DE-LU)', 'elexon-bmrs': 'Elexon BMRS (GB)',
    'sec-edgar': 'SEC EDGAR', 'polymarket-gamma': 'Polymarket', kalshi: 'Kalshi', 'yahoo-finance': 'Yahoo Finance closes', 'hacker-news': 'Hacker News', wikidata: 'Wikidata facilities',
    'flopx-sample-v1': 'FLOPX sample set (authored)',
  }
  if (named[id]) return named[id]
  if (id.startsWith('rss:')) return `${id.slice(4).replace(/^\w/, (c) => c.toUpperCase())} press feed`
  if (id.startsWith('fcpi')) return 'FLOPX index (median of adjusted asks)'
  if (id.startsWith('kalshi:')) return `Kalshi ${id.slice(7)}`
  return id
}

export interface SourceRow {
  id: string
  label: string
  family: Family
  kind: ProvenanceKind
  freshness: Freshness
  observedAt: string | null
  licence: string | null
  /** Which reads named it. */
  seenIn: string[]
}

export interface Unavailable { source: string; reason: string; seenIn: string }

/** Totals counted over a raw payload's live rows (provenance not 'sample'), for the retrieval counters. */
export interface OfferTotals { count: number; providers: number; regions: number; models: number; total: number; freshness: Freshness }
export interface MarketTotals { count: number; venues: number; venueNames: string[]; total: number; freshness: Freshness }
export interface PayloadTotals { offers: OfferTotals | null; markets: MarketTotals | null }

export interface SourcesState {
  status: 'idle' | 'pending' | 'ready' | 'failed'
  rows: SourceRow[]
  unavailable: Unavailable[]
  /** Newest generatedAt across the reads that answered. */
  generatedAt: string | null
  reads: { path: string; ok: boolean; ms: number }[]
  /** What /flopx/gpu/offers and /flopx/markets actually carried, counted here rather than from the design arrays the adapters fill. */
  totals: PayloadTotals
}

const READS = ['/flopx/overview', '/flopx/gpu/offers', '/flopx/feed', '/flopx/markets', '/flopx/pressure', '/flopx/infra/energy']
const NO_TOTALS: PayloadTotals = { offers: null, markets: null }

/** Live offers in the payload: count, distinct providers, regions and GPU models. Pure. */
export function offerTotals(offers: Pick<OfferWire, 'providerId' | 'regionId' | 'gpuModelId' | 'provenance'>[] | undefined): OfferTotals | null {
  if (!offers) return null
  const live = offers.filter((o) => o.provenance?.kind !== 'sample')
  const kinds = new Set(live.map((o) => o.provenance.kind))
  return {
    count: live.length,
    providers: new Set(live.map((o) => o.providerId)).size,
    regions: new Set(live.map((o) => o.regionId)).size,
    models: new Set(live.map((o) => o.gpuModelId)).size,
    total: offers.length,
    freshness: !live.length ? 'unavailable' : kinds.has('live') ? 'live' : 'historical',
  }
}

/** Live markets in the payload: count and distinct venues. Pure. */
export function marketTotals(markets: { venue: string; provenance: { kind: ProvenanceKind } }[] | undefined): MarketTotals | null {
  if (!markets) return null
  const live = markets.filter((m) => m.provenance?.kind !== 'sample')
  const venues = [...new Set(live.map((m) => m.venue))].sort()
  const kinds = new Set(live.map((m) => m.provenance.kind))
  return { count: live.length, venues: venues.length, venueNames: venues, total: markets.length, freshness: !live.length ? 'unavailable' : kinds.has('live') ? 'live' : 'historical' }
}

/** Merge freshness blocks into one row per source id (newest observation wins, licence kept). Pure. */
export function mergeFreshness(blocks: { path: string; f: FreshnessBlock | undefined }[]): Pick<SourcesState, 'rows' | 'unavailable' | 'generatedAt'> {
  const rows = new Map<string, SourceRow>()
  const unavailable: Unavailable[] = []
  let generatedAt: string | null = null
  for (const { path, f } of blocks) {
    if (!f) continue
    if (f.generatedAt && (!generatedAt || Date.parse(f.generatedAt) > Date.parse(generatedAt))) generatedAt = f.generatedAt
    for (const d of f.sourceDetails ?? []) {
      const ex = rows.get(d.source)
      const fresh = freshOf({ kind: d.kind }).freshness
      if (!ex) rows.set(d.source, { id: d.source, label: sourceLabel(d.source), family: familyOf(d.source), kind: d.kind, freshness: fresh, observedAt: d.observedAt ?? null, licence: d.licence ?? null, seenIn: [path] })
      else {
        if (!ex.seenIn.includes(path)) ex.seenIn.push(path)
        if (d.observedAt && (!ex.observedAt || Date.parse(d.observedAt) > Date.parse(ex.observedAt))) { ex.observedAt = d.observedAt; ex.kind = d.kind; ex.freshness = fresh }
        if (!ex.licence && d.licence) ex.licence = d.licence
      }
    }
    // Sources listed by name only (no detail row) still count; the sample set is one of them.
    for (const s of f.sources ?? []) {
      if (rows.has(s)) continue
      const kind: ProvenanceKind = s.startsWith('flopx-sample') ? 'sample' : 'live'
      rows.set(s, { id: s, label: sourceLabel(s), family: familyOf(s), kind, freshness: freshOf({ kind }).freshness, observedAt: null, licence: null, seenIn: [path] })
    }
    for (const u of f.unavailable ?? []) {
      const uu = typeof u === 'string' ? { source: u, reason: 'unavailable' } : (u as { source: string; reason: string })
      if (!unavailable.some((x) => x.source === uu.source)) unavailable.push({ ...uu, seenIn: path })
    }
  }
  const order = (r: SourceRow) => (r.kind === 'sample' ? 1 : 0)
  return { rows: [...rows.values()].sort((a, b) => order(a) - order(b) || a.family.localeCompare(b.family) || a.label.localeCompare(b.label)), unavailable, generatedAt }
}

/** Per family: its live rows, the newest observation among them, and the family's badge. */
export function familySummary(rows: SourceRow[]): { family: Family; label: string; what: string; rows: SourceRow[]; newest: string | null; freshness: Freshness }[] {
  return FAMILIES.map((f) => {
    const mine = rows.filter((r) => r.family === f.id)
    const live = mine.filter((r) => r.freshness !== 'illustrative')
    const newest = live.reduce<string | null>((acc, r) => (r.observedAt && (!acc || Date.parse(r.observedAt) > Date.parse(acc)) ? r.observedAt : acc), null)
    const freshness: Freshness = live.length ? (live.some((r) => r.freshness === 'live') ? 'live' : 'historical') : mine.length ? 'illustrative' : 'unavailable'
    return { family: f.id, label: f.label, what: f.what, rows: mine, newest, freshness }
  })
}

// ---------- a tiny external store, loaded once per session when a tour starts ----------

let state: SourcesState = { status: 'idle', rows: [], unavailable: [], generatedAt: null, reads: [], totals: NO_TOTALS }
const listeners = new Set<() => void>()
const emit = () => listeners.forEach((l) => l())

export const getSources = () => state

export function useSources(): SourcesState {
  return useSyncExternalStore((fn) => { listeners.add(fn); return () => listeners.delete(fn) }, getSources, getSources)
}

/** Fetch the freshness blocks (three at a time, the boot's own cap is six) and merge. Idempotent. */
export async function loadSources(fetchImpl?: typeof fetch): Promise<SourcesState> {
  if (state.status === 'pending' || state.status === 'ready') return state
  state = { ...state, status: 'pending' }
  emit()
  const results: { path: string; f: FreshnessBlock | undefined; ok: boolean; ms: number }[] = []
  const totals: PayloadTotals = { offers: null, markets: null }
  type Raw = { freshness?: FreshnessBlock; offers?: OfferWire[]; markets?: { venue: string; provenance: { kind: ProvenanceKind } }[] }
  for (let i = 0; i < READS.length; i += 3) {
    const batch = READS.slice(i, i + 3)
    const rs = await Promise.all(batch.map((p) => fetchJson<Raw>(p, { timeoutMs: 15_000, fetchImpl })))
    rs.forEach((r, k) => {
      results.push({ path: batch[k], f: r.ok ? r.data.freshness : undefined, ok: r.ok, ms: r.ms })
      if (r.ok && batch[k] === '/flopx/gpu/offers') totals.offers = offerTotals(r.data.offers)
      if (r.ok && batch[k] === '/flopx/markets') totals.markets = marketTotals(r.data.markets)
    })
  }
  const merged = mergeFreshness(results)
  const anyOk = results.some((r) => r.ok)
  state = { status: anyOk ? 'ready' : 'failed', ...merged, reads: results.map(({ path, ok, ms }) => ({ path, ok, ms })), totals }
  emit()
  return state
}

/** Test helper. */
export function __resetSources(): void {
  state = { status: 'idle', rows: [], unavailable: [], generatedAt: null, reads: [], totals: NO_TOTALS }
  listeners.clear()
}
