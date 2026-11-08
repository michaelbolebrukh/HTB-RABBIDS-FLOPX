// W3 adapters against payloads recorded from https://flopx-backend.onrender.com on 08 Nov 2026
// 00:39 UTC (trimmed: energy zones cut to their last six points, the ERCOT series to every tenth
// day, the feed to two items per live source plus four sample items, the markets to eight
// Polymarket, two Kalshi and two sample contracts).
import { describe, expect, it } from 'vitest'
import { ENERGY } from '@/data/infra'
import { NEWS, PRED } from '@/data/finance'
import {
  adaptEnergy, adaptFeed, adaptMarkets, agoOf, summariseFeed, applyEnergy, applyFeed, applyMarkets, closesOf, energyModule, feedModule, mapRegion, marketsModule, marketTotals, MARKET_TOTALS, newsTypeOf, verificationOf, yesPrice,
  type FeedResponse, type InfraEnergyResponse, type MarketsResponse,
} from './adapt-w3'
import { makeGetter } from './boot'
import { LIVE_TOTALS } from './store'
import type { SeriesPoint } from './types'
import energyJson from './__fixtures__/w3/energy.json'
import ercotJson from './__fixtures__/w3/series-ercot.json'
import feedJson from './__fixtures__/w3/feed.json'
import marketsJson from './__fixtures__/w3/markets.json'

const ENERGY_RESP = energyJson as unknown as InfraEnergyResponse
const ERCOT_SERIES = (ercotJson as unknown as { points: SeriesPoint[] }).points
const FEED = feedJson as unknown as FeedResponse
const MARKETS = marketsJson as unknown as MarketsResponse
const NOW = new Date('2026-11-08T00:40:00Z')
const designIds = new Set(['us-tx', 'us-va', 'us-or', 'ie-dub', 'no-nordics', 'de-fra', 'sg', 'jp-tyo'])
const known = (id: string) => designIds.has(id)
const tickers = new Set(['NVDA', 'CRWV', 'IREN', 'NBIS'])

describe('adaptEnergy', () => {
  const rows = adaptEnergy(ENERGY_RESP, ENERGY)
  const de = rows.find((r) => r.id === 'de')!
  const ercot = rows.find((r) => r.id === 'ercot')!
  it('keeps every design row, in order, so Inspector ids still resolve', () => {
    expect(rows.map((r) => r.id)).toEqual(ENERGY.map((r) => r.id))
  })
  it('DE-LU is the observed zone: latest EUR price, archived provenance -> historical, no invented fields', () => {
    expect(de.priceNow).toBe(223.12)
    expect(de.currency).toBe('EUR')
    expect(de.freshness).toBe('historical')
    expect(Number.isNaN(de.chg30)).toBe(true) // backend reports no 30-day change for DE-LU
    expect(de.scarcityHours).toBeNull()
    expect(de.reserveMargin).toBeNull()
    expect(de.queueGW).toBeNull()
    expect(de.constraint).toContain('energy-charts')
    expect(de.constraint).not.toContain('Frankfurt connection wait')
  })
  it('places DE-LU on the current month slot only; earlier slots are the design curve multiplied to join it (G14)', () => {
    expect(de.real!.filter(Boolean).length).toBe(1)
    expect(de.real![26]).toBe(true)
    expect(de.price[26]).toBe(223.12) // last point of November, untouched
    const design = ENERGY.find((r) => r.id === 'de')!
    const f = 223.12 / design.price[26]
    expect(de.price[25]).toBeCloseTo(design.price[25] * f, 2)
    expect(de.price[0]).toBeCloseTo(design.price[0] * f, 2)
    expect(de.scaled).toBe(true)
    expect(ercot.price[26]).toBe(34.71)
    expect(ercot.scaled).toBe(true)
  })
  it('ERCOT is backend sample data: illustrative, never live, with the backend 30-day change', () => {
    expect(ercot.freshness).toBe('illustrative')
    expect(ercot.priceNow).toBe(34.71)
    expect(ercot.chg30).toBeCloseTo(-8.5375, 4)
  })
  it('a longer series refines the month axis: one value per calendar month, last point wins', () => {
    const withSeries = adaptEnergy(ENERGY_RESP, ENERGY, { 'energy:ERCOT:day-ahead': ERCOT_SERIES }).find((r) => r.id === 'ercot')!
    const months = new Set(ERCOT_SERIES.map((p) => p.t.slice(0, 7)))
    expect(withSeries.real!.filter(Boolean).length).toBe(months.size)
    const lastOct = ERCOT_SERIES.filter((p) => p.t.startsWith('2026-10')).at(-1)!
    expect(withSeries.price[25]).toBe(lastOct.value)
  })
  it('rows the backend lists as missing stay illustrative with their design values', () => {
    const pjm = rows.find((r) => r.id === 'pjm')!
    expect(pjm.freshness).toBe('illustrative')
    expect(pjm).toBe(ENERGY.find((r) => r.id === 'pjm'))
  })
  it('every design market starts illustrative before any data lands', () => {
    expect(ENERGY.every((e) => e.freshness === 'illustrative')).toBe(true)
  })
})

