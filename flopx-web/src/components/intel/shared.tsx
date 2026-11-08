import snapshot from '@/data/openrouter.json'
import { hedonic, type Snapshot } from '@/lib/tokenmarket'

/** The OpenRouter snapshot and the hedonic fit, computed once per page load. */
export const SNAP = snapshot as Snapshot
export const FIT = hedonic(SNAP.endpoints)
export const MODEL = new Map(SNAP.models.map((m) => [m.id, m]))

/** $/M with precision that suits the magnitude: $10.00, $1.20, $0.315, $0.0495. */
export function usd(v: number): string {
  if (!Number.isFinite(v)) return '—'
  if (v >= 1) return `$${v.toFixed(2)}`
  if (v >= 0.1) return `$${v.toFixed(3)}`
  return `$${v.toPrecision(3)}`
}
export const times = (v: number) => `${v.toFixed(v >= 10 ? 0 : 1)}×`
export const fmtDate = (unix: number) => new Date(unix * 1000).toLocaleDateString('en-GB', { day: '2-digit', month: 'short', timeZone: 'UTC' })
export const fmtStamp = (iso: string) => new Date(iso).toLocaleString('en-GB', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: 'UTC' })

/** Closed vs open-weight hues, used by every chart on the page. */
export const CLOSED = 'var(--color-bone)'
export const OPEN = 'var(--color-fct)'
