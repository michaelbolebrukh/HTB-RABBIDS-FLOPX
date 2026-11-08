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

/** Page caveats, written once; `Fn n={i + 1}` markers point here and `Notes` lists them at the bottom of the page. */
export const NOTES = [
  'List prices net of OpenRouter discounts, not executed spend; FLOPX has bought nothing. Blended = 0.75 × prompt + 0.25 × completion, so output-heavy work costs more. Closed models are priced by their lab or its cloud resellers. Cache discounts are ignored.',
  "Tier = each lab's product line by name, latest release. FLOPX does not measure capability, so tiers compare list prices, not quality. 100M tokens at the 3:1 blend.",
  'Quantization is a provider label, not verified, and unknown for many endpoints.',
  'Latency and throughput are not in this snapshot, so fast-hardware hosts carry a positive residual that is partly paid-for speed, not mispricing. Some endpoints have time-of-day pricing; the stored price is the default window. Uptime is the last day only.',
  'Ridge on log(blended $/M) ~ model + precision + tier + context cap + uptime gap + tools + structured + caching + time-of-day pricing. Partial correlations in one snapshot, not causal effects.',
  `Sources: GET openrouter.ai/api/v1/models at ${fmtStamp(snapshot.catalogAt)} UTC; GET openrouter.ai/api/v1/models/{id}/endpoints at ${fmtStamp(snapshot.endpointsAt)} UTC. Refresh with node scripts/openrouter-snapshot.mjs.`,
  'Routing only: providers post list prices, so the lever is where you buy, not when.',
]

/** Closed vs open-weight hues (a category pair, always shown with a legend), used by every chart on the page. */
export const CLOSED = 'var(--color-bone)'
export const OPEN = 'var(--color-open)'