describe('adaptFeed', () => {
  const items = adaptFeed(FEED, { now: NOW, knownRegion: known, knownTicker: (t) => tickers.has(t) })
  it('drops authored sample items: only live sources reach the page', () => {
    expect(FEED.items.some((i) => i.provenance.kind === 'sample')).toBe(true)
    expect(items).toHaveLength(FEED.items.filter((i) => i.provenance.kind !== 'sample').length)
    expect(items.every((n) => n.freshness === 'live')).toBe(true)
  })
  it('maps an SEC filing to Corporate, confirmed by filing, with its EDGAR link', () => {
    const f = items.find((n) => n.source === 'SEC EDGAR filing')!
    expect(f.type).toBe('Corporate')
    expect(f.verification).toBe('Confirmed by filing')
    expect(f.tickers).toEqual(['IREN'])
    expect(f.url).toMatch(/^https:\/\/www\.sec\.gov\//)
  })
  it('Hacker News is News and unverified; company newsrooms are one source', () => {
    const hn = items.find((n) => n.source === 'Hacker News discussion')!
    expect(hn.type).toBe('News')
    expect(hn.verification).toBe('Unverified')
    const nv = items.find((n) => n.source === 'NVIDIA newsroom')!
    expect(nv.verification).toBe('Single source')
    expect(nv.tickers).toEqual(['NVDA'])
  })
  it('drops tickers the design cannot open (CORZ is not a tracked security)', () => {
    const corz = items.find((n) => n.source === 'Core Scientific newsroom')!
    expect(corz.tickers).toEqual([])
  })
  it('newest first, with real publication times and whole-day ages', () => {
    expect(items[0].title).toBe(FEED.items[0].headline)
    expect(items[0].time).toBe('07 Nov 2026 17:01 UTC')
    expect(items[0].ago).toBe('0d')
    const ages = items.map((n) => parseInt(n.ago, 10))
    expect([...ages].sort((a, b) => a - b)).toEqual(ages)
  })
  it('summariseFeed counts the shown headlines, their day span, sources and most-mentioned tickers', () => {
    const sum = summariseFeed(items, FEED)
    expect(sum.live).toBe(true)
    expect(sum.headlines).toBe(items.length)
    expect(sum.days).toBe(Math.max(...items.map((n) => parseInt(n.ago, 10))) + 1)
    expect(sum.sources).toContain('SEC EDGAR filing')
    expect(sum.themes).toEqual(['CRWV', 'IREN', 'NVDA']) // two mentions each in the trimmed feed; ties break alphabetically
    expect(sum.asOf).toBe('08 Nov 2026 00:39 UTC')
  })
  it('applyFeed replaces the design items in place and ignores an empty list', () => {
    const ref = NEWS
    applyFeed([])
    expect(NEWS.length).toBeGreaterThan(0)
    applyFeed(items)
    expect(NEWS).toBe(ref)
    expect(NEWS.map((n) => n.id)).toEqual(items.map((n) => n.id))
  })
})

describe('feed helpers', () => {
  it('mapRegion prefers an id the region list knows, then the design mapping, else the backend id', () => {
    expect(mapRegion('eu-ie', known)).toBe('ie-dub')
    expect(mapRegion('us-tx', known)).toBe('us-tx')
    expect(mapRegion('eu-ie', (id) => id === 'eu-ie')).toBe('eu-ie')
    expect(mapRegion('zz-unknown', known)).toBe('zz-unknown')
  })
  it('agoOf never goes negative and counts whole days', () => {
    expect(agoOf('2026-11-09T00:00:00Z', NOW)).toBe('0d')
    expect(agoOf('2026-10-27T20:05:00Z', NOW)).toBe('11d')
  })
  it('newsTypeOf and verificationOf follow the source', () => {
    const base = FEED.items[0]
    expect(newsTypeOf({ ...base, kind: 'policy' })).toBe('Regulatory')
    expect(verificationOf({ ...base, classification: 'verified', provenance: { ...base.provenance, source: 'rss:x' } })).toBe('Single source')
  })
})

describe('adaptMarkets', () => {
  const pms = adaptMarkets(MARKETS, { now: NOW, knownRegion: known, knownTicker: (t) => tickers.has(t) })
  it('shows four live cards, never a sample contract or a stale Kalshi quote', () => {
    expect(pms).toHaveLength(4)
    expect(pms.every((p) => p.freshness === 'live')).toBe(true)
    expect(pms.some((p) => p.venue === 'sample-prediction-venue')).toBe(false)
    expect(pms.some((p) => p.venue === 'Kalshi')).toBe(false) // last Kalshi update was 19 Aug
  })
  it('one card per question family: one B200 ladder does not fill every slot', () => {
    const b200 = pms.filter((p) => p.question.includes('B200'))
    expect(b200).toHaveLength(1)
    expect(b200[0].question).toBe('Will the Ornn B200 Index be at least $10.00 on December 31, 2026?') // highest 24h volume
  })
  it('YES price, 24h volume, depth and the close date read from the payload; no spread or history invented', () => {
    const p = pms[0]
    expect(p.prob).toBe(0.562)
    expect(p.volume).toBeCloseTo(3078.818442, 4)
    expect(p.liquidity).toBeCloseTo(1700.11329, 4)
    expect(p.closes).toBe('31 Dec 2026')
    expect(p.spread).toBeNull()
    expect(p.probHist).toEqual([])
    expect(p.chg7).toBeNull()
    expect(p.caution).toContain('$1.7k depth')
  })
  it('Kalshi YES labels carry the strike; yesPrice still finds them', () => {
    const k = MARKETS.markets.find((m) => m.venue === 'kalshi')!
    expect(yesPrice(k)).toBe(0.19)
  })
  it('closesOf reads only what the question states', () => {
    expect(closesOf('Will X be above 5 on November 30, 2026?')).toBe('30 Nov 2026')
    expect(closesOf('Will load on the ERCOT grid during 2026 get above 93,000 MW?')).toBe('31 Dec 2026')
    expect(closesOf('Texas announces a moratorium?')).toBeNull()
  })
  it('a market with history gets a real 7-day change', () => {
    const withHist = adaptMarkets(
      { ...MARKETS, markets: MARKETS.markets.filter((m) => m.venue === 'sample-prediction-venue').map((m) => ({ ...m, venue: 'polymarket', provenance: { ...m.provenance, kind: 'live' as const }, lastUpdated: '2026-11-06T21:00:00Z' })) },
      { now: new Date('2026-11-06T22:00:00Z') },
    )
    const p = withHist[0]
    const src = MARKETS.markets.find((m) => `pm:${m.id}` === p.id)!
    const weekAgo = src.history.filter((h) => Date.parse(h.t) <= Date.parse('2026-10-30T22:00:00Z')).at(-1)!
    expect(p.chg7).toBe(Math.round((p.prob - weekAgo.value) * 100))
    expect(p.probHist).toHaveLength(src.history.length)
  })
  it('applyMarkets replaces the design cards in place', () => {
    const ref = PRED
    applyMarkets(pms)
    expect(PRED).toBe(ref)
    expect(PRED[0].id).toBe(pms[0].id)
  })
})

describe('boot modules', () => {
  const fixtures: Record<string, unknown> = {
    '/flopx/infra/energy': ENERGY_RESP,
    '/flopx/feed': FEED,
    '/flopx/markets': MARKETS,
    [`/flopx/gpu/series?id=${encodeURIComponent('energy:ERCOT:day-ahead')}`]: { points: ERCOT_SERIES },
  }
  const fakeFetch = (async (url: string) => {
    const path = url.replace('https://test', '')
    const body = fixtures[path]
    return body ? new Response(JSON.stringify(body), { status: 200 }) : new Response('{}', { status: 502 })
  }) as unknown as typeof fetch
  it('energy, feed and markets modules load through the shared getter and write the design exports', async () => {
    const get = makeGetter({ fetchImpl: fakeFetch, base: 'https://test' })
    let applied = 0
    await energyModule.load(get, () => { applied++ })
    expect(applied).toBe(1)
    expect(ENERGY.find((e) => e.id === 'de')!.freshness).toBe('historical')
    expect(ENERGY.find((e) => e.id === 'ercot')!.real!.filter(Boolean).length).toBeGreaterThan(1)
    expect(await feedModule.load(get, () => {})).toBe(true)
    expect(NEWS.every((n) => n.freshness === 'live')).toBe(true)
    expect(await marketsModule.load(get, () => {})).toBe(true)
    expect(PRED.every((p) => p.freshness === 'live')).toBe(true)
    // The tour's counters read the payload totals, not the four design cards (WF4 #4).
    expect(marketTotals(MARKETS)).toEqual({ live: 10, count: 10, venues: 2, venueNames: ['kalshi', 'polymarket'], total: 12, freshness: 'live' })
    expect(LIVE_TOTALS.marketTotals).toEqual(marketTotals(MARKETS))
    expect(MARKET_TOTALS).toBe(LIVE_TOTALS.marketTotals) // the tour reads this live binding
    expect(PRED.length).toBeLessThan(LIVE_TOTALS.marketTotals!.live)
  })
  it('a failed read throws, so the boot loader marks the domain failed and the design stays', async () => {
    const get = makeGetter({ fetchImpl: (async () => new Response('', { status: 404 })) as unknown as typeof fetch, base: 'https://test' })
    await expect(feedModule.load(get, () => {})).rejects.toThrow('HTTP 404')
  })
})

describe('apply energy keeps the array identity', () => {
  it('writes rows into ENERGY in place', () => {
    const ref = ENERGY
    applyEnergy(adaptEnergy(ENERGY_RESP, ENERGY))
    expect(ENERGY).toBe(ref)
  })
})
