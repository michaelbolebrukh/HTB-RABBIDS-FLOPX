import { describe, expect, it } from 'vitest'
import type { Gpu, Offer, Provider } from '@/data/core'
import { applyIndexToGpu, applyOffers, applyPressure, change30, GDH_ATTRIBUTION, GDH_BADGE_LABEL, gpuCardFigures, mapContract, mapOffer, monthlyMeans, offerRegionName, offerTotals, OFFER_TOTALS } from './gpu'
import { setHubNames } from './regions'
import { LIVE_TOTALS } from './store'
import offersFixture from './__fixtures__/w5/offers-h100-h200.json'
import type { GpuIndexWire, OfferWire, OffersResponse, PressureResponse } from './types'

// Trimmed from real /flopx/* responses captured 08 Nov 2026.
const NOW = new Date('2026-11-08T00:39:13Z')
const prov = (kind: 'live' | 'sample' | 'derived' | 'archived', extra: Partial<OfferWire['provenance']> = {}) => ({ kind, source: 'x', observedAt: '2026-11-08T00:39:13Z', ...extra })

const H100_INDEX: GpuIndexWire = {
  id: 'index:fcpi:h100-sxm-80gb',
  gpuModelId: 'h100-sxm-80gb',
  methodology: 'Median of comparable asks per GPU model.',
  coverage: { providers: 2, regions: 1, offerCount: 3 },
  normalisation: [],
  lastUpdated: '2026-11-08T00:39:12Z',
  values: [
    { seriesId: 'index:fcpi:h100-sxm-80gb', t: '2026-11-08T00:09:22Z', value: 2.8, unit: 'USD/GPU-hour', provenance: prov('archived', { inputKinds: ['live'] }) },
    { seriesId: 'index:fcpi:h100-sxm-80gb', t: '2026-11-08T00:39:12Z', value: 2.86, unit: 'USD/GPU-hour', provenance: prov('derived', { inputKinds: ['live'] }) },
  ],
  limitations: ['fewer than five offers behind the median'],
  provenance: { kind: 'derived', source: 'fcpi-v1', observedAt: '2026-11-08T00:39:12Z', inputKinds: ['live'] },
  current: 2.86,
  basket: ['akash', 'runpod'],
  breaks: [],
}

const gpu = (id: string): Gpu => ({ id, name: id, arch: 'x', memory: 'x', index: Array.from({ length: 39 }, (_, i) => 1 + i / 100), unc: 0.1, pressure: 50 })

describe('applyIndexToGpu', () => {
  it('sets the real current value at NOW and multiplies the design months so the curve joins it (G14)', () => {
    const g = gpu('h100-test')
    const before = [...g.index]
    applyIndexToGpu(g, H100_INDEX, undefined, NOW)
    expect(g.live?.current).toBe(2.86)
    expect(g.index[26]).toBe(2.86) // the live value, untouched
    const f = 2.86 / before[26]
    expect(g.index[25]).toBeCloseTo(before[25] * f, 2)
    expect(g.index[0]).toBeCloseTo(before[0] * f, 2)
    expect(g.index[38]).toBeCloseTo(before[38] * f, 2) // forecast months scale with the same factor
    expect(g.live?.scaled).toBe(true)
    expect(g.live?.bandFactor).toBeCloseTo(f, 6)
    expect(g.live?.real.filter(Boolean)).toHaveLength(1)
    expect(g.live?.freshness).toBe('live')
    expect(g.live?.freshLabel).toBe('Live (derived)')
    expect(g.live?.designIndex).toEqual(before)
    expect(g.live?.chgPct).toBeCloseTo(((2.86 - 2.8) / 2.8) * 100, 6)
  })
  it('a GPU with NaN months before launch (B200) keeps them NaN and still joins at Now', () => {
    const g = gpu('b200-test')
    g.index = g.index.map((v, i) => (i < 15 ? NaN : v))
    const before = [...g.index]
    applyIndexToGpu(g, { ...H100_INDEX, gpuModelId: 'b200-sxm-192gb', current: 6.99 }, [{ seriesId: 's', t: '2026-11-08T00:39:12Z', value: 6.99, unit: 'USD/GPU-hour', provenance: prov('derived') }], NOW)
    expect(g.index[26]).toBe(6.99)
    expect(Number.isNaN(g.index[3])).toBe(true)
    expect(g.index[20]).toBeCloseTo(before[20] * (6.99 / before[26]), 2)
  })
  it('a fuller history replaces the design in its months, and only the months that stay illustrative are scaled', () => {
    const g = gpu('h100-hist')
    const before = [...g.index]
    const history = [
      { seriesId: 's', t: '2026-09-15T00:00:00Z', value: 3.0, unit: 'USD/GPU-hour', provenance: prov('archived') },
      { seriesId: 's', t: '2026-11-08T00:39:12Z', value: 2.86, unit: 'USD/GPU-hour', provenance: prov('derived') },
    ]
    applyIndexToGpu(g, H100_INDEX, history, NOW)
    expect(g.index[24]).toBe(3.0)
    expect(g.index[26]).toBe(2.86)
    expect(g.index[23]).toBeCloseTo(before[23] * (3.0 / before[24]), 2) // anchored on the first real month
    expect(g.index[30]).toBeCloseTo(before[30] * (2.86 / before[26]), 2) // forecast anchored on Now
    const fA = 3.0 / before[24]
    const fB = 2.86 / before[26]
    expect(g.index[25]).toBeCloseTo(before[25] * (fA + (fB - fA) / 2), 2) // the gap interpolates between the two anchors
  })
})

