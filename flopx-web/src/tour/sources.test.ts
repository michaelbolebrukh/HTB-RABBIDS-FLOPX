import { afterEach, describe, expect, it } from 'vitest'
import { __resetSources, familyOf, familySummary, loadSources, mergeFreshness, sourceLabel } from './sources'
import type { FreshnessBlock } from '@/live/types'

afterEach(() => __resetSources())

// Trimmed from the real /flopx/overview and /flopx/gpu/offers freshness blocks, 08 Nov 2026 02:51 UTC.
const overview: FreshnessBlock = {
  generatedAt: '2026-11-08T02:51:54.891165Z',
  sources: ['akash', 'elexon-bmrs', 'energy-charts', 'fcpi-v1:median-of-adjusted-asks', 'flopx-sample-v1', 'hacker-news', 'polymarket-gamma', 'rss:nvidia', 'runpod'],
  sourceDetails: [
    { source: 'akash', kind: 'live', observedAt: '2026-11-08T02:49:11Z', licence: 'Akash console API (terms asserted, not verified)' },
    { source: 'elexon-bmrs', kind: 'live', observedAt: '2026-11-08T02:30:00Z', licence: 'BMRS open data licence' },
    { source: 'energy-charts', kind: 'live', observedAt: '2026-11-08T02:45:00Z', licence: 'CC BY 4.0' },
    { source: 'hacker-news', kind: 'live', observedAt: '2026-11-07T17:01:11Z', licence: 'Public Hacker News content (terms asserted, not verified)' },
    { source: 'polymarket-gamma', kind: 'live', observedAt: '2026-11-08T02:47:27Z', licence: 'Public market data API (terms asserted, not verified)' },
    { source: 'rss:nvidia', kind: 'live', observedAt: '2026-11-06T16:00:00Z', licence: 'Company press-release feeds' },
    { source: 'runpod', kind: 'live', observedAt: '2026-11-08T02:49:12Z', licence: 'Public GraphQL list prices' },
  ],
  unavailable: ['yahoo-finance:http-429'],
}
const offers: FreshnessBlock = {
  generatedAt: '2026-11-08T02:49:53.694330Z',
  sources: ['akash', 'flopx-sample-v1', 'runpod', 'vast-ai'],
  sourceDetails: [
    { source: 'akash', kind: 'live', observedAt: '2026-11-08T02:55:00Z', licence: null },
    { source: 'vast-ai', kind: 'live', observedAt: '2026-11-08T02:49:12Z', licence: 'Vast.ai public search API, shown as a current market reading only' },
  ],
}

describe('familyOf / sourceLabel', () => {
  it('groups backend source ids into the six families', () => {
    expect(familyOf('vast-ai')).toBe('asks')
    expect(familyOf('runpod')).toBe('asks')
    expect(familyOf('fcpi-v1:median-of-adjusted-asks')).toBe('asks')
    expect(familyOf('energy-charts')).toBe('power')
    expect(familyOf('elexon-bmrs')).toBe('power')
    expect(familyOf('sec-edgar')).toBe('filings')
    expect(familyOf('polymarket-gamma')).toBe('markets')
    expect(familyOf('kalshi:KXUTILITYPJMWEST')).toBe('markets')
    expect(familyOf('yahoo-finance')).toBe('markets')
    expect(familyOf('hacker-news')).toBe('news')
    expect(familyOf('rss:coreweave')).toBe('news')
    expect(familyOf('wikidata')).toBe('facilities')
    expect(familyOf('flopx-sample-v1')).toBe('facilities')
  })
  it('labels feeds readably', () => {
    expect(sourceLabel('rss:nvidia')).toBe('Nvidia press feed')
    expect(sourceLabel('vast-ai')).toBe('Vast.ai asks')
    expect(sourceLabel('unknown-thing')).toBe('unknown-thing')
  })
})

describe('mergeFreshness', () => {
  it('merges rows across reads, keeping the newest observation and the licence, and records unavailability', () => {
    const m = mergeFreshness([{ path: '/flopx/overview', f: overview }, { path: '/flopx/gpu/offers', f: offers }, { path: '/flopx/feed', f: undefined }])
    const akash = m.rows.find((r) => r.id === 'akash')!
    expect(akash.observedAt).toBe('2026-11-08T02:55:00Z')
    expect(akash.licence).toContain('Akash')
    expect(akash.seenIn).toEqual(['/flopx/overview', '/flopx/gpu/offers'])
    expect(akash.freshness).toBe('live')
    expect(m.rows.find((r) => r.id === 'vast-ai')!.family).toBe('asks')
    // Named only in `sources`, no detail row: still listed; the sample set reads illustrative.
    const sample = m.rows.find((r) => r.id === 'flopx-sample-v1')!
    expect(sample.freshness).toBe('illustrative')
    expect(m.rows.find((r) => r.id === 'fcpi-v1:median-of-adjusted-asks')!.freshness).toBe('live')
    expect(m.unavailable).toEqual([{ source: 'yahoo-finance:http-429', reason: 'unavailable', seenIn: '/flopx/overview' }])
    expect(m.generatedAt).toBe('2026-11-08T02:51:54.891165Z')
    // Sample rows sort last.
    expect(m.rows[m.rows.length - 1].kind).toBe('sample')
  })
  it('summarises each family with its newest live observation and badge; empty families say unavailable', () => {
    const m = mergeFreshness([{ path: '/flopx/overview', f: overview }])
    const fam = familySummary(m.rows)
    expect(fam.map((f) => f.family)).toEqual(['asks', 'power', 'filings', 'markets', 'news', 'facilities'])
    expect(fam.find((f) => f.family === 'power')!.newest).toBe('2026-11-08T02:45:00Z')
    expect(fam.find((f) => f.family === 'power')!.freshness).toBe('live')
    expect(fam.find((f) => f.family === 'filings')!.freshness).toBe('unavailable')
    // Facilities has only the sample set here: illustrative, not live.
    expect(fam.find((f) => f.family === 'facilities')!.freshness).toBe('illustrative')
  })
})

describe('loadSources', () => {
  it('fetches the reads, merges, and reports failed reads without inventing rows', async () => {
    const calls: string[] = []
    const fetchImpl: typeof fetch = async (input) => {
      const url = String(input)
      calls.push(url)
      if (url.endsWith('/flopx/overview')) return new Response(JSON.stringify({ freshness: overview }), { status: 200, headers: { 'content-type': 'application/json' } })
      if (url.endsWith('/flopx/gpu/offers')) return new Response(JSON.stringify({ freshness: offers }), { status: 200, headers: { 'content-type': 'application/json' } })
      return new Response('nope', { status: 502 })
    }
    const s = await loadSources(fetchImpl)
    expect(s.status).toBe('ready')
    expect(calls.length).toBe(6)
    expect(s.reads.filter((r) => r.ok).length).toBe(2)
    expect(s.rows.some((r) => r.id === 'vast-ai')).toBe(true)
    // Idempotent: a second call returns the same state without refetching.
    await loadSources(fetchImpl)
    expect(calls.length).toBe(6)
  })
  it('is failed, with no rows, when nothing answers', async () => {
    const s = await loadSources(async () => new Response('x', { status: 500 }))
    expect(s.status).toBe('failed')
    expect(s.rows).toEqual([])
  })
})
