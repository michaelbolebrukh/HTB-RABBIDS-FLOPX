import { describe, expect, it } from 'vitest'
import { AXIS_LEN, AXIS_NOW_IDX, axisLabels, joinToNow, placeAndJoin, placeOnAxis, realSpan, slotOf } from './dates'

const NOW = new Date('2026-11-08T00:39:13Z')

describe('axisLabels', () => {
  it('puts the current UTC month at slot 26 and keeps 39 slots', () => {
    const m = axisLabels(NOW)
    expect(m).toHaveLength(AXIS_LEN)
    expect(m[AXIS_NOW_IDX]).toBe('Nov 26')
    expect(m[0]).toBe('Sep 24')
    expect(m[AXIS_NOW_IDX - 1]).toBe('Oct 26')
    expect(m[AXIS_NOW_IDX + 2]).toBe('Jan 27')
    expect(m[AXIS_LEN - 1]).toBe('Nov 27')
  })
})

describe('slotOf', () => {
  it('maps calendar months onto slots relative to now', () => {
    expect(slotOf('2026-11-01T00:00:00Z', NOW)).toBe(26)
    expect(slotOf('2026-10-31T23:59:59Z', NOW)).toBe(25)
    expect(slotOf('2025-11-15T00:00:00Z', NOW)).toBe(14)
    expect(slotOf('2026-12-01T00:00:00Z', NOW)).toBe(27)
  })
})

describe('placeOnAxis', () => {
  const design = Array.from({ length: AXIS_LEN }, (_, i) => 10 + i)

  it('replaces only slots with real points, last point in a month wins', () => {
    const r = placeOnAxis(design, [
      { t: '2026-11-08T00:39:12Z', value: 2.86 },
      { t: '2026-11-08T00:09:22Z', value: 2.8 },
      { t: '2026-09-03T00:00:00Z', value: 3.1 },
    ], NOW)
    expect(r.values[26]).toBe(2.86)
    expect(r.values[24]).toBe(3.1)
    expect(r.real[26]).toBe(true)
    expect(r.real[24]).toBe(true)
    expect(r.real[25]).toBe(false)
    expect(r.values[25]).toBe(35) // design value untouched, not blended
    expect(r.used).toBe(3)
    expect(realSpan(r.real)).toEqual({ count: 2, first: 24 })
  })

  it('ignores future and pre-axis points and null values', () => {
    const r = placeOnAxis(design, [
      { t: '2027-01-01T00:00:00Z', value: 9 },
      { t: '2020-01-01T00:00:00Z', value: 9 },
      { t: '2026-11-02T00:00:00Z', value: null },
    ], NOW)
    expect(r.values).toEqual(design)
    expect(r.real.every((x) => !x)).toBe(true)
    expect(r.used).toBe(0)
  })
})

