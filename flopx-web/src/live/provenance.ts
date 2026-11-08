// Backend provenance -> the design's own Freshness vocabulary. One mapping, used by every
// adapter, so a 'live' badge can only ever come from backend data whose provenance supports it.
import type { Freshness } from '@/data/core'
import type { Provenance, ProvenanceKind } from './types'

export interface FreshTag {
  freshness: Freshness
  /** Optional badge label when the design's default label would overstate (e.g. derived). */
  label?: string
}

/**
 * live     -> 'live'
 * archived -> 'historical' (a stored FLOPX observation)
 * sample   -> 'illustrative' (authored sample data, never observed)
 * derived  -> follows its inputs: all live -> 'live' labelled "Live (derived)"; any sample ->
 *             'illustrative'; otherwise (archived inputs, or inputs not stated) -> 'historical'.
 */
export function freshOf(p: Pick<Provenance, 'kind' | 'inputKinds'> | null | undefined): FreshTag {
  if (!p) return { freshness: 'illustrative' }
  switch (p.kind) {
    case 'live':
      return { freshness: 'live' }
    case 'archived':
      return { freshness: 'historical' }
    case 'sample':
      return { freshness: 'illustrative' }
    case 'derived': {
      const inputs: ProvenanceKind[] = p.inputKinds ?? []
      if (inputs.includes('sample')) return { freshness: 'illustrative' }
      if (inputs.length && inputs.every((k) => k === 'live')) return { freshness: 'live', label: 'Live (derived)' }
      return { freshness: 'historical' }
    }
    default:
      return { freshness: 'illustrative' }
  }
}

/** True when a value may carry the design's OBS glyph: backend data that is not authored sample. */
export function isObserved(p: Pick<Provenance, 'kind' | 'inputKinds'> | null | undefined): boolean {
  return freshOf(p).freshness !== 'illustrative'
}

const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** "08 Nov 2026 00:39 UTC", the design's AS_OF format. */
export function fmtAsOf(iso: string | Date): string {
  const d = typeof iso === 'string' ? new Date(iso) : iso
  if (Number.isNaN(d.getTime())) return ''
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getUTCDate())} ${MON[d.getUTCMonth()]} ${d.getUTCFullYear()} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())} UTC`
}

/** "08 Nov 2026" */
export function fmtDay(iso: string | Date): string {
  return fmtAsOf(iso).split(' ').slice(0, 3).join(' ')
}

/** "08 Nov 00:39", the design's per-offer "observed" format. */
export function fmtObserved(iso: string | null | undefined): string {
  if (!iso) return 'n/a'
  const s = fmtAsOf(iso)
  if (!s) return 'n/a'
  const [dd, mon, , hm] = s.split(' ')
  return `${dd} ${mon} ${hm}`
}
