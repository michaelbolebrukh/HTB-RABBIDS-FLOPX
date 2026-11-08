// W5 adapter against payloads recorded from https://flopx-backend.onrender.com on 08 Nov 2026
// 02:10 UTC: POST /flopx/procure/plan for H100 and H200 at 8 and 64 GPUs over 12 weeks, the live
// offers for those two GPUs (sample offers trimmed to the two the plans name), and the 422 body
// /flopx/forecast returns for index:fcpi:h100-sxm-80gb (3 of 60 points).
import { beforeEach, describe, expect, it } from 'vitest'
import type { Offer, Provider } from '@/data/core'
import {
  __resetProcureCaches, adaptPlan, AREA_REGIONS, buildScenarios, cheapestOf, DEFAULT_SPEC, fetchForecast, fetchPlan, hourOf, loadProcure, timingGateOf, toWorkload, uncertaintyOf, VAST_MARKET_READING,
  type ForecastResponse, type ProcurePlanResponse, type ProcureSpec,
} from './adapt-w5'
import { applyOffers } from './gpu'
import type { OffersResponse } from './types'
import h100x8 from './__fixtures__/w5/plan-h100-8-none.json'
import h200x8 from './__fixtures__/w5/plan-h200-8-none.json'
import h200x8high from './__fixtures__/w5/plan-h200-8-high.json'
import h100x64 from './__fixtures__/w5/plan-h100-64-none.json'
import offersJson from './__fixtures__/w5/offers-h100-h200.json'
import refusedJson from './__fixtures__/w5/forecast-h100-refused.json'

const H100_8 = h100x8 as unknown as ProcurePlanResponse
const H200_8 = h200x8 as unknown as ProcurePlanResponse
const H200_8_HIGH = h200x8high as unknown as ProcurePlanResponse
const H100_64 = h100x64 as unknown as ProcurePlanResponse
const OFFERS_RESP = offersJson as unknown as OffersResponse
const NOW = new Date('2026-11-08T02:17:33Z')

// The live offers and providers as W0's gpu module leaves them in OFFERS / PROVIDERS.
const offers: Offer[] = []
const providers: Provider[] = []
applyOffers(OFFERS_RESP, offers, providers)
const regionName = (id: string) => ({ 'ap-jp': 'Japan (Tokyo hub)', 'eu-no': 'Nordics (Oslo hub)', 'eu-de': 'Germany (Frankfurt hub)', 'ap-sg': 'Singapore hub', global: 'Location not published', is: 'Iceland' })[id] ?? id
const SPEC8: ProcureSpec = { ...DEFAULT_SPEC, gpu: 'h200', n: 8 }

describe('toWorkload', () => {
  it('sends the design spec as the backend workload: model id, hours, count, budget in USD', () => {
    const w = toWorkload(DEFAULT_SPEC, 'flex', NOW)!
    expect(w.gpuModelIds).toEqual(['h100-sxm-80gb'])
    expect(w.durationHours).toBe(12 * 168)
    expect(w.gpuCount).toBe(64)
    expect(w.gpuHours).toBeUndefined() // the backend takes exactly one of gpuCount and gpuHours
    expect(w.budgetUsd).toBe(1_500_000)
    expect(w.interruptionTolerance).toBe('none')
    expect(w.regionIds).toBeUndefined()
  })
  it('turns "must start within N days" into a completion deadline of now (hour floor) + N days + duration', () => {
    const w = toWorkload(DEFAULT_SPEC, 'flex', NOW)!
    expect(hourOf(NOW).toISOString()).toBe('2026-11-08T02:00:00.000Z')
    expect(w.deadline).toBe(new Date(Date.UTC(2026, 10, 8, 2) + 30 * 86_400_000 + 2016 * 3_600_000).toISOString().replace('.000Z', 'Z'))
  })
  it('run now keeps two hours of slack on the duration; interruptible asks with tolerance high', () => {
    expect(toWorkload(DEFAULT_SPEC, 'now', NOW)!.deadline).toBe(new Date(Date.UTC(2026, 10, 8, 2) + 2 * 3_600_000 + 2016 * 3_600_000).toISOString().replace('.000Z', 'Z'))
    expect(toWorkload(DEFAULT_SPEC, 'interrupt', NOW)!.interruptionTolerance).toBe('high')
  })
  it('maps a location area to backend hub ids plus the ISO codes Vast asks carry, and InfiniBand to a performance need', () => {
    const w = toWorkload({ ...DEFAULT_SPEC, area: 'EU', ib: true }, 'flex', NOW)!
    expect(w.regionIds).toEqual(AREA_REGIONS.EU)
    expect(w.regionIds).toContain('eu-de')
    expect(w.regionIds).toContain('de')
    expect(w.performanceNeeds).toEqual(['fast-interconnect'])
  })
  it('returns null for a GPU with no backend model (MI300X)', () => {
    expect(toWorkload({ ...DEFAULT_SPEC, gpu: 'mi300x' }, 'flex', NOW)).toBeNull()
  })
})