describe('joinToNow (decision G14: illustrative months scaled to join the real present)', () => {
  const design = Array.from({ length: AXIS_LEN }, (_, i) => 2 + i / 10) // 2.0 .. 5.8, Now = 4.6
  const nowOnly = (value: number) => placeOnAxis(design, [{ t: '2026-11-08T00:39:12Z', value }], NOW)

  it('multiplicative: every illustrative slot is design x (real / design at Now); real slots untouched', () => {
    const j = joinToNow(nowOnly(2.3), design, 'multiply')
    const f = 2.3 / design[26]
    expect(j.values[26]).toBe(2.3)
    expect(j.real[26]).toBe(true)
    expect(j.values[0]).toBeCloseTo(design[0] * f, 2)
    expect(j.values[25]).toBeCloseTo(design[25] * f, 2)
    expect(j.values[38]).toBeCloseTo(design[38] * f, 2)
    expect(j.scaled).toBe(true)
    expect(j.forecastScale).toBeCloseTo(f, 9)
    expect(j.bandFactor).toBeCloseTo(f, 9)
  })

  it('keeps the design shape: ratios between illustrative slots are unchanged', () => {
    const j = joinToNow(nowOnly(9.2), design, 'multiply')
    expect(j.values[10] / j.values[5]).toBeCloseTo(design[10] / design[5], 6)
  })

  it('continuity at Now: the step into the real slot is the design step scaled, not the design-to-real gap', () => {
    const j = joinToNow(nowOnly(2.3), design, 'multiply')
    expect(j.values[26] - j.values[25]).toBeCloseTo((design[26] - design[25]) * (2.3 / design[26]), 6)
    expect(j.values[27] - j.values[26]).toBeCloseTo((design[27] - design[26]) * (2.3 / design[26]), 6)
  })

  it('shift with clamp (0..100 scores): illustrative slots move by (real - design at Now) and never leave the range', () => {
    const scores = Array.from({ length: AXIS_LEN }, (_, i) => 50 + i) // Now = 76
    const placed = placeOnAxis(scores, [{ t: '2026-11-08T00:39:12Z', value: 34.1 }], NOW)
    const j = joinToNow(placed, scores, 'shift', { clamp: [0, 100], digits: 1 })
    expect(j.values[26]).toBe(34.1)
    expect(j.values[25]).toBeCloseTo(75 + (34.1 - 76), 6)
    expect(j.values[38]).toBeCloseTo(88 + (34.1 - 76), 6)
    expect(j.values[0]).toBe(8.1)
    expect(j.bandFactor).toBe(1) // a shift does not change a band's width
    const up = joinToNow(placeOnAxis(scores, [{ t: '2026-11-08T00:39:12Z', value: 99 }], NOW), scores, 'shift', { clamp: [0, 100] })
    expect(up.values[38]).toBe(100)
    expect(Math.max(...up.values)).toBeLessThanOrEqual(100)
  })

  it('the uncertainty band scales with the series under multiply', () => {
    const j = joinToNow(nowOnly(9.2), design, 'multiply')
    const unc = 0.14
    expect(unc * j.bandFactor).toBeCloseTo(0.14 * 2, 6)
  })

  it('several real points: real slots replace the design, gaps interpolate between anchors, each join is continuous', () => {
    const placed = placeOnAxis(design, [
      { t: '2026-08-10T00:00:00Z', value: 6.0 }, // slot 23, design 4.3 -> f 1.395..
      { t: '2026-11-08T00:00:00Z', value: 2.3 }, // slot 26, design 4.6 -> f 0.5
    ], NOW)
    const j = joinToNow(placed, design, 'multiply')
    const fA = 6 / design[23]
    const fB = 2.3 / design[26]
    expect(j.values[23]).toBe(6)
    expect(j.values[26]).toBe(2.3)
    expect(j.values[22]).toBeCloseTo(design[22] * fA, 2)
    expect(j.values[24]).toBeCloseTo(design[24] * (fA + (fB - fA) / 3), 2)
    expect(j.values[25]).toBeCloseTo(design[25] * (fA + (2 * (fB - fA)) / 3), 2)
    expect(j.values[30]).toBeCloseTo(design[30] * fB, 2)
    expect(j.forecastScale).toBeCloseTo(fB, 9)
  })

  it('no real point: nothing is scaled and the design is returned as is', () => {
    const j = joinToNow(placeOnAxis(design, [], NOW), design, 'multiply')
    expect(j.values).toEqual(design)
    expect(j.scaled).toBe(false)
    expect(j.forecastScale).toBe(1)
    expect(j.bandFactor).toBe(1)
  })

  it('NaN design slots stay NaN; a zero or NaN design value at a real slot cannot anchor a multiply', () => {
    const d = design.map((v, i) => (i < 15 ? NaN : v))
    const j = placeAndJoin(d, [{ t: '2026-11-08T00:39:12Z', value: 2.3 }], 'multiply', { now: NOW })
    expect(Number.isNaN(j.values[3])).toBe(true)
    expect(j.values[20]).toBeCloseTo(design[20] * (2.3 / design[26]), 2)
    const zero = design.map((v, i) => (i === 26 ? 0 : v))
    const k = placeAndJoin(zero, [{ t: '2026-11-08T00:39:12Z', value: 2.3 }], 'multiply', { now: NOW })
    expect(k.values[26]).toBe(2.3)
    expect(k.values[25]).toBe(zero[25])
    expect(k.scaled).toBe(false)
  })

  it('never mutates its inputs', () => {
    const placed = nowOnly(2.3)
    const snapshot = JSON.stringify({ placed, design })
    joinToNow(placed, design, 'multiply')
    expect(JSON.stringify({ placed, design })).toBe(snapshot)
  })
})
