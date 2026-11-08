import { describe, expect, it } from 'vitest'
import { TOKENS } from './handoff'
import { ENDPOINTS, makeEndpoints, median, selectProvider, type Endpoint } from './intelligence'

const ep = (provider: string, price: number, bits: 0 | 8 | 16 = 8, contextK = 128, uptime = 99): Endpoint => ({ provider, price, bits, contextK, uptime, real: false })
const any = { minBits: 0 as const, minContextK: 0, minUptime: 0 }

describe('selectProvider', () => {
  const eps = [ep('a', 0.5, 16, 256, 99.9), ep('b', 0.2, 8, 64, 95), ep('c', 0.3, 0, 128, 99), ep('d', 0.4, 8, 128, 98)]
  it('picks the cheapest with no needs', () => expect(selectProvider(eps, any).best?.provider).toBe('b'))
  it('respects precision and excludes unstated', () => {
    expect(selectProvider(eps, { ...any, minBits: 8 }).best?.provider).toBe('b')
    expect(selectProvider(eps, { ...any, minBits: 16 }).best?.provider).toBe('a')
    expect(selectProvider(eps, { ...any, minBits: 8 }).qualifying).toBe(3)
  })
  it('respects context and uptime', () => {
    expect(selectProvider(eps, { ...any, minContextK: 128 }).best?.provider).toBe('c')
    expect(selectProvider(eps, { ...any, minUptime: 98 }).best?.provider).toBe('c')
    expect(selectProvider(eps, { minBits: 8, minContextK: 128, minUptime: 98 }).best?.provider).toBe('d')
  })
  it('returns null when nothing qualifies', () => {
    const s = selectProvider(eps, { minBits: 16, minContextK: 256, minUptime: 99.95 })
    expect(s.best).toBeNull(); expect(s.vsMedianPct).toBeNull(); expect(s.qualifying).toBe(0)
  })
  it('handles empty input', () => expect(selectProvider([], any).best).toBeNull())
  it('breaks price ties by uptime', () => expect(selectProvider([ep('x', 1, 8, 128, 95), ep('y', 1, 8, 128, 99)], any).best?.provider).toBe('y'))
  it('reports price vs median', () => expect(selectProvider(eps, any).vsMedianPct).toBeCloseTo((0.2 / 0.35 - 1) * 100, 6))
})

describe('median', () => {
  it('odd and even', () => { expect(median([3, 1, 2])).toBe(2); expect(median([1, 2, 3, 4])).toBe(2.5) })
  it('empty is NaN', () => expect(median([])).toBeNaN())
})

describe('ENDPOINTS', () => {
  const { cheapest, dearest, providers } = TOKENS.spread
  it('has the real count and real ends', () => {
    expect(ENDPOINTS).toHaveLength(providers)
    expect(ENDPOINTS.filter((e) => e.real).map((e) => [e.provider, e.price])).toEqual([[cheapest.provider, cheapest.price], [dearest.provider, dearest.price]])
  })
  it('keeps synthetic prices inside the real range, strictly', () => {
    for (const e of ENDPOINTS.filter((x) => !x.real)) { expect(e.price).toBeGreaterThan(cheapest.price); expect(e.price).toBeLessThan(dearest.price) }
  })
  it('is deterministic', () => expect(makeEndpoints()).toEqual(makeEndpoints()))
  it('has unique provider names', () => expect(new Set(ENDPOINTS.map((e) => e.provider)).size).toBe(providers))
})
