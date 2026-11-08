// WF4 #4, #5, #7, #8: the retrieval counters come from the payload totals, the estimation badge
// from the evaluation's price basis, the factor card's "inputs real" from 1 - sampleShare, and the
// pressure mean's badge from the cells, never a hardcoded live. Fixtures are the recorded payloads
// under src/live/__fixtures__.
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { REGIONS, type Region } from '@/data/core'
import { W2_LIVE } from '@/live/adapt-w2'
import { evalPriceBasis } from '@/live/evalBasis'
import type { MarketsResponse } from '@/live/adapt-w3'
import type { OffersResponse } from '@/live/types'
import marketsJson from '@/live/__fixtures__/w3/markets.json'
import offersJson from '@/live/__fixtures__/w5/offers-h100-h200.json'
import evaluationJson from '@/live/__fixtures__/w2/evaluation.json'
import { evaluationFacts, factorsOf, pressureMeanFreshness, retrievalCounters } from './data'
import { __resetSources, loadSources, marketTotals, offerTotals } from './sources'

const OFFERS = offersJson as unknown as OffersResponse
const MARKETS = marketsJson as unknown as MarketsResponse

describe('payload totals (WF4 #4)', () => {
  it('counts live offers, providers, regions and models over the raw payload, leaving sample rows out', () => {
    const t = offerTotals(OFFERS.offers)!
    const live = OFFERS.offers.filter((o) => o.provenance.kind !== 'sample')
    expect(t.count).toBe(live.length)
    expect(t.count).toBe(23)
    expect(t.total).toBe(25)
    expect(t.providers).toBe(3)
    expect(t.regions).toBe(10)
    expect(t.models).toBe(2)
    expect(t.freshness).toBe('live')
  })
  it('counts live markets and their venues the same way', () => {
    const t = marketTotals(MARKETS.markets)!
    expect(t.count).toBe(10)
    expect(t.total).toBe(12)
    expect(t.venues).toBe(2)
    expect(t.venueNames).toEqual(['kalshi', 'polymarket'])
    expect(t.freshness).toBe('live')
  })
  it('an all-sample payload reads unavailable, never live', () => {
    const t = offerTotals(OFFERS.offers.filter((o) => o.provenance.kind === 'sample'))!
    expect(t.count).toBe(0)
    expect(t.freshness).toBe('unavailable')
    expect(marketTotals(undefined)).toBeNull()
  })
})

describe('retrievalCounters reads the payload totals the tour fetched', () => {
  beforeEach(() => __resetSources())
  afterEach(() => __resetSources())
  const fetchImpl = (async (input: RequestInfo | URL) => {
    const u = String(input)
    const body = u.includes('/flopx/gpu/offers') ? OFFERS : u.includes('/flopx/markets') ? MARKETS : { freshness: { generatedAt: '2026-11-08T04:00:00Z' } }
    return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })
  }) as unknown as typeof fetch
  it('shows "Counting" before the payload lands and the payload counts after, with the venue names', async () => {
    const before = retrievalCounters()
    expect(before.find((f) => f.label === 'GPU asks observed')!.freshLabel).toBe('Counting')
    expect(before.find((f) => f.label === 'Prediction markets')!.value).toBe('…')
    await loadSources(fetchImpl)
    const after = retrievalCounters()
    const asks = after.find((f) => f.label === 'GPU asks observed')!
    expect(asks.num).toBe(23)
    expect(asks.note).toContain('3 providers · 10 regions · 2 GPU models')
    expect(asks.note).toContain('/flopx/gpu/offers')
    const pm = after.find((f) => f.label === 'Prediction markets')!
    expect(pm.num).toBe(10)
    expect(pm.note).toContain('2 venues (Kalshi, Polymarket)')
    // Never the design's 65 / 20 / 5 or 4 / 1.
    expect(asks.value).not.toBe('65')
    expect(pm.value).not.toBe('4')
  })
})

