import { describe, expect, it } from 'vitest'
import type { Candle } from '@/data/fund'
import { tail } from './indicators'
import { computeIndicator } from './indicatorMath'

const candles: Candle[] = Array.from({ length: 115 }, (_, i) => { const c = 100 + i + 5 * Math.sin(i / 3); return { o: c - 1, h: c + 2, l: c - 2, c } })
const volume = candles.map((_, i) => 1000 + (i % 7) * 100)

describe('tail', () => {
  it('keeps the last n points of every line and band', () => {
    const full = computeIndicator('bb', candles, volume)
    const t = tail(full, 52)
    t.lines.forEach((l, k) => expect(l.values).toEqual(full.lines[k].values.slice(-52)))
    expect(t.band!.upper).toEqual(full.band!.upper.slice(-52))
    expect(t.band!.lower).toEqual(full.band!.lower.slice(-52))
    expect(t.guides).toEqual(full.guides)
  })

  it('warms indicators up from the first visible bar when computed on the full history', () => {
    const n = 52
    for (const id of ['bb', 'donchian', 'rsi'] as const) {
      const windowOnly = computeIndicator(id, candles.slice(-n), volume.slice(-n))
      const fromHistory = tail(computeIndicator(id, candles, volume), n)
      expect(windowOnly.lines[0].values[0]).toBeNull()
      expect(fromHistory.lines[0].values[0]).not.toBeNull()
    }
  })

  it('returns everything when n covers the whole series', () => {
    const full = computeIndicator('rsi', candles, volume)
    expect(tail(full, 500).lines[0].values).toEqual(full.lines[0].values)
  })
})
