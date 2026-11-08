// The date model. The design draws every monthly chart on a fixed 39-slot axis with "now" at slot
// 26 (MONTHS / NOW_IDX in data/core.ts). We keep the slot count and NOW_IDX, so every piece of
// design index arithmetic stays valid, and re-anchor the labels on the real calendar: slot 26 is
// the current UTC month, slots 0..25 the 26 months before it, slots 27..38 the 12 after it.
// See src/live/README.md for how real points are placed on this axis.
//
// This module must not import data/core.ts (core imports it to build MONTHS).

export const AXIS_LEN = 39
export const AXIS_NOW_IDX = 26

const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** Months since year 0, UTC. */
export const ym = (d: Date) => d.getUTCFullYear() * 12 + d.getUTCMonth()

/** Labels like "Nov 26" for the 39 slots, with `now`'s month at AXIS_NOW_IDX. */
export function axisLabels(now: Date): string[] {
  const base = ym(now) - AXIS_NOW_IDX
  return Array.from({ length: AXIS_LEN }, (_, i) => {
    const m = base + i
    return `${MON[((m % 12) + 12) % 12]} ${String(Math.floor(m / 12) % 100).padStart(2, '0')}`
  })
}

let anchor: Date = new Date()

export function axisAnchor(): Date {
  return anchor
}

/** Re-anchor (e.g. on the backend's generatedAt). Returns true when the month changed. */
export function setAxisAnchor(d: Date): boolean {
  if (Number.isNaN(d.getTime())) return false
  const changed = ym(d) !== ym(anchor)
  anchor = d
  return changed
}

/** Axis slot for a timestamp; may fall outside 0..AXIS_LEN-1. */
export function slotOf(t: string | Date, now: Date = anchor): number {
  const d = typeof t === 'string' ? new Date(t) : t
  return ym(d) - ym(now) + AXIS_NOW_IDX
}

export interface PlacedSeries {
  /** The axis values: real where `real[i]`, otherwise the design value passed in. */
  values: number[]
  /** Which slots carry a real (backend) value. */
  real: boolean[]
  /** Number of backend points that fell on the past/present part of the axis. */
  used: number
}

/**
 * The one placement rule every month-axis chart follows:
 *   1. Each backend point lands in the slot of its UTC calendar month.
 *   2. A slot with several points takes the LAST one by time (month to date for the current month).
 *   3. Points in future slots (> NOW) or before slot 0 are ignored: the past axis never shows a
 *      forecast, and nothing is extrapolated backwards.
 *   4. Slots with no backend point keep the design value and stay flagged not-real. Placement
 *      never rescales them; that is the separate, second step `joinToNow` (decision G14), which
 *      every month-axis series with a real present runs after placing.
 */
export function placeOnAxis(design: number[], points: { t: string; value: number | null }[], now: Date = anchor): PlacedSeries {
  const values = [...design]
  const real = design.map(() => false)
  const lastT: number[] = design.map(() => -Infinity)
  let used = 0
  for (const p of points) {
    if (p.value == null || !Number.isFinite(p.value)) continue
    const i = slotOf(p.t, now)
    if (i < 0 || i > AXIS_NOW_IDX || i >= design.length) continue
    const tt = new Date(p.t).getTime()
    used++
    if (tt >= lastT[i]) {
      lastT[i] = tt
      values[i] = p.value
      real[i] = true
    }
  }
  return { values, real, used }
}

/** How an illustrative design curve is joined onto real points: prices multiply, 0..100 scores shift. */
export type JoinMode = 'multiply' | 'shift'

export interface JoinedSeries extends PlacedSeries {
  /** True when at least one illustrative slot was rescaled to meet a real point. */
  scaled: boolean
  /** The factor (multiply) or offset (shift) applied to the slots after the last real point: the forecast slots. */
  forecastScale: number
  /** Multiply an uncertainty width by this to scale a band with the series (1 under 'shift'). */
  bandFactor: number
}

const IDENTITY = (mode: JoinMode) => (mode === 'multiply' ? 1 : 0)

/**
 * Decision G14 (planning/instance-briefs/flopx.md): where a series has a real present and only an
 * illustrative past or future, the illustrative points are scaled so the curve joins the real
 * points continuously. Real points never change. The design curve keeps its shape:
 *   - 'multiply' (prices, indices, $/MWh): each illustrative slot is design[i] * f.
 *   - 'shift' (0..100 scores): each illustrative slot is design[i] + f, then clamped to `clamp`.
 * f is anchored at every real slot (f_k = real_k / design_k, or real_k - design_k). Illustrative
 * slots before the first real slot take the first anchor, slots after the last real slot (the
 * forecast months) take the last anchor, and slots between two real slots interpolate linearly
 * between their anchors, so the curve is continuous through every join. A real slot whose design
 * value is NaN or zero cannot anchor a multiply and is skipped; with no usable anchor nothing is
 * scaled. NaN design slots (a GPU before launch) stay NaN.
 */
export function joinToNow(placed: PlacedSeries, design: number[], mode: JoinMode, opts: { clamp?: [number, number]; digits?: number } = {}): JoinedSeries {
  const digits = opts.digits ?? 2
  const anchors: { i: number; f: number }[] = []
  placed.real.forEach((r, i) => {
    if (!r) return
    const d = design[i]
    const v = placed.values[i]
    if (!Number.isFinite(d) || !Number.isFinite(v)) return
    if (mode === 'multiply') {
      if (d === 0) return
      anchors.push({ i, f: v / d })
    } else anchors.push({ i, f: v - d })
  })
  const id = IDENTITY(mode)
  if (!anchors.length) return { ...placed, values: [...placed.values], real: [...placed.real], scaled: false, forecastScale: id, bandFactor: 1 }
  const factorAt = (i: number): number => {
    if (i <= anchors[0].i) return anchors[0].f
    const last = anchors[anchors.length - 1]
    if (i >= last.i) return last.f
    let k = 0
    while (k < anchors.length - 1 && anchors[k + 1].i < i) k++
    const a = anchors[k]
    const b = anchors[k + 1]
    const t = (i - a.i) / (b.i - a.i)
    return a.f + (b.f - a.f) * t
  }
  const values = placed.values.map((v, i) => {
    if (placed.real[i]) return v
    const d = design[i]
    if (!Number.isFinite(d)) return d
    const f = factorAt(i)
    let out = mode === 'multiply' ? d * f : d + f
    if (opts.clamp) out = Math.max(opts.clamp[0], Math.min(opts.clamp[1], out))
    return +out.toFixed(digits)
  })
  const scaled = placed.real.some((r, i) => !r && Number.isFinite(design[i]) && values[i] !== design[i])
  const forecastScale = anchors[anchors.length - 1].f
  return { values, real: [...placed.real], used: placed.used, scaled, forecastScale, bandFactor: mode === 'multiply' ? forecastScale : 1 }
}

/** Place real points on the axis, then join the design curve onto them (G14), in one call. */
export function placeAndJoin(design: number[], points: { t: string; value: number | null }[], mode: JoinMode, opts: { clamp?: [number, number]; digits?: number; now?: Date } = {}): JoinedSeries {
  return joinToNow(placeOnAxis(design, points, opts.now), design, mode, opts)
}

/** Count of real slots, and the first real slot (for "history starts" notes). */
export function realSpan(real: boolean[]): { count: number; first: number } {
  let count = 0
  let first = -1
  real.forEach((r, i) => {
    if (r) {
      count++
      if (first < 0) first = i
    }
  })
  return { count, first }
}