describe('adaptPlan', () => {
  it('H200 at 8 GPUs: rank 1 is a live Vast ask, and live plans hide the sample ones', () => {
    const plan = adaptPlan(H200_8, { offers, providers, regionName })
    expect(plan.tier).toBe('live')
    expect(plan.returned).toBe(10)
    expect(plan.options.map((o) => o.id)).toEqual(['vast-59208031', 'vast-59648345'])
    const top = plan.options[0]
    expect(top.providerName).toBe('Vast.ai')
    expect(top.regionName).toBe('Japan (Tokyo hub)')
    expect(top.price).toBe(3.2897)
    expect(top.total).toBe(53056.28)
    expect(top.lo).toBe(51729.87)
    expect(top.hi).toBe(55709.1)
    expect(top.contract).toBe('On-demand')
    expect(top.freshness).toBe('live')
    expect(top.observed).toBe('08 Nov 02:10')
    expect(top.marketReading).toBe(VAST_MARKET_READING)
    expect(top.live).toBe(true)
    expect(top.risks).toEqual(['Expected duration overruns the start-by window'])
    expect(plan.freshness).toBe('live')
    expect(plan.asOf).toBe('08 Nov 02:11')
    expect(plan.caveats).toEqual(['advertised asks, not quotes', 'spot restart overhead assumed', 'no egress or storage costs'])
  })
  it('H100 at 64 GPUs: no live ask lists 64 GPUs, so the sample-tier plans show and every one reads illustrative', () => {
    const plan = adaptPlan(H100_64, { offers, providers, regionName })
    expect(plan.tier).toBe('sample')
    expect(plan.freshness).toBe('illustrative')
    expect(plan.options).toHaveLength(2)
    expect(plan.options.every((o) => o.freshness === 'illustrative' && !o.live)).toBe(true)
    expect(plan.options[0].providerName).toBe('Atlas Hyperscale')
    expect(plan.options[0].regionName).toBe('Germany (Frankfurt hub)')
    expect(plan.options[0].contract).toBe('Reserved')
    expect(plan.options[0].observed).toBe('n/a')
    expect(Number.isNaN(plan.options[0].reliability)).toBe(true)
    expect(plan.options[0].risks[0]).toBe('FLOPX sample ask: an authored example, not a live quote')
    expect(plan.options[0].risks).toContain('Reserved commitment')
    expect(plan.excluded['gpu-count']).toBe(12)
  })
  it('H100 at 8 GPUs: the one live ask (Akash, 11 GPUs, location not published) is shown alone, sample rank 1 and 2 hidden', () => {
    const plan = adaptPlan(H100_8, { offers, providers, regionName })
    expect(plan.tier).toBe('live')
    expect(plan.options.map((o) => o.id)).toEqual(['akash-h100-80gi-sxm5'])
    expect(plan.options[0].regionName).toBe('Location not published')
    expect(plan.options[0].marketReading).toBeUndefined()
  })
  it('drops plans whose provider reports a reliability below the minimum; unknown reliability passes', () => {
    const vast = providers.find((p) => p.id === 'vast-ai')!
    expect(vast.reliability).toBeGreaterThan(80)
    expect(adaptPlan(H200_8, { offers, providers, regionName, minRel: 80 }).options).toHaveLength(2)
    expect(adaptPlan(H200_8, { offers, providers, regionName, minRel: vast.reliability + 1 }).tier).toBe('none')
    expect(adaptPlan(H100_64, { offers, providers, regionName, minRel: 101 }).options).toHaveLength(2)
  })
  it('an interruptible plan carries the interruptible flag and the spot copy', () => {
    const plan = adaptPlan(H200_8_HIGH, { offers: [], providers, regionName })
    const spot = plan.options.find((o) => o.id === 'offer-039')!
    expect(spot.interruptible).toBe(true)
    expect(spot.contract).toBe('Spot')
    expect(spot.risks).toContain('Interruptible capacity: an assumed restart overhead is included in cost and duration')
  })
  it('cheapestOf prefers plans that meet the deadline and the lowest expected cost', () => {
    const plan = adaptPlan(H100_64, { offers, providers, regionName })
    expect(cheapestOf(plan)!.id).toBe('offer-052')
    expect(cheapestOf(null)).toBeNull()
  })
})

