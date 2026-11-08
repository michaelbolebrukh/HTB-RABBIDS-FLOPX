// W4 adapter against POST /flopx/ask payloads recorded from https://flopx-backend.onrender.com on
// 08 Nov 2026 02:11 to 02:20 UTC (trimmed: the brief's freshness.sourceDetails dropped; the adapter
// reads nothing from it). One recorded payload per prepared Agent Desk question, three route-only
// intents and one question the grammar matched nothing in.
import { describe, expect, it } from 'vitest'
import { AGENT_SUGGESTED } from '@/lib/ask'
import {
  adaptAsk, briefFacts, briefQueryFor, confidenceOf, freshOfRef, mapRoute, mapRoutes, mergeTargetBrief, nameRegions, targetOf, toolOf, truthOf,
  type AskResponse,
} from './adapt-w4'
import texas from './__fixtures__/w4/ask-texas.json'
import h100 from './__fixtures__/w4/ask-h100.json'
import crwv from './__fixtures__/w4/ask-crwv.json'
import nvda from './__fixtures__/w4/ask-nvda.json'
import virginia from './__fixtures__/w4/ask-virginia.json'
import h200 from './__fixtures__/w4/ask-h200.json'
import nebius from './__fixtures__/w4/ask-nebius.json'
import frankfurt from './__fixtures__/w4/ask-frankfurt.json'
import gpuRoute from './__fixtures__/w4/ask-gpu-route.json'
import procureRoute from './__fixtures__/w4/ask-procure-route.json'
import feedRoute from './__fixtures__/w4/ask-feed-route.json'
import noMatch from './__fixtures__/w4/ask-overview-nomatch.json'

const as = (j: unknown) => j as AskResponse
const TEXAS = as(texas)
const H100 = as(h100)
const CRWV = as(crwv)
const NVDA = as(nvda)

/** The names the backend's /flopx/map/regions gives these hubs (what W1's regions module loads). */
const HUBS: Record<string, string> = { 'us-tx': 'US Central (Dallas hub)', 'us-va': 'US East and Midwest (Ashburn hub)', 'eu-de': 'Western Europe (Frankfurt hub)', 'ap-jp': 'Japan (Tokyo hub)' }
const namer = (id: string) => HUBS[id] ?? id
const shortNamer = (id: string) => HUBS[id]?.match(/\(([^)]+?) hub\)/)?.[1] ?? id.toUpperCase()
const opts = { namer, shortNamer }

describe('provenance to freshness and truth', () => {
  it('a live envelope is live, archived is historical, sample is illustrative', () => {
    expect(freshOfRef({ provenance: { kind: 'live', source: 'kalshi', observedAt: null } }).freshness).toBe('live')
    expect(freshOfRef({ provenanceKind: 'archived' }).freshness).toBe('historical')
    expect(freshOfRef({ provenance: { kind: 'sample', source: 'flopx-sample-v1', observedAt: null } }).freshness).toBe('illustrative')
  })
  it('a derived envelope follows its inputs: all live -> live (derived); any sample -> illustrative; archived -> historical', () => {
    expect(freshOfRef({ provenance: { kind: 'derived', source: 'fcpi-v1', observedAt: null, inputKinds: ['live'] } })).toEqual({ freshness: 'live', label: 'Live (derived)' })
    expect(freshOfRef({ provenance: { kind: 'derived', source: 'cpx-v1', observedAt: null, inputKinds: ['derived', 'live', 'sample'] } }).freshness).toBe('illustrative')
    expect(freshOfRef({ provenanceKind: 'derived', inputKinds: ['archived', 'live'] }).freshness).toBe('historical')
  })
  it('an evaluation verdict has no envelope: its input kinds decide; nothing stated is illustrative', () => {
    expect(freshOfRef({ provenance: null, provenanceKind: null, inputKinds: ['archived'] }).freshness).toBe('historical')
    expect(freshOfRef({ provenance: null, provenanceKind: null, inputKinds: ['archived', 'sample'] }).freshness).toBe('illustrative')
    expect(freshOfRef({ provenance: null, provenanceKind: null, inputKinds: [] }).freshness).toBe('illustrative')
  })
  it('statement class -> glyph, and a sample-backed observation never reads as observed', () => {
    expect(truthOf('observation', 'live')).toBe('obs')
    expect(truthOf('observation', 'historical')).toBe('obs')
    expect(truthOf('observation', 'illustrative')).toBeNull()
    expect(truthOf('inference', 'illustrative')).toBe('inf')
    expect(truthOf('forecast', 'live')).toBe('fct')
    expect(truthOf('decision', 'live')).toBe('dec')
    expect(truthOf('something-else', 'live')).toBeNull()
  })
})

