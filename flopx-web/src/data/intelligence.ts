import { TOKENS } from './handoff'
import { rng } from './core'

/**
 * Same-model provider endpoints for DeepSeek V4.1 Flash.
 * Only Morph and Fireworks prices are real (TOKENS.spread). Every other row, and every
 * attribute (precision, context, uptime) of every row, is synthetic: seeded, deterministic,
 * and kept inside the real min/max. Render with the Illustrative badge.
 */
export interface Endpoint {
  provider: string
  /** $ per million tokens, blended 3:1. */
  price: number
  /** Precision in bits; 0 when the endpoint does not state it. */
  bits: 0 | 8 | 16
  /** Context window in thousands of tokens. */
  contextK: number
  /** Uptime over 30 days, percent. */
  uptime: number
  /** True only for the two endpoints whose price comes from the snapshot. */
  real: boolean
}

export interface Needs { minBits: 0 | 8 | 16; minContextK: number; minUptime: number }

export interface Selection { best: Endpoint | null; qualifying: number; total: number; medianAll: number; vsMedianPct: number | null }

const NAMES = ['Atlas', 'Baseten', 'Cerebrium', 'Chutes', 'Crusoe', 'DeepInfra', 'Featherless', 'Friendli', 'Groq', 'Hyperbolic', 'Inception', 'Inferless', 'Kluster', 'Lambda', 'Lepton', 'Modal', 'Nebius', 'Nscale', 'Novita', 'Parasail', 'Phala', 'Replicate', 'SambaNova', 'SiliconFlow', 'Targon', 'Together', 'Venice', 'Vultr', 'Wafer', 'Zeta']

export function median(xs: number[]): number {
  if (!xs.length) return NaN
  const s = [...xs].sort((a, b) => a - b)
  const m = s.length >> 1
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2
}

export function makeEndpoints(seed = 41): Endpoint[] {
  const { cheapest, dearest, providers } = TOKENS.spread
  const lo = cheapest.price, hi = dearest.price
  const r = rng(seed)
  const attrs = (): Pick<Endpoint, 'bits' | 'contextK' | 'uptime'> => {
    const u = r()
    const bits = u < 0.25 ? 0 : u < 0.7 ? 8 : 16
    const contextK = ([64, 128, 128, 164, 256] as const)[Math.floor(r() * 5)]
    return { bits, contextK, uptime: Math.round((94 + r() * 5.9) * 10) / 10 }
  }
  const out: Endpoint[] = [
    // Illustrative attributes chosen so tighter needs move the answer off Morph.
    { provider: 'Morph', price: lo, real: true, bits: 8, contextK: 64, uptime: 96.4 },
    { provider: 'Fireworks', price: hi, real: true, bits: 16, contextK: 164, uptime: 99.7 },
  ]
  for (let i = 0; i < providers - 2; i++) {
    // Log-space, skewed towards the cheap end, strictly inside (lo, hi).
    const t = Math.pow(r(), 1.6)
    const price = Math.exp(Math.log(lo) + (0.02 + 0.96 * t) * (Math.log(hi) - Math.log(lo)))
    out.push({ provider: NAMES[i % NAMES.length] + (i >= NAMES.length ? ' 2' : ''), price: Math.round(price * 10000) / 10000, real: false, ...attrs() })
  }
  return out
}

export const ENDPOINTS: Endpoint[] = makeEndpoints()

/** Cheapest endpoint meeting every need. Ties go to higher uptime. Unstated precision never meets a precision need. */
export function selectProvider(eps: Endpoint[], needs: Needs): Selection {
  const med = median(eps.map((e) => e.price))
  const ok = eps.filter((e) => e.bits >= needs.minBits && (needs.minBits === 0 || e.bits > 0) && e.contextK >= needs.minContextK && e.uptime >= needs.minUptime)
  const best = ok.reduce<Endpoint | null>((b, e) => (!b || e.price < b.price || (e.price === b.price && e.uptime > b.uptime) ? e : b), null)
  return { best, qualifying: ok.length, total: eps.length, medianAll: med, vsMedianPct: best ? (best.price / med - 1) * 100 : null }
}
