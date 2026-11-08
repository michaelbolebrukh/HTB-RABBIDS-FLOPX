// WF5 #7: the review card's index row takes its badge from the gpu/index payload's own provenance,
// never from the offers count beside it (a derived-from-sample index reads Illustrative, as the ticker does).
import { describe, expect, it } from 'vitest'
import type { GpuLive } from '@/data/core'
import { indexRow } from './data'

const live = (over: Partial<GpuLive>): GpuLive => ({
  modelId: 'h100-sxm-80gb', seriesId: 'index:fcpi:h100-sxm-80gb', current: 3.3368, freshness: 'live', observed: '08 Nov 05:37', real: [], scaled: false, bandFactor: 1, designIndex: [], chgPct: 1.21, chgWindow: 'since 06 Nov', coverage: { providers: 3, regions: 24, offerCount: 116 }, basket: [], limitations: [], methodology: 'median of adjusted asks', historyPoints: 7,
  ...over,
})

describe('indexRow', () => {
  it('reads illustrative when the backend derived the index from sample inputs, whatever the offers say', () => {
    const r = indexRow(live({ freshness: 'illustrative', freshLabel: 'Illustrative · sample inputs' }))
    expect(r.fresh).toBe('illustrative')
    expect(r.status).toBe('illustrative')
    expect(r.note).toContain('$3.34')
    expect(r.note).toContain('7 history points')
    expect(r.note).toContain('sample inputs')
  })
  it('reads live only when the index payload is live', () => {
    const r = indexRow(live({ freshness: 'live' }))
    expect(r.fresh).toBe('live')
    expect(r.status).toBe('real')
  })
  it('carries any other provenance through as partly real', () => {
    const r = indexRow(live({ freshness: 'historical', freshLabel: 'Historical' }))
    expect(r.fresh).toBe('historical')
    expect(r.status).toBe('partly')
  })
  it('is unavailable before the payload lands', () => {
    const r = indexRow(undefined)
    expect(r.fresh).toBe('unavailable')
    expect(r.status).toBe('illustrative')
  })
})