describe('briefFacts (Texas region brief)', () => {
  const facts = briefFacts(TEXAS.brief!, namer)
  it('one fact per cited sentence, in the backend order', () => {
    expect(facts).toHaveLength(TEXAS.brief!.sentences.length)
    expect(facts[0].text).toContain('ERCOT day-ahead power was last observed at 34.71 USD/MWh')
  })
  it('the sample-backed power observation carries no OBS glyph and is illustrative', () => {
    expect(facts[0].truth).toBeNull()
    expect(facts[0].freshness).toBe('illustrative')
    expect(facts[0].source).toBe('flopx-sample-v1 · series')
    expect(facts[0].time).toBe('06 Nov 2026 21:00 UTC')
  })
  it('the pressure inference is INF, illustrative because its inputs include sample, and names the hub', () => {
    const p = facts.find((f) => f.evidenceId === 'pressure:us-tx:rtx-4090-24gb')!
    expect(p.truth).toBe('inf')
    expect(p.freshness).toBe('illustrative')
    expect(p.text).toContain('in US Central (Dallas hub) is 44 of 100')
    expect(p.text).not.toMatch(/\bus-tx\b/)
    expect(p.source).toBe('cpx-v1 · pressure-cell')
  })
  it('the Kalshi quotes are live observations: OBS and live', () => {
    const k = facts.filter((f) => f.evidenceId?.startsWith('kalshi:'))
    expect(k).toHaveLength(2)
    for (const f of k) {
      expect(f.truth).toBe('obs')
      expect(f.freshness).toBe('live')
      expect(f.source).toBe('kalshi · market')
      expect(f.time).toBe('19 Aug 2026 19:30 UTC')
    }
  })
  it('authored sample feed items are illustrative with no OBS glyph', () => {
    const feed = facts.filter((f) => f.evidenceId?.startsWith('intel-'))
    expect(feed).toHaveLength(3)
    for (const f of feed) {
      expect(f.freshness).toBe('illustrative')
      expect(f.truth).toBeNull()
    }
  })
  it('no fact row is ever live or OBS when its evidence is sample (whole fixture set)', () => {
    for (const r of [TEXAS, H100, CRWV, NVDA, as(virginia), as(h200), as(nebius), as(frankfurt)]) {
      const refs = new Map(r.brief!.evidence.map((e) => [e.id, e]))
      for (const f of briefFacts(r.brief!, namer)) {
        const ref = refs.get(f.evidenceId!)!
        const kinds = [ref.provenance?.kind, ...(ref.provenance?.inputKinds ?? []), ...(ref.inputKinds ?? [])]
        if (kinds.includes('sample')) {
          expect(f.freshness, f.evidenceId).toBe('illustrative')
          expect(f.truth, f.evidenceId).not.toBe('obs')
        }
        if (f.freshness === 'live') expect(kinds.every((k) => k === 'live' || k === 'derived'), f.evidenceId).toBe(true)
      }
    }
  })
})

