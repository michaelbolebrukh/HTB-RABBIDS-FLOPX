import { NOW_IDX, type Region } from '@/data/core'
import type { Scenario } from './store'

/** Additional inferred pressure (index pts) produced by a what-if scenario at month t. */
export function scenarioDelta(r: Region, s: Scenario | null, t: number) {
  if (!s || s.region !== r.id || t <= NOW_IDX) return 0
  const ramp = Math.min(1, (t - NOW_IDX) / 6)
  const slip = (s.slipMonths / 12) * (r.slipSensitivity / Math.max(1, r.opMW)) * 38 * ramp
  const power = (s.powerShock / 100) * 9 * ramp
  return +(slip + power).toFixed(1)
}

export function pressureAt(r: Region, t: number, s: Scenario | null = null) {
  if (r.quality === 'none') return null
  return Math.max(0, Math.min(100, r.pressure[t] + scenarioDelta(r, s, t)))
}

export function uncAt(r: Region, t: number, s: Scenario | null = null) {
  const base = t <= NOW_IDX ? r.unc : r.unc * (1 + 0.9 * Math.sqrt(t - NOW_IDX))
  // scenarios add model uncertainty: they extrapolate beyond observed regimes
  return base * (s && s.region === r.id && t > NOW_IDX ? 1.25 : 1)
}

/** Estimated quantities affected by a scenario, before → after. */
export function scenarioImpact(r: Region, s: Scenario) {
  const slipMW = Math.round((r.slipSensitivity * Math.min(12, s.slipMonths)) / 12)
  const eff: [number, number] = [Math.max(0, r.eff27[0] - slipMW), Math.max(0, r.eff27[1] - Math.round(slipMW * 0.7))]
  const d6 = scenarioDelta(r, s, Math.min(38, NOW_IDX + 6))
  const d12 = scenarioDelta(r, s, 38)
  const premiumAfter = r.premium + d6 * 0.55
  return { slipMW, eff, d6, d12, premiumAfter }
}

/** `ramp` 'p' is the text/mark ramp; 'g' is the lighter ramp for translucent map glows. */
export function pressureColor(p: number | null, ramp: 'p' | 'g' = 'p') {
  if (p == null) return 'var(--color-line)'
  // Low → high pressure, interpolated between the active blend's --color-p0..p4 tokens.
  const stops = [30, 50, 65, 80, 92]
  const at = (k: number) => `var(--color-${ramp}${k})`
  if (p <= stops[0]) return at(0)
  for (let k = 0; k < stops.length - 1; k++) {
    if (p <= stops[k + 1]) {
      const t = (p - stops[k]) / (stops[k + 1] - stops[k])
      return `color-mix(in oklab, ${at(k)} ${Math.round((1 - t) * 100)}%, ${at(k + 1)})`
    }
  }
  return at(stops.length - 1)
}

/**
 * Pressure bands: the backend's cpx-v1 vocabulary and thresholds (/flopx/pressure `bands`:
 * slack < 25, balanced 25 to 50, tight 50 to 75, acute >= 75), used everywhere a score gets a
 * word so one number never carries two words on one screen. The design's own Loose/Balanced/
 * Tight/Severe split at 52/66/78 is retired; `setPressureBands` lets the live layer replace the
 * thresholds if the backend ever changes them.
 */
export const PRESSURE_BANDS: { word: string; min: number }[] = [
  { word: 'Acute', min: 75 },
  { word: 'Tight', min: 50 },
  { word: 'Balanced', min: 25 },
  { word: 'Slack', min: 0 },
]
export const PRESSURE_BAND_WORDS = () => [...PRESSURE_BANDS].reverse().map((b) => b.word)

/** Parse the backend's `bands` object ({ slack: '< 25', balanced: '25 to 50', ... }) into thresholds. */
export function setPressureBands(bands: Record<string, string>): boolean {
  const next: { word: string; min: number }[] = []
  for (const [k, v] of Object.entries(bands)) {
    const m = v.match(/(\d+(?:\.\d+)?)/g)
    if (!m) continue
    const min = v.trim().startsWith('<') ? 0 : +m[0]
    next.push({ word: k.charAt(0).toUpperCase() + k.slice(1), min })
  }
  if (next.length < 2) return false
  next.sort((a, b) => b.min - a.min)
  PRESSURE_BANDS.length = 0
  PRESSURE_BANDS.push(...next)
  return true
}

export function pressureWord(p: number | null) {
  if (p == null) return 'No estimate'
  return (PRESSURE_BANDS.find((b) => p >= b.min) ?? PRESSURE_BANDS[PRESSURE_BANDS.length - 1]).word
}
