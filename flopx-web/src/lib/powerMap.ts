import type { Project, Stage } from '@/data/infra'

/** Day-ahead power above this ($/MWh) is shown as expensive. */
export const POWER_HOT = 90

export const powerTone = (usdPerMWh: number): 'down' | 'bone' => (usdPerMWh > POWER_HOT ? 'down' : 'bone')

/** Most built first, so a region's pips read left to right from real capacity to paper capacity. */
export const STAGE_ORDER: Stage[] = ['operational', 'construction', 'proposed', 'announced']

/** Each region's facility stages, most built first. Regions with no facilities are absent. */
export function stagesByRegion(projects: readonly Pick<Project, 'region' | 'stage'>[]): Map<string, Stage[]> {
  const out = new Map<string, Stage[]>()
  for (const p of projects) out.set(p.region, [...(out.get(p.region) ?? []), p.stage])
  for (const s of out.values()) s.sort((a, b) => STAGE_ORDER.indexOf(a) - STAGE_ORDER.indexOf(b))
  return out
}

/** At most `max` pips; the rest collapse into a "+n" count. */
export function capPips(stages: readonly Stage[], max = 4): { shown: Stage[]; more: number } {
  return { shown: stages.slice(0, max), more: Math.max(0, stages.length - max) }
}

/**
 * The perspective (map layer) a region list and the map labels read their value from: the most
 * recently switched-on layer wins, so turning Rental economics on relabels the list in dollars.
 * Values come only from the live blocks composed by src/live/adapt-w1.ts; a region the backend
 * did not cover reads 'n/a' rather than a design number.
 */
export type Perspective = 'pressure' | 'rental' | 'power' | 'pipeline' | 'effective' | 'exposure'

export function activePerspective(layers: readonly Perspective[]): Perspective {
  return layers.length ? layers[layers.length - 1] : 'pressure'
}

/** Exposure weight: a documented (filing-backed) tie counts twice an authored profile tie. */
export const DOCUMENTED_WEIGHT = 2

interface PerspectiveRegion {
  documented: readonly { t: string }[]
  authored?: readonly { t: string }[]
  live?: {
    rental: { median: number | null; premiumPct: number | null; fallback: boolean } | null
    power: { value: number; unit: string; freshness: string } | null
    supply: { facilities: number; effectiveMw: number } | null
    exposure: { freshness: string } | null
  }
}

/** Companies tied to a region (documented first), with the weighted score; only once the live exposure read landed. */
export function exposureOf(r: PerspectiveRegion): { tickers: { t: string; documented: boolean }[]; documented: number; authored: number; score: number } | null {
  if (!r.live?.exposure) return null
  const doc = r.documented.map((d) => ({ t: d.t, documented: true }))
  const seen = new Set(doc.map((d) => d.t))
  const auth = (r.authored ?? []).filter((a) => !seen.has(a.t)).map((a) => ({ t: a.t, documented: false }))
  return { tickers: [...doc, ...auth], documented: doc.length, authored: auth.length, score: doc.length * DOCUMENTED_WEIGHT + auth.length }
}

const UNIT_SYMBOL: Record<string, string> = { 'USD/MWh': '$', 'EUR/MWh': '€', 'GBP/MWh': '£' }

/** Short label plus a quiet qualifier for one region under one perspective. Null value means the backend has nothing here. */
export function perspectiveValue(r: PerspectiveRegion, layer: Perspective): { value: string | null; note: string; sort: number } {
  const L = r.live
  switch (layer) {
    case 'rental': {
      const q = L?.rental
      if (!q || q.median == null) return { value: null, note: 'no asks', sort: -Infinity }
      if (q.fallback || q.premiumPct == null) return { value: `$${q.median.toFixed(2)}`, note: 'global median, n/a premium', sort: q.median }
      return { value: `$${q.median.toFixed(2)}`, note: `${q.premiumPct >= 0 ? '+' : ''}${q.premiumPct.toFixed(1)}% vs global`, sort: q.median }
    }
    case 'power': {
      const p = L?.power
      if (!p) return { value: null, note: 'no power feed', sort: -Infinity }
      const sym = UNIT_SYMBOL[p.unit]
      const v = sym ? `${sym}${p.value.toFixed(0)}` : `${p.value.toFixed(0)} ${p.unit}`
      return { value: v, note: p.freshness === 'live' || p.freshness === 'delayed' ? 'live day-ahead' : `${p.freshness === 'illustrative' || p.freshness === 'simulated' ? 'sample' : p.freshness}`, sort: p.value }
    }
    case 'pipeline':
    case 'effective': {
      const s = L?.supply
      if (!s) return { value: null, note: 'no facility records', sort: -Infinity }
      return { value: `${Math.round(s.effectiveMw)} MW`, note: `${s.facilities} facilit${s.facilities === 1 ? 'y' : 'ies'}, effective`, sort: s.effectiveMw }
    }
    case 'exposure': {
      const x = exposureOf(r)
      if (!x) return { value: null, note: 'not loaded', sort: -Infinity }
      return { value: `${x.tickers.length}`, note: `${x.documented} documented, ${x.authored} authored`, sort: x.score }
    }
    default:
      return { value: null, note: '', sort: 0 }
  }
}
