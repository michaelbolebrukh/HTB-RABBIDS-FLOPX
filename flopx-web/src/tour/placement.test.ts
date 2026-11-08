// The placement rule WF4 #9 and #10 asked for: the card never covers the lit element, the cut-out
// never runs under the rail, and the seven 1440 x 900 cases WF4 measured as covered now place clear.
import { describe, expect, it } from 'vitest'
import { intersection, placeCard, placeSheet, spotlightRect } from './geometry'

const vp = { w: 1440, h: 900 }
const RAIL = 64
const opts = { viewport: vp, railH: RAIL, pad: 10, gap: 20 }

/** Spotlight (as the dim measured it, already padded) and card size, from wf4/t1440b/results.json. */
const WF4_COVERED: { name: string; spot: { x: number; y: number; w: number; h: number }; card: { w: number; h: number }; prefer: ('right' | 'left' | 'below' | 'above')[] }[] = [
  { name: 'G-R 2 / MV 3 truth-system grid', spot: { x: 230, y: 219, w: 1196, h: 288 }, card: { w: 470, h: 664 }, prefer: ['below'] },
  { name: 'G-R 5 / MV 6 harness table', spot: { x: 230, y: 297, w: 1196, h: 382 }, card: { w: 470, h: 580 }, prefer: ['above', 'below'] },
  { name: 'HRT 1 / MV 2 offers table', spot: { x: 230, y: 298, w: 1196, h: 548 }, card: { w: 470, h: 392 }, prefer: ['above', 'left'] },
  { name: 'HRT 2 price distribution', spot: { x: 230, y: 305, w: 796, h: 366 }, card: { w: 470, h: 655 }, prefer: ['above', 'right'] },
  { name: 'HRT 3 edge banner', spot: { x: 230, y: 191, w: 1196, h: 102 }, card: { w: 470, h: 600 }, prefer: ['below'] },
]
// The dim rect is the padded cut-out; undo the padding to get the target the stage measured.
const unpad = (s: { x: number; y: number; w: number; h: number }, pad: number) => ({ x: s.x + pad, y: s.y + pad, w: s.w - 2 * pad, h: s.h - 2 * pad })

describe('placeCard never covers the lit element', () => {
  for (const c of WF4_COVERED) {
    it(`places clear of the spotlight: ${c.name}`, () => {
      const p = placeCard(unpad(c.spot, 10), { ...opts, card: c.card, prefer: c.prefer })
      expect(p.overlaps).toBe(false)
      expect(intersection(p.card, p.spotlight)).toBe(0)
      // Inside the usable area, above the rail.
      expect(p.card.x).toBeGreaterThanOrEqual(16)
      expect(p.card.x + p.card.w).toBeLessThanOrEqual(vp.w - 16)
      expect(p.card.y).toBeGreaterThanOrEqual(16)
      expect(p.card.y + p.card.h).toBeLessThanOrEqual(vp.h - RAIL - 16)
      // Shrinking is reported, and never below the minimum worth reading.
      if (p.card.h < c.card.h || p.card.w < c.card.w) expect(p.shrunk).toBe(true)
      expect(p.card.h).toBeGreaterThanOrEqual(180)
      expect(p.card.w).toBeGreaterThanOrEqual(300)
    })
  }
  it('takes the preferred side at full size when it fits', () => {
    const p = placeCard({ x: 200, y: 300, w: 300, h: 120 }, { ...opts, card: { w: 470, h: 260 }, prefer: ['right'] })
    expect(p.side).toBe('right')
    expect(p.shrunk).toBe(false)
    expect(p.card.x).toBe(p.spotlight.x + p.spotlight.w + 20)
  })
  it('shrinks into the largest free band when no band takes the card whole', () => {
    // A banner across the page with 600 px of card: below has 507 px, above 155 px.
    const p = placeCard(unpad({ x: 230, y: 191, w: 1196, h: 102 }, 10), { ...opts, card: { w: 470, h: 600 }, prefer: ['below'] })
    expect(p.side).toBe('below')
    expect(p.shrunk).toBe(true)
    expect(p.card.h).toBeLessThan(600)
    expect(p.card.y).toBe(p.spotlight.y + p.spotlight.h + 20)
  })
  it('clips a tall full-width target from below so the card can sit under its lit top', () => {
    // The offers table scrolled to the top of the page: 548 px tall from y 80, no free side band.
    const target = { x: 240, y: 90, w: 1176, h: 548 }
    const p = placeCard(target, { ...opts, card: { w: 470, h: 392 }, prefer: ['above', 'left'] })
    expect(p.clipped).toBe(true)
    expect(p.side).toBe('below')
    expect(p.spotlight.h).toBeLessThan(548)
    expect(p.spotlight.h).toBeGreaterThanOrEqual(120)
    expect(intersection(p.card, p.spotlight)).toBe(0)
    expect(p.overlaps).toBe(false)
  })
  it('reports an overlap honestly only when no band is usable at all', () => {
    const p = placeCard({ x: 0, y: 0, w: 1440, h: 900 }, { ...opts, card: { w: 470, h: 400 } })
    // The spotlight is clipped first; a 900 px target leaves a band under its lit top.
    expect(p.overlaps).toBe(false)
    const tiny = placeCard({ x: 0, y: 0, w: 600, h: 500 }, { viewport: { w: 600, h: 500 }, railH: 64, card: { w: 470, h: 400 }, pad: 10, gap: 20, minCard: { w: 580, h: 400 } })
    expect(tiny.side).toBe('over')
    expect(tiny.overlaps).toBe(true)
  })
})

describe('the cut-out stays above the rail (WF4 #10)', () => {
  it('clamps a spotlight that would run under the rail', () => {
    const s = spotlightRect({ x: 240, y: 654, w: 1176, h: 243 }, vp, 10, vp.h - RAIL)
    expect(s.y + s.h).toBeLessThanOrEqual(vp.h - RAIL)
    const p = placeCard({ x: 240, y: 654, w: 1176, h: 243 }, { ...opts, card: { w: 470, h: 320 }, prefer: ['above'] })
    expect(p.spotlight.y + p.spotlight.h).toBeLessThanOrEqual(vp.h - RAIL)
    expect(intersection(p.card, p.spotlight)).toBe(0)
  })
})

describe('placeSheet (below 768 px, WF4 #11)', () => {
  const m = { w: 390, h: 844 }
  it('is a full-width sheet above the rail, and the cut-out never reaches under it', () => {
    const p = placeSheet({ x: 216, y: 300, w: 174, h: 900 }, { viewport: m, card: { w: 390, h: 700 }, railH: 44, sheetH: Math.floor(844 * 0.45), pad: 6 })
    expect(p.side).toBe('sheet')
    expect(p.card).toEqual({ x: 0, y: 844 - 44 - 379, w: 390, h: 379 })
    expect(p.card.h).toBeLessThanOrEqual(844 * 0.45)
    expect(p.spotlight.y + p.spotlight.h).toBeLessThanOrEqual(p.card.y)
    expect(intersection(p.card, p.spotlight)).toBe(0)
    expect(p.clipped).toBe(true)
    expect(p.connector).toBeNull()
  })
  it('centres nothing: with no target the dim covers the page and the sheet stays put', () => {
    const p = placeSheet(null, { viewport: m, card: { w: 390, h: 200 }, railH: 44, sheetH: 200, pad: 6 })
    expect(p.side).toBe('centre')
    expect(p.spotlight.w).toBe(0)
    expect(p.card.y).toBe(844 - 44 - 200)
  })
})
