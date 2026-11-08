// A tiny external store for live data state. Design modules (data/*.ts) keep exporting the same
// arrays the components already import; live adapters mutate those exports in place, then call
// bump(). Anything that renders through useStore() re-renders on bump, because the app store
// subscribes via useLiveVersion() (lib/store.tsx). Components with useMemo over design data add
// `live` (the version) to their dependency list.
import { useSyncExternalStore } from 'react'

export type DomainStatus = 'pending' | 'live' | 'failed'

export interface DomainState {
  status: DomainStatus
  error?: string
  ms?: number
  /** Backend freshness.generatedAt of the payload that landed. */
  generatedAt?: string
}

interface State {
  version: number
  domains: Record<string, DomainState>
  /** Latest backend generatedAt seen across all landed payloads. */
  generatedAt: string | null
}

const state: State = { version: 0, domains: {}, generatedAt: null }
const listeners = new Set<() => void>()

export function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

export const getVersion = () => state.version

/** Notify every subscriber that live data changed. Cheap; batched by React. */
export function bump(): void {
  state.version++
  listeners.forEach((fn) => fn())
}

export function setDomain(id: string, s: DomainState): void {
  state.domains[id] = s
}

export function domain(id: string): DomainState {
  return state.domains[id] ?? { status: 'pending' }
}

export function allDomains(): Readonly<Record<string, DomainState>> {
  return state.domains
}

/** Record a payload's generatedAt; returns true when it is the newest seen so far. */
export function noteGeneratedAt(iso: string | null | undefined): boolean {
  if (!iso) return false
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return false
  if (state.generatedAt && Date.parse(state.generatedAt) >= t) return false
  state.generatedAt = iso
  return true
}

export const latestGeneratedAt = () => state.generatedAt

// ---------- Payload totals (read by the tour cards; written by the adapters) ----------
// The design arrays (OFFERS, PRED) keep the design's GPU, region and card sets after the adapters
// fill them, so counting them under-reports the backend (WF4 #4: 65 vs 119 offers, 1 vs 2 venues).
// These are counted from the raw payloads at apply time. Null until the payload has landed.

export type TotalsFreshness = 'live' | 'historical' | 'unavailable'
export interface LiveOffersTotals {
  /** Offers in /flopx/gpu/offers whose provenance is not 'sample'. */
  count: number
  providers: number
  regions: number
  models: number
  /** Every offer in the payload, sample ones included. */
  total: number
  /** 'live' when any counted offer is live, 'historical' when all are archived, 'unavailable' when none counted. */
  freshness: TotalsFreshness
}
export interface MarketTotals {
  /** Rows in /flopx/markets whose provenance is live or archived (not sample); `count` is the same number under the tour's name. */
  live: number
  count: number
  venues: number
  venueNames: string[]
  total: number
  freshness: TotalsFreshness
}
export interface PriceBasisTotals {
  /** Source ids from the evaluation's freshness block, joined with '+', or 'sample'. */
  source: string
  /** Last close date (YYYY-MM-DD) the closes run through, null when not stated. */
  through: string | null
  kind: 'sample' | 'archived' | 'live'
  /** Badge text, the same string Eval.tsx prints: "Archived · alpaca-data closes through 2026-11-06". */
  label: string
}
export interface LiveTotals {
  liveOffers: LiveOffersTotals | null
  marketTotals: MarketTotals | null
  priceBasis: PriceBasisTotals | null
}

export const LIVE_TOTALS: LiveTotals = { liveOffers: null, marketTotals: null, priceBasis: null }

export function setLiveTotals(patch: Partial<LiveTotals>): void {
  Object.assign(LIVE_TOTALS, patch)
}

/** Re-render on every live update. Returns the version (use it as a useMemo dependency). */
export function useLiveVersion(): number {
  return useSyncExternalStore(subscribe, getVersion, getVersion)
}

/** Test helper: reset to the initial state. */
export function __resetLiveStore(): void {
  state.version = 0
  state.domains = {}
  state.generatedAt = null
  listeners.clear()
  LIVE_TOTALS.liveOffers = null
  LIVE_TOTALS.marketTotals = null
  LIVE_TOTALS.priceBasis = null
}