describe('H100 and CRWV briefs', () => {
  it('the FCPI index observation is derived from live asks: OBS, Live (derived)', () => {
    const f = briefFacts(H100.brief!, namer)[0]
    expect(f.evidenceId).toBe('index:fcpi:h100-sxm-80gb')
    expect(f.truth).toBe('obs')
    expect(f.freshness).toBe('live')
    expect(f.freshLabel).toBe('Live (derived)')
    expect(f.source).toBe('fcpi-v1:median-of-adjusted-asks · series')
  })
  it('CoreWeave RSS items are live OBS; the sample close and sample markets are illustrative; evaluation verdicts are INF illustrative (archived + sample closes)', () => {
    const facts = briefFacts(CRWV.brief!, namer)
    expect(facts[0].evidenceId).toBe('equity:CRWV:close')
    expect(facts[0].freshness).toBe('illustrative')
    expect(facts[0].truth).toBeNull()
    const rss = facts.filter((f) => f.evidenceId?.startsWith('rss:coreweave:'))
    expect(rss).toHaveLength(3)
    for (const f of rss) { expect(f.truth).toBe('obs'); expect(f.freshness).toBe('live') }
    const ev = facts.filter((f) => f.evidenceId?.startsWith('evaluation:'))
    expect(ev).toHaveLength(2)
    for (const f of ev) { expect(f.truth).toBe('inf'); expect(f.freshness).toBe('illustrative') }
  })
  it('NVDA evaluation verdicts rest on archived closes only: INF, historical', () => {
    const ev = briefFacts(NVDA.brief!, namer).filter((f) => f.evidenceId?.startsWith('evaluation:'))
    expect(ev).toHaveLength(2)
    for (const f of ev) { expect(f.truth).toBe('inf'); expect(f.freshness).toBe('historical') }
  })
})

describe('confidence', () => {
  it('no facts -> Insufficient; any sample row -> Low; otherwise Moderate; never High', () => {
    expect(confidenceOf([])).toBe('Insufficient')
    expect(adaptAsk(TEXAS, opts).confidence).toBe('Low')
    const allLive = briefFacts(CRWV.brief!, namer).filter((f) => f.freshness === 'live')
    expect(allLive.length).toBeGreaterThan(0)
    expect(confidenceOf(allLive)).toBe('Moderate')
  })
})