// GetDeploying weekly medians as the backend archives them: each week stamped at its END (the
// following Monday 00:00 UTC), provenance archived, CC BY 4.0 (shape of the 08 Nov 2026 payload).
const gdhPt = (t: string, value: number) => ({ seriesId: 'gpu:h100-sxm-80gb:getdeploying:weekly-median', t: `${t}T00:00:00Z`, value, unit: 'USD/GPU-hour', provenance: prov('archived', { source: 'getdeploying', observedAt: `${t}T00:00:00Z`, sourceUrl: 'https://getdeploying.com/dataset/gpu-prices', licence: 'CC BY 4.0' }) })
const GDH_WEEKS = [
  gdhPt('2026-09-07', 3.0), gdhPt('2026-09-14', 3.2), gdhPt('2026-09-21', 3.4), gdhPt('2026-09-28', 3.4), // Sep 26 -> slot 24, mean 3.25
  gdhPt('2026-10-05', 4.0), gdhPt('2026-10-12', 4.1), gdhPt('2026-10-19', 3.96), gdhPt('2026-10-26', 3.9), // Oct 26 -> slot 25, mean 3.99
  gdhPt('2026-11-02', 3.85), // the week beginning 26 Oct, stamped at its end: Nov 26 -> slot 26
]

describe('GetDeploying weekly medians as the real past months (N-GDH)', () => {
  it('weeks to months: one mean per slot of the week-end stamp, month to date for the current month', () => {
    expect(monthlyMeans(GDH_WEEKS, NOW)).toEqual([
      { slot: 24, mean: 3.25, weeks: 4 },
      { slot: 25, mean: 3.99, weeks: 4 },
      { slot: 26, mean: 3.85, weeks: 1 },
    ])
    expect(monthlyMeans([gdhPt('2026-12-07', 9), gdhPt('2024-01-01', 9)], NOW)).toEqual([]) // a future week or one before the axis is never drawn
  })
  it('fills the past months and Now from GetDeploying, keeps the FLOPX days as marks, and joins the forecast to the line (continuity at Now)', () => {
    const g = gpu('h100-gdh')
    const before = [...g.index]
    applyIndexToGpu(g, H100_INDEX, undefined, NOW, GDH_WEEKS)
    const l = g.live!
    expect(g.index[24]).toBe(3.25)
    expect(g.index[25]).toBe(3.99)
    expect(g.index[26]).toBe(3.85) // GetDeploying's month to date, not the FLOPX 2.86: one source end to end
    expect(l.current).toBe(2.86) // the FLOPX present is still the headline level
    expect(l.indexMarks!.map((v, i) => (v == null ? null : i)).filter((i) => i != null)).toEqual([26])
    expect(l.indexMarks![26]).toBe(2.86)
    expect(l.real.map((r, i) => (r ? i : -1)).filter((i) => i >= 0)).toEqual([24, 25, 26])
    expect(l.history!.slots.map((r, i) => (r ? i : -1)).filter((i) => i >= 0)).toEqual([24, 25, 26])
    expect(g.index[23]).toBeCloseTo(before[23] * (3.25 / before[24]), 2) // months before the first week take its anchor
    expect(g.index[27]).toBeCloseTo(before[27] * (3.85 / before[26]), 2) // the forecast starts where the line ends
    expect(l.bandFactor).toBeCloseTo(3.85 / before[26], 6)
    expect(l.scaled).toBe(true)
    expect(l.history).toMatchObject({ source: 'getdeploying', seriesId: 'gpu:h100-sxm-80gb:getdeploying:weekly-median', licence: 'CC BY 4.0', attribution: GDH_ATTRIBUTION, sourceUrl: 'https://getdeploying.com/dataset/gpu-prices', weeks: 9, first: '07 Sep 2026', last: '02 Nov 2026' })
    expect(l.history!.monthly).toHaveLength(3)
    expect(l.history!.weekly[8]).toEqual({ t: '2026-11-02T00:00:00Z', value: 3.85 })
    expect(l.historyPoints).toBe(2) // still the FLOPX index's own count (the tour and the gate read it)
  })
  it('the 30-day change falls back to GetDeploying when the FLOPX index is hours old, labelled as such', () => {
    const g = gpu('h100-chg')
    applyIndexToGpu(g, H100_INDEX, undefined, NOW, GDH_WEEKS)
    expect(g.live!.chg30).toEqual({ pct: ((3.85 - 4.0) / 4.0) * 100, source: 'getdeploying', label: '30d · GetDeploying', from: '05 Oct 2026', to: '02 Nov 2026', spanDays: 28 })
    const fig = gpuCardFigures(g)
    expect(fig.delta).toBeCloseTo(-3.75, 6)
    expect(fig.note).toBe(`FLOPX index now · 30d · GetDeploying (05 Oct 2026 to 02 Nov 2026) · spark: ${GDH_BADGE_LABEL}`)
    expect(GDH_BADGE_LABEL).toBe('GetDeploying weekly median · CC BY 4.0 · archived')
    expect(GDH_ATTRIBUTION).toBe('GetDeploying (2026). GPU rental price history, CC BY 4.0')
  })
  it("the FLOPX index's own 30-day change wins once its history reaches back 30 days", () => {
    const g = gpu('h100-own')
    const history = [
      { seriesId: 's', t: '2026-10-01T00:00:00Z', value: 2.5, unit: 'USD/GPU-hour', provenance: prov('archived') },
      { seriesId: 's', t: '2026-11-08T00:39:12Z', value: 2.86, unit: 'USD/GPU-hour', provenance: prov('derived') },
    ]
    applyIndexToGpu(g, H100_INDEX, history, NOW, GDH_WEEKS)
    expect(g.live!.chg30).toMatchObject({ source: 'flopx', label: '30d · FLOPX index', from: '01 Oct 2026', to: '08 Nov 2026', spanDays: 38 })
    expect(g.live!.chg30!.pct).toBeCloseTo(14.4, 6)
    expect(change30(H100_INDEX.values, 'flopx', 'x')).toBeNull() // two points half an hour apart are not a 30-day change
  })
  it('the FLOPX present fills Now only in a month no GetDeploying week has landed in yet', () => {
    const g = gpu('h100-nonov')
    applyIndexToGpu(g, H100_INDEX, undefined, NOW, GDH_WEEKS.slice(0, 8))
    expect(g.index[25]).toBe(3.99)
    expect(g.index[26]).toBe(2.86)
    expect(g.live!.real[26]).toBe(true)
    expect(g.live!.history!.slots[26]).toBe(false)
    expect(g.live!.indexMarks![26]).toBe(2.86)
  })
  it('a model without the series is unchanged: FLOPX at Now, the design scaled around it, no history, no marks, no 30d claim', () => {
    const g = gpu('h100-plain')
    const before = [...g.index]
    applyIndexToGpu(g, H100_INDEX, undefined, NOW, undefined)
    expect(g.index[26]).toBe(2.86)
    expect(g.index[25]).toBeCloseTo(before[25] * (2.86 / before[26]), 2)
    expect(g.live!.history).toBeUndefined()
    expect(g.live!.indexMarks).toBeUndefined()
    expect(g.live!.chg30).toBeNull()
    const fig = gpuCardFigures(g)
    expect(fig.delta).toBe(g.live!.chgPct) // the FLOPX index's own window, never the design's delta
    expect(fig.note).toBe(`FLOPX index now · ${g.live!.chgWindow} · spark: history scaled to today, illustrative`)
    const empty = gpu('h100-empty')
    applyIndexToGpu(empty, H100_INDEX, undefined, NOW, [])
    expect(empty.live!.history).toBeUndefined()
    expect(empty.index).toEqual(g.index)
  })
  it('a GPU the backend never indexed keeps the design card figures behind its illustrative badge', () => {
    const g = gpu('mi300x')
    expect(gpuCardFigures(g)).toEqual({ delta: ((g.index[26] - g.index[25]) / g.index[25]) * 100, note: 'Observed · median on-demand' })
  })
})

