import { describe, expect, it } from 'vitest'
import { centredCard, connectorFor, placeCard, railProgress, railReserve, spotlightRect } from './geometry'

const vp = { w: 1440, h: 900 }
const card = { w: 470, h: 260 }

describe('spotlightRect', () => {
  it('pads the target and clamps to the viewport', () => {
    expect(spotlightRect({ x: 100, y: 50, w: 200, h: 40 }, vp, 8)).toEqual({ x: 92, y: 42, w: 216, h: 56 })
    expect(spotlightRect({ x: 0, y: 0, w: 50, h: 50 }, vp, 8)).toEqual({ x: 0, y: 0, w: 58, h: 58 })
    const r = spotlightRect({ x: 1400, y: 880, w: 200, h: 200 }, vp, 8)
    expect(r.x + r.w).toBeLessThanOrEqual(vp.w)
    expect(r.y + r.h).toBeLessThanOrEqual(vp.h)
  })
  it('keeps a degenerate target degenerate', () => {
    expect(spotlightRect({ x: 10, y: 10, w: 0, h: 0 }, vp, 0)).toEqual({ x: 10, y: 10, w: 0, h: 0 })
  })
})

describe('placeCard', () => {
  it('centres the card and cuts nothing out when there is no target', () => {
    const p = placeCard(null, { viewport: vp, card, railH: 64 })
    expect(p.side).toBe('centre')
    expect(p.connector).toBeNull()
    expect(p.spotlight.w).toBe(0)
    expect(p.card).toEqual(centredCard({ viewport: vp, card, railH: 64 }))
    expect(p.card.x + card.w / 2).toBeCloseTo(vp.w / 2)
  })
  it('puts the card to the right of a target with room there, without covering it', () => {
    const target = { x: 200, y: 300, w: 300, h: 120 }
    const p = placeCard(target, { viewport: vp, card, railH: 64, gap: 20, pad: 10 })
    expect(p.side).toBe('right')
    expect(p.card.x).toBe(p.spotlight.x + p.spotlight.w + 20)
    // Never overlaps the spotlight.
    expect(p.card.x).toBeGreaterThanOrEqual(p.spotlight.x + p.spotlight.w)
    // Connector goes from the card's left edge to the spotlight's right edge.
    expect(p.connector!.from.x).toBe(p.card.x)
    expect(p.connector!.to.x).toBe(p.spotlight.x + p.spotlight.w)
  })
  it('falls through the preference order when the preferred side would clip', () => {
    const target = { x: 1000, y: 300, w: 400, h: 120 } // right edge at 1400: no room on the right
    const p = placeCard(target, { viewport: vp, card, railH: 64, prefer: ['right', 'left', 'below', 'above'] })
    expect(p.side).toBe('left')
    expect(p.card.x + p.card.w).toBeLessThanOrEqual(p.spotlight.x)
  })
  it('honours an explicit preference such as below, sliding along the axis to stay inside the viewport', () => {
    const target = { x: 0, y: 48, w: 1440, h: 28 } // the ticker: full width, top of the page
    const p = placeCard(target, { viewport: vp, card, railH: 64, prefer: ['below'] })
    expect(p.side).toBe('below')
    expect(p.card.y).toBeGreaterThanOrEqual(p.spotlight.y + p.spotlight.h)
    expect(p.card.x).toBeGreaterThanOrEqual(16)
    expect(p.card.x + p.card.w).toBeLessThanOrEqual(vp.w - 16)
  })
  it('keeps the card above the progress rail', () => {
    const target = { x: 100, y: 820, w: 300, h: 60 } // near the bottom
    const p = placeCard(target, { viewport: vp, card, railH: 64, prefer: ['below', 'right'] })
    expect(p.card.y + p.card.h).toBeLessThanOrEqual(vp.h - 64 - 16)
  })
  it('pins the least-clipping side when nothing fits (a tiny viewport)', () => {
    const p = placeCard({ x: 100, y: 100, w: 400, h: 300 }, { viewport: { w: 600, h: 500 }, card: { w: 470, h: 260 }, railH: 64 })
    expect(p.card.x).toBeGreaterThanOrEqual(16)
    expect(p.card.y).toBeGreaterThanOrEqual(16)
    expect(p.card.x + p.card.w).toBeLessThanOrEqual(600 - 16)
  })
})

describe('railReserve (WF5 #3)', () => {
  it('reserves down to the rail\'s measured top, never less than the nominal height', () => {
    expect(railReserve(900, 828, 64)).toBe(72) // the 1440 rail: 60 px tall plus 12 px of bottom padding
    expect(railReserve(900, 836, 64)).toBe(64)
    expect(railReserve(900, 850, 64)).toBe(64)
  })
  it('falls back to the nominal height before the rail is measured or when the measurement is nonsense', () => {
    expect(railReserve(900, null, 64)).toBe(64)
    expect(railReserve(900, undefined, 44)).toBe(44)
    expect(railReserve(900, 0, 64)).toBe(64)
    expect(railReserve(900, 900, 64)).toBe(64)
    expect(railReserve(900, NaN, 64)).toBe(64)
  })
  it('keeps the cut-out above the measured rail: a target running to the bottom ends at 828, not 836', () => {
    const target = { x: 580, y: 76, w: 836, h: 980 } // G-R 9's Ranked options table
    const p = placeCard(target, { viewport: vp, card: { w: 470, h: 334 }, railH: railReserve(900, 828, 64), prefer: ['left'], pad: 10, gap: 20 })
    expect(p.spotlight.y + p.spotlight.h).toBeLessThanOrEqual(828)
    const q = placeCard(target, { viewport: vp, card: { w: 470, h: 334 }, railH: 64, prefer: ['left'], pad: 10, gap: 20 })
    expect(q.spotlight.y + q.spotlight.h).toBe(836) // the old clamp, 8 px under the rail
  })
})

describe('connectorFor', () => {
  it('ends on the spotlight edge nearest the card', () => {
    const s = { x: 100, y: 100, w: 200, h: 100 }
    const c = connectorFor({ x: 340, y: 60, w: 300, h: 200 }, s, 'right')
    expect(c!.to).toEqual({ x: 300, y: 108 })
    const b = connectorFor({ x: 50, y: 240, w: 300, h: 200 }, s, 'below')
    expect(b!.to).toEqual({ x: 200, y: 200 })
    expect(connectorFor({ x: 0, y: 0, w: 1, h: 1 }, s, 'centre')).toBeNull()
  })
})

describe('railProgress', () => {
  it('runs 0..1 across the beats and is 1 for a single beat', () => {
    expect(railProgress(0, 5)).toBe(0)
    expect(railProgress(4, 5)).toBe(1)
    expect(railProgress(2, 5)).toBe(0.5)
    expect(railProgress(0, 1)).toBe(1)
  })
})
