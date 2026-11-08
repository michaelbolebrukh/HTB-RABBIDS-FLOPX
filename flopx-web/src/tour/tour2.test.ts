// N-TOUR2: the region the modelling beats stand on (most factors with data, never a region whose
// factors all read "no data", US East and Midwest as the stated fallback) and the placement rule that
// prefers lighting a tall target's top part over a shrunk, scrolling card at 1440 x 900.
import { describe, expect, it } from 'vitest'
import type { Region } from '@/data/core'
import { FALLBACK_REGION_ID, pickRegion } from './data'
import { intersection, placeCard } from './geometry'

const region = (id: string, score: number, contributions: (number | null)[], backendId = id): Region =>
  ({ id, name: id, live: { backendId, pressure: { score, factorsTotal: 6, factors: contributions.map((c, i) => ({ factor: `f${i}`, contribution: c, weight: 0.1, note: '', freshness: 'live' })) } } }) as unknown as Region

describe('pickRegion (N-TOUR2 #2)', () => {
  it('chooses the region with the most factors carrying data, not the highest score', () => {
    const tight = region('ap-sg', 71, [1, null, null, null, null, null])
    const fuller = region('ap-jp', 41, [1, 1, 1, null, 1, 1])
    const p = pickRegion([tight, fuller])
    expect(p.region?.id).toBe('ap-jp')
    expect(p.withData).toBe(5)
    expect(p.why).toBe('5 of 6 factors carry data, the most of the 2 scored regions')
  })
  it('breaks a tie on score and says so', () => {
    const a = region('a', 40, [1, 1, null, null, null, null])
    const b = region('b', 55, [null, 1, 1, null, null, null])
    const p = pickRegion([a, b])
    expect(p.region?.id).toBe('b')
    expect(p.why).toContain('(2 tie; highest score wins)')
  })
  it('never picks a region whose six factors all read no data; US East and Midwest is the fallback', () => {
    const empty = region('ap-sg', 90, [null, null, null, null, null, null])
    const p = pickRegion([empty])
    expect(p.region?.id).not.toBe('ap-sg')
    expect(p.region?.id).toBe(FALLBACK_REGION_ID)
    expect(p.withData).toBe(0)
    expect(p.why).toContain('US East and Midwest is the stated fallback')
  })
  it('with no scored region at all falls back to the design region and says the feed has not landed', () => {
    const p = pickRegion([])
    expect(p.region?.id).toBe(FALLBACK_REGION_ID)
    expect(p.why).toContain('has not landed')
  })
})

const vp = { w: 1440, h: 900 }
const opts = { viewport: vp, railH: 72, pad: 10, gap: 20 }

describe('placeCard prefers a natural-height card over a shrunk one (N-TOUR2 #1)', () => {
  it('G-R 5 / MV 6: lights the top of the 378 px harness panel so the 430 px card keeps its height', () => {
    // The dim measured 230,66,1196,378 (padded); the card is 720 x 430 at its natural size.
    const p = placeCard({ x: 240, y: 76, w: 1176, h: 358 }, { ...opts, card: { w: 720, h: 430 }, prefer: ['above', 'below'] })
    expect(p.clipped).toBe(true)
    expect(p.shrunk).toBe(false)
    expect(p.side).toBe('below')
    expect(p.card.h).toBe(430)
    expect(p.spotlight.h).toBeGreaterThanOrEqual(120)
    expect(intersection(p.card, p.spotlight)).toBe(0)
  })
  it('keeps the shrunk band when clipping could not keep 120 px of the target lit', () => {
    // The whole quotes table at the foot of Markets: 439 px tall from y 346; nothing fits a 424 px card.
    const p = placeCard({ x: 240, y: 346, w: 1176, h: 419 }, { ...opts, card: { w: 780, h: 424 }, prefer: ['above', 'left'] })
    expect(p.overlaps).toBe(false)
    expect(p.shrunk).toBe(true)
    expect(p.clipped).toBe(false)
    expect(intersection(p.card, p.spotlight)).toBe(0)
  })
  it('a band that fits the natural height beats a larger band that would not', () => {
    // Target in the top-right corner: the band below is shorter than the card, the band to the left is taller but narrower.
    const p = placeCard({ x: 900, y: 40, w: 500, h: 500 }, { ...opts, card: { w: 600, h: 560 }, prefer: ['below'] })
    expect(p.side).toBe('left')
    expect(p.card.h).toBe(560)
    expect(intersection(p.card, p.spotlight)).toBe(0)
  })
})