const vast: OfferWire = {
  id: 'vast-1', providerId: 'vast-ai', regionId: 'tw', gpuModelId: 'h100-sxm-80gb', gpuCount: 8, pricePerGpuHourUsd: 1.93, availability: 'available', interruptible: false, contract: 'on-demand',
  provenance: prov('live', { note: 'Vast.ai asks: a current market reading, not an index or benchmark.', sourceUrl: 'https://cloud.vast.ai/' }), reliabilityScore: 0.98, bandwidthGbps: 1, minDurationHours: null,
}
const runpod: OfferWire = { ...vast, id: 'runpod-1', providerId: 'runpod', regionId: 'global', availability: 'limited', reliabilityScore: null, provenance: prov('live') }
const sample: OfferWire = { ...vast, id: 'offer-038', providerId: 'helios-nodes', regionId: 'us-or', provenance: prov('sample') }
const rtx: OfferWire = { ...vast, id: 'vast-2', gpuModelId: 'rtx-4090-24gb' }

describe('mapOffer / mapContract', () => {
  it('maps a Vast offer with its market-reading note and no invented fabric, SLA or egress', () => {
    const o = mapOffer(vast)!
    expect(o).toMatchObject({ provider: 'vast-ai', gpu: 'h100', region: 'tw', price: 1.93, contract: 'On-demand', nodeSize: 8, available: 'Available', freshness: 'live', interconnect: 'Not reported', sla: 'n/a', egress: 'n/a', note: 'market reading, not an index', live: true })
  })
  it('drops GPU models the design has no tab for', () => {
    expect(mapOffer(rtx)).toBeNull()
  })
  it('maps contracts', () => {
    expect(mapContract({ contract: 'spot', interruptible: true, minDurationHours: null })).toBe('Spot')
    expect(mapContract({ contract: 'reserved', interruptible: false, minDurationHours: 720 })).toBe('1-mo reserved')
    expect(mapContract({ contract: 'reserved', interruptible: false, minDurationHours: 8760 })).toBe('1-yr reserved')
  })
})

