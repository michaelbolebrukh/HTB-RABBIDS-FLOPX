// Energy page summary strip, counted from the rows the page actually shows rather than the 7 Nov
// hand-off figures (28 markets, 27 real). A row is "real" when its freshness is an observed class,
// "sample" when the backend supplied a series it marks sample (the row carries `real[]` but reads
// illustrative), and "no series" when nothing but the design value exists.
import type { Freshness } from '@/data/core'

export interface StripRow { id: string; name: string; priceNow: number; currency: string; freshness: Freshness; real?: boolean[] }
export type StripKind = 'real' | 'sample' | 'none'

const OBSERVED: Freshness[] = ['live', 'delayed', 'historical', 'stale', 'experimental']

export function stripKind(r: StripRow): StripKind {
  if (OBSERVED.includes(r.freshness)) return 'real'
  if (r.real) return 'sample'
  return 'none'
}

export interface StripCounts {
  total: number
  real: number
  sample: number
  none: number
  kinds: StripKind[]
  /** Cheapest and dearest among rows with a backend series (real or sample); null when none. */
  cheapest: StripRow | null
  dearest: StripRow | null
  label: string
}

export function energyStripCounts(rows: StripRow[]): StripCounts {
  const kinds = rows.map(stripKind)
  const real = kinds.filter((k) => k === 'real').length
  const sample = kinds.filter((k) => k === 'sample').length
  const none = kinds.length - real - sample
  // The price range runs over every market with a backend series (real or sample); the label beside it names which is which.
  const pool = rows.filter((_, i) => kinds[i] !== 'none')
  const sorted = [...pool].sort((a, b) => a.priceNow - b.priceNow)
  const name = (r: StripRow[]) => r.map((x) => x.name.split(' (')[0]).join(', ')
  const realRows = rows.filter((_, i) => kinds[i] === 'real')
  const sampleRows = rows.filter((_, i) => kinds[i] === 'sample')
  const label = [
    `${real} real zone${real === 1 ? '' : 's'}${real ? ` (${name(realRows)})` : ''}`,
    `${sample} sample${sample ? ` (${name(sampleRows)})` : ''}`,
    `${none} no series`,
  ].join(' · ')
  return { total: rows.length, real, sample, none, kinds, cheapest: sorted[0] ?? null, dearest: sorted[sorted.length - 1] ?? null, label }
}