describe('buildScenarios', () => {
  it('builds Run now, Flexible and Interruptible from the three backend plans and Second venue from the flexible plan', () => {
    const flex = adaptPlan(H200_8, { offers, providers, regionName })
    const now = adaptPlan(H200_8, { offers, providers, regionName })
    const interrupt = adaptPlan(H200_8_HIGH, { offers: [], providers, regionName })
    const sc = buildScenarios(SPEC8, now, flex, interrupt, false)!
    expect(sc.rows.map((r) => r.id)).toEqual(['now', 'flex', 'interrupt'])
    const [runNow, flexible, inter] = sc.rows
    expect(runNow.cost).toBe(53056.28)
    expect(runNow.live).toBe(true)
    expect(flexible.cost).toBe(53056.28)
    expect(flexible.note).toBe('Same as run now, because the timing gate is closed')
    expect(inter.cost).toBe(24482.3)
    expect(inter.feasible).toBe(false)
    expect(inter.live).toBe(false)
    expect(inter.note).toContain('this workload does not allow interruption')
    expect(runNow.recommended).toBe(true)
    expect(sc.median).toBe((53056.28 + 109310.75) / 2)
    expect(sc.n).toBe(2)
    expect(sc.freshness).toBe('illustrative') // one row rests on a sample ask
    expect(sc.job).toBe('8 × H200 · 12 wk · start within 30 d')
  })
  it('adds a Second venue row only when another provider exists, and reads live when every row rests on live asks', () => {
    const flex = adaptPlan(H200_8, { offers, providers, regionName })
    const sc = buildScenarios(SPEC8, flex, flex, null, false)!
    expect(sc.rows.map((r) => r.id)).toEqual(['now', 'flex']) // both live asks are Vast: no second venue, no interruptible row
    expect(sc.freshness).toBe('live')
    const sample = adaptPlan(H100_64, { offers, providers, regionName })
    const sc2 = buildScenarios({ ...DEFAULT_SPEC }, sample, sample, null, false)!
    expect(sc2.rows.map((r) => r.id)).toEqual(['now', 'flex']) // both sample asks are Atlas Hyperscale
    expect(sc2.freshness).toBe('illustrative')
  })
  it('returns null when no plan has anything behind it', () => {
    expect(buildScenarios(SPEC8, null, null, null, false)).toBeNull()
    const none = adaptPlan(H200_8, { offers, providers, regionName, minRel: 99 })
    expect(buildScenarios(SPEC8, none, none, none, false)).toBeNull()
  })
})

describe('timing gate and uncertainty from the forecast', () => {
  it('the recorded refusal closes the gate with the backend counts, skill not measurable', () => {
    const g = timingGateOf({ kind: 'refused', detail: (refusedJson as { detail: { code: string; reason?: string; history_points?: number; horizon?: number; required_points?: number } }).detail })!
    expect(g.open).toBe(false)
    expect(g.historyPoints).toBe(3)
    expect(g.historyNeeded).toBe(60)
    expect(g.skillPct).toBeNull()
    expect(g.note).toContain('3 of 60 daily points of history (history-shorter-than-twice-horizon)')
  })
  it('a failed call gives no gate (the page keeps the design snapshot)', () => {
    expect(timingGateOf({ kind: 'failed', error: 'timeout' })).toBeNull()
    expect(timingGateOf(null)).toBeNull()
  })
  const answered: ForecastResponse = {
    seriesId: 'index:fcpi:h100-sxm-80gb', horizon: 30, level: 0.8, historyPoints: 90, chosen: 'drift',
    baselines: [{ method: 'naive', residualStd: 0.1, points: [{ step: 30, t: '2026-12-08T00:00:00Z', mean: 2.5, lower: 2.2, upper: 2.8 }] }, { method: 'drift', residualStd: 0.1, points: [{ step: 1, t: '2026-11-09T00:00:00Z', mean: 2.5, lower: 2.4, upper: 2.6 }, { step: 30, t: '2026-12-08T00:00:00Z', mean: 2.4, lower: 2.0, upper: 2.75 }] }],
    backtest: [{ method: 'drift', mae: 0.1, mape: 0.04, maeVsNaive: 0.92, origins: 30 }],
    caveats: [], exploratory: false, exploratoryReasons: [],
    provenance: { kind: 'derived', source: 'forecast:baselines-v1', observedAt: null, inputKinds: ['live'] },
    freshness: { generatedAt: '2026-11-08T02:00:00Z' },
  }
  it('an answered forecast opens the gate when the chosen baseline beats naive by the design bar', () => {
    const g = timingGateOf({ kind: 'ok', data: answered })!
    expect(g.open).toBe(true)
    expect(g.skillPct).toBeCloseTo(8, 6)
    expect(g.historyNeeded).toBe(60)
    expect(timingGateOf({ kind: 'ok', data: { ...answered, exploratory: true, exploratoryReasons: ['thin-history-relative-to-horizon'] } })!.open).toBe(false)
  })
  it('uncertainty is the chosen baseline\'s end-of-horizon change against the current level, in %', () => {
    const u = uncertaintyOf(answered, 2.5)!
    expect(u.mean).toBeCloseTo(-4, 6)
    expect(u.p10).toBeCloseTo(-20, 6)
    expect(u.p90).toBeCloseTo(10, 6)
    expect(uncertaintyOf(answered, NaN)).toBeNull()
  })
})