describe('routes to design destinations', () => {
  it('X-Ray cells map to the design region and GPU ids with a state patch', () => {
    expect(mapRoute('/xray?r=us-tx&g=a100-sxm-80gb', shortNamer)).toEqual({ label: 'Open Dallas A100 80GB pressure in X-Ray', page: 'xray', xray: { region: 'us-tx', gpu: 'a100' } })
    expect(mapRoute('/xray?r=eu-de&g=h100-sxm-80gb', shortNamer)).toEqual({ label: 'Open Frankfurt H100 SXM pressure in X-Ray', page: 'xray', xray: { region: 'de-fra', gpu: 'h100' } })
    expect(mapRoute('/xray?r=us-tx&tab=supply', shortNamer)).toEqual({ label: 'Open Dallas supply in X-Ray', page: 'xray', xray: { region: 'us-tx', tab: 'pipeline' } })
    // the design has no RTX 4090: the region opens, the GPU selection is left alone
    expect(mapRoute('/xray?r=us-tx&g=rtx-4090-24gb', shortNamer)!.xray).toEqual({ region: 'us-tx' })
  })
  it('anomaly, energy, research, feed, markets, companies, gpu, procure, desk, map and root routes', () => {
    expect(mapRoute('/xray/anomalies/anomaly:overhang:eu-de:h100-sxm-80gb', shortNamer)).toEqual({ label: 'Open Frankfurt anomaly in X-Ray', page: 'xray', xray: { region: 'de-fra', gpu: 'h100' } })
    expect(mapRoute('/infra?r=us-tx', shortNamer)).toEqual({ label: 'Open ERCOT power in Energy', page: 'energy', params: { market: 'ercot' } })
    expect(mapRoute('/infra', shortNamer)).toEqual({ label: 'Open Energy & Infra', page: 'energy', params: undefined })
    expect(mapRoute('/research?tab=lab&series=index%3Afcpi%3Ah100-sxm-80gb', shortNamer)).toEqual({ label: 'Open Research & Eval', page: 'research' })
    expect(mapRoute('/research?tab=evaluation&target=CRWV&horizon=1d', shortNamer)!.label).toBe('Open CRWV evaluation in Research')
    expect(mapRoute('/feed/intel-040', shortNamer)).toEqual({ label: 'Open News & Prediction', page: 'news' })
    expect(mapRoute('/markets/mkt-001', shortNamer)).toEqual({ label: 'Open prediction markets', page: 'news' })
    expect(mapRoute('/companies/CRWV', shortNamer)).toEqual({ label: 'Research CRWV', page: 'securities', params: { t: 'CRWV' } })
    expect(mapRoute('/gpu?g=h100-sxm-80gb&r=us-tx&range=1d&view=offers&sort=adjusted', shortNamer)).toEqual({ label: 'Compare offers', page: 'markets', params: { gpu: 'h100' } })
    expect(mapRoute('/gpu/h100-sxm-80gb', shortNamer)).toEqual({ label: 'Open GPU Markets', page: 'markets', params: { gpu: 'h100' } })
    expect(mapRoute('/procure?g=h200-sxm-141gb&gpus=64&gpuHours=64', shortNamer)).toEqual({ label: 'Open Procurement Desk', page: 'procurement', params: { gpu: 'h200' } })
    expect(mapRoute('/desk', shortNamer)).toEqual({ label: 'Open Trading Desk', page: 'trading' })
    expect(mapRoute('/map?r=us-tx', shortNamer)).toEqual({ label: 'Open Dallas in X-Ray', page: 'xray', xray: { region: 'us-tx' } })
    expect(mapRoute('/', shortNamer)).toEqual({ label: 'Open Global Intelligence', page: 'overview' })
    expect(mapRoute('/ask?q=What+is+happening', shortNamer)).toBeNull()
  })
  it('mapRoutes de-duplicates by destination and caps the list', () => {
    const d = mapRoutes([...TEXAS.brief!.followThrough.map((f) => f.route), TEXAS.route], shortNamer)
    expect(d.length).toBeLessThanOrEqual(4)
    expect(new Set(d.map((x) => JSON.stringify([x.page, x.params, x.xray]))).size).toBe(d.length)
    expect(d[0]).toEqual({ label: 'Open ERCOT power in Energy', page: 'energy', params: { market: 'ercot' } })
  })
})