describe('estimation badge follows the price basis (WF4 #5)', () => {
  const keep = W2_LIVE.evaluation
  afterEach(() => { W2_LIVE.evaluation = keep })
  const base = { generatedAt: '2026-11-08T04:00:00Z', fresh: { freshness: 'illustrative' as const }, tickers: ['NVDA', 'CRWV'], reliabilityNote: 'n', augmented: { '1W': {}, '1M': { worsens: 2 }, '3M': {} } as never, combinedScored: { '1W': 0, '1M': 0, '3M': 0 } }
  it('says sample closes when the payload names only the sample set', () => {
    W2_LIVE.evaluation = { ...base, priceBasis: evalPriceBasis((evaluationJson as { freshness: { sources: string[] } }).freshness) }
    const ev = evaluationFacts()!
    expect(ev.basis.kind).toBe('sample')
    expect(ev.basis.label).toBe('Illustrative · sample closes')
    expect(ev.basis.freshness).toBe('illustrative')
  })
  it('says archived alpaca-data closes through the date when the payload does (what the backend served on 8 Nov)', () => {
    const f = { sources: ['alpaca-data', 'flopx-sample-v1'], sourceDetails: [{ source: 'alpaca-data', kind: 'archived', observedAt: '2026-11-06T21:00:00Z' }, { source: 'flopx-sample-v1', kind: 'sample', observedAt: null as unknown as string }] }
    W2_LIVE.evaluation = { ...base, priceBasis: evalPriceBasis(f) }
    const ev = evaluationFacts()!
    expect(ev.basis.kind).toBe('archived')
    expect(ev.basis.label).toBe('Archived · alpaca-data closes through 2026-11-06')
    expect(ev.basis.freshness).toBe('historical')
    expect(ev.basis.caption).toContain('alpaca-data closes through 2026-11-06')
  })
  it('says WRDS CRSP when the payload names wrds-crsp', () => {
    W2_LIVE.evaluation = { ...base, priceBasis: evalPriceBasis({ sources: ['wrds-crsp'], sourceDetails: [{ source: 'wrds-crsp', kind: 'archived', observedAt: '2025-12-31T00:00:00Z' }] }) }
    expect(evaluationFacts()!.basis.label).toBe('Archived · WRDS CRSP closes through 2025-12-31')
  })
})

const cell = (over: Partial<NonNullable<Region['live']>['pressure'] & object>): Region => ({
  ...REGIONS[0],
  live: { backendId: 'x', asOf: '2026-11-08T03:00:00Z', rental: null, power: null, supply: null, exposure: null, pressure: { gpu: 'h100', score: 48, band: 'balanced', label: 'exploratory', coverage: 1, factorsWithData: 5, factorsTotal: 6, sampleShare: 1, freshness: 'live', realSlots: [], scaled: false, factors: [], ...over } },
})

describe('inputs real and the pressure mean badge (WF4 #7, #8)', () => {
  it('"inputs real" is 1 - sampleShare, and factors with data are stated separately', () => {
    const f = factorsOf(cell({ coverage: 1, sampleShare: 1, factorsWithData: 5, factorsTotal: 6 }))!
    expect(f.realShare).toBe(0)
    expect(f.factorsWithData).toBe(5)
    expect(f.factorsTotal).toBe(6)
    const g = factorsOf(cell({ coverage: 0.5, sampleShare: 0.25, factorsWithData: 3, factorsTotal: 6 }))!
    expect(g.realShare).toBe(0.75)
  })
  it('a mean over cells that are mostly sample-fed is illustrative, however the cells are badged', () => {
    const f = pressureMeanFreshness([cell({ sampleShare: 1, freshness: 'live' }), cell({ sampleShare: 0.9, freshness: 'live' })])
    expect(f.fresh).toBe('illustrative')
    expect(f.sampleFed).toBe(2)
  })
  it('a mean over live cells with real inputs is Live (derived); archived cells read historical', () => {
    expect(pressureMeanFreshness([cell({ sampleShare: 0.1, freshness: 'live' })])).toMatchObject({ fresh: 'live', label: 'Live (derived)', sampleFed: 0 })
    expect(pressureMeanFreshness([cell({ sampleShare: 0.1, freshness: 'historical' }), cell({ sampleShare: 0, freshness: 'live' })]).fresh).toBe('historical')
    expect(pressureMeanFreshness([]).fresh).toBe('unavailable')
  })
})
