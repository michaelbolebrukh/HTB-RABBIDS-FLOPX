// What closes the research evaluation was scored on, read from the payload's freshness block, so
// the panel badge and caption follow the backend: "Illustrative · sample closes" while it served
// authored sample closes, "Archived · alpaca-data closes through 2026-11-06" when it says alpaca-data
// (what it serves tonight), "Archived · WRDS CRSP closes through 2025-12-31" when it says wrds-crsp.
// The badge says what the payload says; it never prefers one archive over another.
import type { Freshness } from '@/data/core'

export interface BasisFreshness {
  sources?: string[]
  oldestObservedAt?: string
  sourceDetails?: { source: string; kind?: string; observedAt?: string }[]
}

export interface PriceBasis {
  kind: 'sample' | 'archived' | 'live'
  freshness: Freshness
  /** The payload's own source ids for the closes, joined with '+': "alpaca-data", "wrds-crsp", "sample". */
  source: string
  /** Last close date the closes run through (YYYY-MM-DD), null when the payload states none. */
  through: string | null
  /** Badge text. */
  label: string
  /** Sentence fragment for captions: "scored on WRDS CRSP closes through 2025-12-31". */
  caption: string
  /** Short phrase for inline scope text: "WRDS CRSP closes" / "sample closes". */
  short: string
}

const NAMES: Record<string, string> = { 'wrds-crsp': 'WRDS CRSP', 'flopx-sample-v1': 'sample', alpaca: 'Alpaca', yahoo: 'Yahoo' }
const day = (iso: string | undefined) => (iso && !Number.isNaN(new Date(iso).getTime()) ? iso.slice(0, 10) : null)

export function evalPriceBasis(f: BasisFreshness | null | undefined): PriceBasis {
  const sources = f?.sources ?? []
  const details = f?.sourceDetails ?? []
  const kindOf = (s: string) => details.find((d) => d.source === s)?.kind
  const real = sources.filter((s) => s !== 'flopx-sample-v1' && kindOf(s) !== 'sample')
  const hasSample = sources.includes('flopx-sample-v1') || sources.some((s) => kindOf(s) === 'sample')
  if (!real.length) {
    return { kind: 'sample', freshness: 'illustrative', source: 'sample', through: null, label: 'Illustrative · sample closes', caption: 'scored on authored sample closes, not market data', short: 'sample closes' }
  }
  const live = real.every((s) => kindOf(s) === 'live')
  const name = real.map((s) => NAMES[s] ?? s).join(' + ')
  const source = real.join('+')
  const through = day(details.find((d) => real.includes(d.source))?.observedAt ?? f?.oldestObservedAt)
  const mix = hasSample ? ' (sample closes where the archive has none)' : ''
  if (live) return { kind: 'live', freshness: 'live', source, through, label: `Live · ${name} closes`, caption: `scored on ${name} closes${mix}`, short: `${name} closes` }
  return {
    kind: 'archived',
    freshness: 'historical',
    source,
    through,
    label: `Archived · ${name} closes${through ? ` through ${through}` : ''}`,
    caption: `scored on ${name} closes${through ? ` through ${through}` : ''}${mix}`,
    short: `${name} closes`,
  }
}
