import { describe, expect, it } from 'vitest'
import { beeswarm } from './charts'

const minGap = (xs: number[], ys: number[]) => {
  let m = Infinity
  for (let i = 0; i < xs.length; i++) for (let j = i + 1; j < xs.length; j++) m = Math.min(m, Math.hypot(xs[i] - xs[j], ys[i] - ys[j]))
  return m
}

describe('beeswarm', () => {
  it('keeps well-separated dots on the axis', () => {
    expect(beeswarm([0, 50, 100], 4)).toEqual([0, 0, 0])
  })

  it('stacks dots at the same x without overlap', () => {
    const xs = [10, 10, 10, 10, 10]
    const ys = beeswarm(xs, 4)
    expect(minGap(xs, ys)).toBeGreaterThanOrEqual(9)
    expect(ys[0]).toBe(0)
  })

  it('keeps every pair at least one diameter apart in a dense cluster', () => {
    const xs = [100, 101, 103, 104, 104, 106, 108, 109, 112, 115, 116]
    expect(minGap(xs, beeswarm(xs, 4))).toBeGreaterThanOrEqual(9 - 1e-9)
  })

  it('returns offsets in input order, independent of x order', () => {
    const ys = beeswarm([50, 10, 10], 4)
    expect(ys[0]).toBe(0)
    expect(ys[1]).toBe(0)
    expect(Math.abs(ys[2])).toBeGreaterThan(0)
  })

  it('never exceeds maxOff (falls back to the axis instead)', () => {
    const xs = Array.from({ length: 20 }, () => 0)
    const ys = beeswarm(xs, 4, 10)
    expect(Math.max(...ys.map(Math.abs))).toBeLessThanOrEqual(10)
  })

  it('handles empty input', () => {
    expect(beeswarm([], 4)).toEqual([])
  })
})