describe('applyOffers', () => {
  it('replaces offers in place, skips backend samples, keeps design offers only for uncovered GPUs, adds providers', () => {
    const designOffer = (id: string, g: string): Offer => ({ id, provider: 'azure', gpu: g, region: 'us-va', price: 3, contract: 'On-demand', interconnect: 'InfiniBand 3.2T', nodeSize: 8, available: 'Available', observed: 'x', freshness: 'illustrative', egress: 'x', sla: 'x' })
    const offers: Offer[] = [designOffer('d1', 'h100'), designOffer('d2', 'mi300x')]
    const ref = offers
    const providers: Provider[] = [{ id: 'runpod', name: 'RunPod', type: 'Marketplace', reliability: 82, note: 'design', freshness: 'illustrative' }]
    const resp: OffersResponse = { offers: [vast, runpod, sample, rtx], freshness: { generatedAt: '2026-11-08T00:39:13Z' } }
    expect(applyOffers(resp, offers, providers)).toBe(2)
    expect(offers).toBe(ref)
    expect(offers.map((o) => o.id)).toEqual(['vast-1', 'runpod-1', 'd2'])
    const v = providers.find((p) => p.id === 'vast-ai')!
    expect(v).toMatchObject({ name: 'Vast.ai', reliability: 98, freshness: 'live' })
    const r = providers.find((p) => p.id === 'runpod')!
    expect(Number.isNaN(r.reliability)).toBe(true) // not reported, never the design's 82
    expect(r.freshness).toBe('live')
    expect(providers.some((p) => p.id === 'helios-nodes')).toBe(false)
  })
  it('leaves everything alone when the backend has no real offers', () => {
    const offers: Offer[] = []
    expect(applyOffers({ offers: [sample], freshness: { generatedAt: 'x' } }, offers, [])).toBe(0)
    expect(offers).toHaveLength(0)
  })
  it('records the payload totals for the tour counters: every non-sample offer, whatever its GPU model (WF4 #4)', () => {
    const resp: OffersResponse = { offers: [vast, runpod, sample, rtx], freshness: { generatedAt: 'x' } }
    expect(offerTotals(resp)).toMatchObject({ count: 3, total: 4, freshness: 'live', providers: new Set([vast, runpod, rtx].map((w) => w.providerId)).size, regions: new Set([vast, runpod, rtx].map((w) => w.regionId)).size, models: new Set([vast, runpod, rtx].map((w) => w.gpuModelId)).size })
    const recorded = offerTotals(offersFixture as unknown as OffersResponse)
    expect(recorded).toEqual({ count: 23, providers: 3, regions: 10, models: 2, total: 25, freshness: 'live' }) // the 8 Nov recording: 25 offers, 2 sample
    applyOffers(offersFixture as unknown as OffersResponse, [], [])
    expect(LIVE_TOTALS.liveOffers).toEqual(recorded)
    expect(OFFER_TOTALS).toBe(LIVE_TOTALS.liveOffers) // the tour reads this live binding
    expect(offerTotals({ offers: [sample], freshness: { generatedAt: 'x' } })).toMatchObject({ count: 0, total: 1, freshness: 'unavailable' })
  })
})

