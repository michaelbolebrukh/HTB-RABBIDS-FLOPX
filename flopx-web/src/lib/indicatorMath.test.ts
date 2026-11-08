import { describe, expect, it } from 'vitest'
import type { Candle } from '@/data/fund'
import { INDICATORS } from './indicators'
import { computeIndicator, ema, sma, stdev, trueRange, wilder, wma } from './indicatorMath'

const seq = (n: number) => Array.from({ length: n }, (_, i) => i + 1)
const flat = (n: number, c = 100, half = 1): Candle[] => Array.from({ length: n }, () => ({ o: c, h: c + half, l: c - half, c }))
const fromCloses = (cs: number[]): Candle[] => cs.map((c) => ({ o: c, h: c + 1, l: c - 1, c }))
const last = <T,>(a: T[]) => a[a.length - 1]

function walk(n: number, seed = 42): { candles: Candle[]; volume: number[] } {
  let s = seed
  const rnd = () => { s = (s + 0x6d2b79f5) | 0; let t = Math.imul(s ^ (s >>> 15), 1 | s); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296 }
  let p = 100
  const candles: Candle[] = []
  const volume: number[] = []
  for (let i = 0; i < n; i++) {
    const o = p
    p = Math.max(1, p + (rnd() - 0.5) * 4)
    candles.push({ o, h: Math.max(o, p) + rnd(), l: Math.min(o, p) - rnd(), c: p })
    volume.push(1000 + Math.floor(rnd() * 500))
  }
  return { candles, volume }
}

describe('helpers', () => {
  it('sma of 1..10', () => {
    const r = sma(seq(10), 3)
    expect(r.slice(0, 2)).toEqual([null, null])
    expect(r[2]).toBeCloseTo(2)
    expect(r[9]).toBeCloseTo(9)
  })
  it('ema seeds with sma and uses 2/(n+1)', () => {
    const r = ema(seq(10), 3)
    expect(r[1]).toBeNull()
    expect(r[2]).toBeCloseTo(2)
    expect(r[3]).toBeCloseTo(3) // 4*0.5 + 2*0.5
    expect(r[9]).toBeCloseTo(9)
  })
  it('wma of 1..10', () => {
    const r = wma(seq(10), 3)
    expect(r[2]).toBeCloseTo((1 + 4 + 9) / 6)
    expect(r[9]).toBeCloseTo((8 + 18 + 30) / 6)
  })
  it('stdev population and sample', () => {
    expect(stdev([2, 4, 4, 4, 5, 5, 7, 9], 8)[7]).toBeCloseTo(2)
    expect(stdev([1, 3], 2, true)[1]).toBeCloseTo(Math.SQRT2)
  })
  it('wilder seeds with mean then smooths', () => {
    const r = wilder([2, 4, 6, 8], 2)
    expect(r[1]).toBeCloseTo(3)
    expect(r[2]).toBeCloseTo(4.5)
    expect(r[3]).toBeCloseTo(6.25)
  })
  it('true range uses previous close', () => {
    const tr = trueRange([{ o: 1, h: 2, l: 1, c: 2 }, { o: 5, h: 6, l: 5, c: 5 }])
    expect(tr).toEqual([1, 4])
  })
  it('handles short inputs', () => {
    expect(sma([1, 2], 5)).toEqual([null, null])
    expect(ema([], 5)).toEqual([])
    expect(wilder([1], 3)).toEqual([null])
  })
})

describe('computeIndicator', () => {
  it('RSI is 100 on rising closes and ~50 on alternating', () => {
    const up = computeIndicator('rsi', fromCloses(seq(40)), []).lines[0].values
    expect(up[13]).toBeNull()
    expect(last(up)).toBeCloseTo(100)
    const alt = fromCloses(Array.from({ length: 60 }, (_, i) => 100 + (i % 2)))
    expect(Math.abs((last(computeIndicator('rsi', alt, []).lines[0].values) as number) - 50)).toBeLessThan(3)
  })
  it('Bollinger width is 0 on a constant series', () => {
    const r = computeIndicator('bb', fromCloses(Array(30).fill(50)), [])
    expect(last(r.band!.upper)).toBeCloseTo(50)
    expect(last(r.band!.lower)).toBeCloseTo(50)
    expect(r.band!.upper[18]).toBeNull()
  })
  it('ATR equals the range on constant-range candles', () => {
    const v = computeIndicator('atr', flat(40, 100, 1.5), []).lines[0].values
    expect(v[12]).toBeNull()
    expect(last(v)).toBeCloseTo(3)
  })
  it('OBV follows close direction', () => {
    const r = computeIndicator('obv', fromCloses([1, 2, 2, 1, 3]), [10, 20, 30, 40, 50]).lines[0].values
    expect(r).toEqual([0, 20, 20, -20, 30])
  })
  it('Williams %R is 0 at the range high', () => {
    const cs: Candle[] = seq(20).map((i) => ({ o: i, h: i, l: i - 1, c: i }))
    const v = computeIndicator('willr', cs, []).lines[0].values
    expect(last(v)).toBeCloseTo(0)
  })
  it('Donchian is rolling max high / min low', () => {
    const { candles } = walk(60)
    const r = computeIndicator('donchian', candles, [])
    const w = candles.slice(-20)
    expect(last(r.band!.upper)).toBeCloseTo(Math.max(...w.map((b) => b.h)))
    expect(last(r.band!.lower)).toBeCloseTo(Math.min(...w.map((b) => b.l)))
  })
  it('MACD histogram = macd - signal', () => {
    const { candles } = walk(120)
    const r = computeIndicator('macd', candles, [])
    const [h, m, s] = r.lines.map((l) => l.values)
    expect(r.lines[0].kind).toBe('histogram')
    expect(last(h)).toBeCloseTo((last(m) as number) - (last(s) as number))
    expect(h[32]).toBeNull()
    expect(h[33]).not.toBeNull()
  })
  it('Aroon is 100 up on a rising series', () => {
    const r = computeIndicator('aroon', fromCloses(seq(30)), [])
    expect(last(r.lines[0].values)).toBeCloseTo(100)
    expect(last(r.lines[1].values)).toBeCloseTo(0)
  })
  it('PSAR stays below price in a steady uptrend', () => {
    const cs = fromCloses(seq(30))
    const v = computeIndicator('psar', cs, []).lines[0].values
    expect(last(v) as number).toBeLessThan(last(cs).l)
  })

  describe.each(INDICATORS.map((d) => d.id))('%s', (id) => {
    it('has aligned, finite output on a random walk', () => {
      const { candles, volume } = walk(120)
      const r = computeIndicator(id, candles, volume)
      const all = [...r.lines.map((l) => l.values), ...(r.band ? [r.band.upper, r.band.lower] : [])]
      expect(all.length).toBeGreaterThan(0)
      for (const s of all) {
        expect(s).toHaveLength(120)
        expect(s.every((x) => x === null || Number.isFinite(x))).toBe(true)
        expect(s.some((x) => x !== null)).toBe(true)
      }
    })
    it('survives empty, short and flat inputs', () => {
      for (const cs of [[], flat(1), flat(5), flat(60)]) {
        const r = computeIndicator(id, cs, cs.map(() => 0))
        for (const l of r.lines) {
          expect(l.values).toHaveLength(cs.length)
          expect(l.values.every((x) => x === null || Number.isFinite(x))).toBe(true)
        }
      }
    })
  })
})