describe('adaptAsk', () => {
  it('a brief answer: facts, composed interpretation naming the sample share and the method, Low confidence, design destinations', () => {
    const a = adaptAsk(TEXAS, opts)
    expect(a.kind).toBe('brief')
    expect(a.facts).toHaveLength(11)
    expect(a.headline).toBe('US Central (Dallas hub): inferred pressure 44/100, 1 open anomalies, 3 recent items')
    expect(a.interpretation).toContain('9 of 11 cited rows rest on the backend\'s authored sample set')
    expect(a.interpretation).toContain('composed-from-evidence-v1')
    expect(a.interpretation).toContain('no language model')
    expect(a.interpretation).not.toMatch(/\bus-tx\b/)
    expect(a.confidence).toBe('Low')
    expect(a.engine).toBe('composed brief · composed-from-evidence-v1')
    expect(a.tool).toBeUndefined()
    expect(a.destinations.map((d) => d.page)).toEqual(['energy', 'xray', 'xray', 'xray'])
  })
  it('a route-only gpu answer: no facts, Insufficient, the target it named, the matching design tool', () => {
    const a = adaptAsk(as(gpuRoute), opts)
    expect(a.kind).toBe('route')
    expect(a.facts).toEqual([])
    expect(a.confidence).toBe('Insufficient')
    // the grammar named both a region and a GPU: the region wins, as in the grammar's own target order
    expect(a.target).toEqual({ kind: 'region', id: 'us-tx' })
    expect(a.tool).toBe('cheapest_compute')
    expect(a.interpretation).toContain('GPU market question')
    expect(a.interpretation).toContain('region texas')
    expect(a.destinations).toEqual([{ label: 'Compare offers', page: 'markets', params: { gpu: 'h100' } }])
  })
  it('procure and feed intents map onto the design tools plan_job and news; the no-match answer has none', () => {
    expect(adaptAsk(as(procureRoute), opts).tool).toBe('plan_job')
    expect(adaptAsk(as(procureRoute), opts).destinations[0]).toEqual({ label: 'Open Procurement Desk', page: 'procurement', params: { gpu: 'h200' } })
    expect(toolOf(as(feedRoute))).toBe('news')
    const none = adaptAsk(as(noMatch), opts)
    expect(none.kind).toBe('none')
    expect(none.tool).toBeUndefined()
    expect(none.target).toBeUndefined()
    expect(none.confidence).toBe('Insufficient')
    expect(none.interpretation).toContain('matched none of these words')
    expect(none.destinations).toEqual([{ label: 'Open Global Intelligence', page: 'overview' }])
  })
  it('targetOf prefers ticker, then a single region, then GPU, like the grammar', () => {
    expect(targetOf({ ...as(gpuRoute), filters: { t: 'CRWV', r: 'us-tx', g: 'h100-sxm-80gb' } })).toEqual({ kind: 'ticker', id: 'CRWV' })
    expect(targetOf({ ...as(gpuRoute), filters: { r: 'us-tx,us-va', g: 'h100-sxm-80gb' } })).toEqual({ kind: 'gpu', id: 'h100-sxm-80gb' })
    expect(briefQueryFor({ kind: 'region', id: 'us-tx' })).toBe('tell me about us-tx')
  })
  it('mergeTargetBrief gives a route-only answer the target brief\'s facts and keeps the first screen as the primary action', () => {
    const first = adaptAsk(as(gpuRoute), opts)
    const merged = mergeTargetBrief(first, H100, opts)
    expect(merged.kind).toBe('brief')
    expect(merged.facts).toHaveLength(7)
    expect(merged.destinations[0]).toEqual({ label: 'Compare offers', page: 'markets', params: { gpu: 'h100' } })
    expect(merged.destinations.length).toBeLessThanOrEqual(5)
    expect(merged.confidence).toBe('Low')
    expect(merged.engine).toBe('grammar · intent gpu + composed brief')
    expect(merged.interpretation).toContain('FLOPX read this as a GPU market question')
    expect(merged.interpretation).not.toContain('carries no fact rows')
    expect(mergeTargetBrief(first, as(gpuRoute), opts)).toBe(first)
  })
})

describe('the prepared Agent Desk questions', () => {
  const recorded: Record<string, AskResponse> = {
    'What is happening in Texas?': TEXAS,
    'Tell me about the H100': H100,
    'How is CoreWeave doing?': CRWV,
    'How is Nvidia doing?': NVDA,
    'What is the state of Virginia?': as(virginia),
    'Tell me about the H200': as(h200),
    'How is Nebius doing?': as(nebius),
    'What is happening in Frankfurt?': as(frankfurt),
  }
  it('every prepared question has a recorded backend payload whose query is that question and which composed a brief with facts', () => {
    expect(AGENT_SUGGESTED).toHaveLength(Object.keys(recorded).length)
    for (const q of AGENT_SUGGESTED) {
      const r = recorded[q]
      expect(r, q).toBeDefined()
      expect(r.query, q).toBe(q)
      expect(r.intent, q).toBe('brief')
      const a = adaptAsk(r, opts)
      expect(a.kind, q).toBe('brief')
      expect(a.facts.length, q).toBeGreaterThan(0)
      expect(a.destinations.length, q).toBeGreaterThan(0)
      for (const f of a.facts) {
        expect(f.source, q).toBeTruthy()
        expect(f.time, q).toBeTruthy()
        if (f.freshness === 'illustrative') expect(f.truth, `${q} ${f.evidenceId}`).not.toBe('obs')
      }
    }
  })
  it('nameRegions leaves unknown ids alone', () => {
    expect(nameRegions('pressure in us-tx and ap-sg', namer)).toBe('pressure in US Central (Dallas hub) and ap-sg')
  })
})