describe('offer region names', () => {
  it('uses backend hub names and country names for live offers', () => {
    setHubNames([{ id: 'us-va', name: 'US East and Midwest (Ashburn hub)' }])
    expect(offerRegionName({ region: 'us-va', live: true })).toBe('US East and Midwest (Ashburn hub)')
    expect(offerRegionName({ region: 'tw', live: true })).toBe('Taiwan')
    expect(offerRegionName({ region: 'global', live: true })).toBe('Location not published')
  })
})

describe('applyPressure', () => {
  it('takes the median of regional cpx scores per GPU and leaves uncovered GPUs illustrative', () => {
    const gs = [gpu('h100'), gpu('mi300x')]
    const resp: PressureResponse = {
      asOf: '2026-11-08T00:47:07Z', method: 'cpx-v1', freshness: { generatedAt: 'x' },
      cells: [
        { regionId: 'ap-jp', gpuModelId: 'h100-sxm-80gb', asOf: 'x', score: 31.86, band: 'balanced' },
        { regionId: 'us-tx', gpuModelId: 'h100-sxm-80gb', asOf: 'x', score: 60.2, band: 'tight' },
        { regionId: 'eu-de', gpuModelId: 'h100-sxm-80gb', asOf: 'x', score: 44.1, band: 'balanced' },
      ],
    }
    expect(applyPressure(resp, gs)).toBe(1)
    expect(gs[0].pressure).toBe(44)
    expect(gs[0].pressureLive?.cells).toBe(3)
    expect(gs[1].pressure).toBe(50)
    expect(gs[1].pressureLive).toBeUndefined()
  })
})
