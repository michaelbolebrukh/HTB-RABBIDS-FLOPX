import { describe, expect, it } from 'vitest'
import { SECURITIES, secBars, secByTicker } from './finance'

describe('secBars', () => {
  it('returns one bar, volume and drawdown per weekly close', () => {
    for (const s of SECURITIES) {
      const b = secBars(s)
      expect(b.candles).toHaveLength(s.px.length)
      expect(b.volume).toHaveLength(s.px.length)
      expect(b.drawdown).toHaveLength(s.px.length)
    }
  })

  it('closes on the weekly close and opens on the previous close', () => {
    const s = secByTicker('VST')!
    const { candles } = secBars(s)
    candles.forEach((k, i) => {
      expect(k.c).toBe(s.px[i])
      if (i) expect(k.o).toBe(s.px[i - 1])
    })
  })

  it('keeps every bar well-formed', () => {
    for (const s of SECURITIES) {
      secBars(s).candles.forEach((k) => {
        expect(k.h).toBeGreaterThanOrEqual(Math.max(k.o, k.c))
        expect(k.l).toBeLessThanOrEqual(Math.min(k.o, k.c))
        expect(k.l).toBeGreaterThan(0)
      })
    }
  })

  it('gives traded names positive volume and indices none', () => {
    for (const s of SECURITIES) {
      const { volume } = secBars(s)
      if (s.group === 'Index') volume.forEach((v) => expect(v).toBe(0))
      else volume.forEach((v) => expect(v).toBeGreaterThan(0))
    }
  })

  it('measures drawdown from the running peak: never positive, zero at a new high', () => {
    const s = secByTicker('NVDA')!
    const { drawdown } = secBars(s)
    let peak = -Infinity
    s.px.forEach((p, i) => {
      expect(drawdown[i]).toBeLessThanOrEqual(0)
      if (p >= peak) expect(drawdown[i]).toBeCloseTo(0, 9)
      peak = Math.max(peak, p)
    })
  })

  it('is deterministic per ticker', () => {
    const s = secByTicker('CRWV')!
    expect(secBars(s)).toBe(secBars(s))
    expect(secBars(s).candles).not.toEqual(secBars(secByTicker('NBIS')!).candles)
  })

  // WF8 #4: signed in, the header showed the archive's close (186.70) while the candle readout kept the
  // design's (168.19), because the bars were cached by ticker before applyCloses rewrote the closes.
  it('follows the closes when they are rewritten in place, so the readout close is the header close', () => {
    const s = secByTicker('NVDA')!
    const before = secBars(s)
    const design = s.px.at(-1)!
    expect(before.candles.at(-1)!.c).toBe(design)
    s.px[s.px.length - 1] = 186.7
    const after = secBars(s)
    expect(after).not.toBe(before)
    expect(after.candles.at(-1)!.c).toBe(186.7)
    expect(after.candles.at(-1)!.o).toBe(s.px.at(-2)) // open stays the previous week's close
    expect(secBars(s)).toBe(after) // unchanged closes, same object
    s.px[s.px.length - 1] = design
    expect(secBars(s).candles.at(-1)!.c).toBe(design)
  })
})