describe('fetching', () => {
  beforeEach(() => __resetProcureCaches())
  const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

  it('posts the workload once per distinct spec and caches the answer', async () => {
    const calls: { url: string; body: unknown }[] = []
    const fetchImpl: typeof fetch = async (url, init) => {
      calls.push({ url: String(url), body: JSON.parse(String(init?.body)) })
      return json(H200_8)
    }
    const w = toWorkload(SPEC8, 'flex', NOW)!
    const [a, b] = await Promise.all([fetchPlan(w, { fetchImpl, base: 'http://x' }), fetchPlan(w, { fetchImpl, base: 'http://x' })])
    expect(a.ok && b.ok).toBe(true)
    expect(calls).toHaveLength(1)
    expect(calls[0].url).toBe('http://x/flopx/procure/plan')
    expect(calls[0].body).toEqual({ workload: w })
  })
  it('a 422 from /flopx/forecast is a refusal with its reason, not a failure; a 500 is a failure and is not cached', async () => {
    let n = 0
    const fetchImpl: typeof fetch = async (url) => {
      n++
      return String(url).endsWith('/flopx/forecast') ? json(refusedJson, 422) : json({}, 500)
    }
    const r = await fetchForecast('h100-sxm-80gb', { fetchImpl, base: 'http://x' })
    expect(r.kind).toBe('refused')
    if (r.kind === 'refused') expect(r.detail.history_points).toBe(3)
    expect(await fetchForecast('h100-sxm-80gb', { fetchImpl, base: 'http://x' })).toBe(r) // cached
    expect(n).toBe(1)
    const bad: typeof fetch = async () => json({}, 500)
    expect((await fetchForecast('h200-sxm-141gb', { fetchImpl: bad, base: 'http://x' })).kind).toBe('failed')
    expect((await fetchForecast('h200-sxm-141gb', { fetchImpl: bad, base: 'http://x' })).kind).toBe('failed')
  })
  it('loadProcure assembles the page state from the three plans and the forecast', async () => {
    const fetchImpl: typeof fetch = async (url, init) => {
      if (String(url).endsWith('/flopx/forecast')) return json(refusedJson, 422)
      const body = JSON.parse(String(init?.body)) as { workload: { interruptionTolerance: string } }
      return json(body.workload.interruptionTolerance === 'high' ? H200_8_HIGH : H200_8)
    }
    applyOffers(OFFERS_RESP) // the live offers W0's module leaves in OFFERS, which loadProcure matches plans against
    const s = await loadProcure(SPEC8, NOW, { fetchImpl, base: 'http://x' })
    expect(s.status).toBe('live')
    expect(s.plan!.tier).toBe('live')
    expect(s.scenarios!.rows.map((r) => r.id)).toEqual(['now', 'flex'])
    expect(s.gate!.open).toBe(false)
    expect(s.forecast!.kind).toBe('refused')
  })
  it('loadProcure reports a GPU with no backend model as failed without a request', async () => {
    const fetchImpl: typeof fetch = async () => {
      throw new Error('must not be called')
    }
    const s = await loadProcure({ ...DEFAULT_SPEC, gpu: 'mi300x' }, NOW, { fetchImpl, base: 'http://x' })
    expect(s.status).toBe('failed')
    expect(s.error).toContain('mi300x')
  })
})
