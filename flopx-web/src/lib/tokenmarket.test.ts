import { describe, expect, it } from 'vitest'
import snapshot from '@/data/openrouter.json'
import { bits, blended, closedVsOpen, decompose, FEATURES, hedonic, labLadder, leaderboard, median, pctEffect, route, solve, spreadFor, tierModels, TIERS, type EndpointRow, type Model, type Snapshot } from './tokenmarket'

const ep = (o: Partial<EndpointRow> & { provider: string; prompt: number; completion: number }): EndpointRow => ({
  model: 'm', tag: o.provider, quant: 'fp8', tier: 'standard', context: 128000, uptime: 100, up: true,
  tools: true, structured: true, caching: false, timeOfDay: false, discount: 0, ...o,
})
const mdl = (o: Partial<Model> & { id: string; lab: string; prompt: number }): Model => ({ name: o.id, created: 0, open: false, context: 1, completion: o.prompt, ...o })

describe('basics', () => {
  it('blends 3:1 prompt to completion', () => expect(blended({ prompt: 1, completion: 5 })).toBeCloseTo(2))
  it('median handles odd, even and empty', () => {
    expect(median([3, 1, 2])).toBe(2)
    expect(median([4, 1, 2, 3])).toBe(2.5)
    expect(median([])).toBeNaN()
  })
  it('buckets quantization labels', () => {
    expect([bits('fp4'), bits('mxfp4'), bits('fp8'), bits('int8'), bits('bf16'), bits('fp32'), bits('unknown')]).toEqual([4, 4, 8, 8, 16, 16, 0])
  })
  it('solves a small linear system', () => {
    const x = solve([[2, 1], [1, 3]], [3, 5])
    expect(x[0]).toBeCloseTo(0.8)
    expect(x[1]).toBeCloseTo(1.4)
  })
})

describe('labs', () => {
  const ms = [
    mdl({ id: 'a1', lab: 'A', prompt: 1 }), mdl({ id: 'a2', lab: 'A', prompt: 3 }), mdl({ id: 'a3', lab: 'A', prompt: 9, open: true }),
    mdl({ id: 'b1', lab: 'B', prompt: 0.1, open: true }), mdl({ id: 'b2', lab: 'B', prompt: 0.2, open: true }),
  ]
  it('keeps labs with enough models, dearest median first', () => {
    const l = labLadder(ms)
    expect(l.map((r) => r.lab)).toEqual(['A'])
    expect(l[0]).toMatchObject({ median: 3, min: 1, max: 9, open: 1 })
    expect(labLadder(ms, 2).map((r) => r.lab)).toEqual(['A', 'B'])
  })
  it('compares closed and open medians', () => {
    const c = closedVsOpen(ms)
    expect(c.closedMedian).toBe(2)
    expect(c.openMedian).toBeCloseTo(0.2)
    expect(c.ratio).toBeCloseTo(10)
  })
  it('picks the latest model per product line', () => {
    const t = tierModels([
      mdl({ id: 'o1', lab: 'Anthropic', name: 'Claude Opus 5', created: 1, prompt: 5 }),
      mdl({ id: 'o2', lab: 'Anthropic', name: 'Claude Opus 5.5', created: 2, prompt: 5 }),
      mdl({ id: 'g1', lab: 'OpenAI', name: 'GPT-6 Astra', created: 1, prompt: 20 }),
      mdl({ id: 'g2', lab: 'OpenAI', name: 'GPT-6.1 Sol', created: 2, prompt: 4 }),
    ], TIERS.large)
    expect(t.map((m) => m.id).sort()).toEqual(['g1', 'g2', 'o2'])
  })
})

describe('spreads and routing', () => {
  const eps = [
    ep({ provider: 'cheap', prompt: 0.1, completion: 0.1, quant: 'fp8' }),
    ep({ provider: 'mid', prompt: 0.3, completion: 0.3, quant: 'fp8' }),
    ep({ provider: 'dear8', prompt: 0.4, completion: 0.4, quant: 'fp8' }),
    ep({ provider: 'dear', prompt: 1, completion: 1, quant: 'unknown' }),
    ep({ provider: 'down', prompt: 0.01, completion: 0.01, up: false }),
    ep({ provider: 'prio', prompt: 0.05, completion: 0.05, tier: 'priority' }),
    ep({ provider: 'notools', prompt: 0.08, completion: 0.08, tools: false }),
  ]
  it('measures spread over standard endpoints that are up', () => {
    const s = spreadFor('m', eps)!
    expect(s.cheapest.provider).toBe('notools')
    expect(s.dearest.provider).toBe('dear')
    expect(s.spread).toBeCloseTo(12.5)
    expect(s.samePrecision).toEqual({ bits: 8, spread: expect.closeTo(5) })
  })
  it('returns null with fewer than two live hosts', () => expect(spreadFor('m', [eps[0]])).toBeNull())
  it('routes to the cheapest endpoint that meets the job', () => {
    const r = route('m', eps, { promptM: 75, completionM: 25, minUptime: 99, standardOnly: true, tools: true })
    expect(r.best!.e.provider).toBe('cheap')
    expect(r.best!.cost).toBeCloseTo(10)
    expect(r.plans.map((p) => p.e.provider)).not.toContain('down')
    expect(r.plans.map((p) => p.e.provider)).not.toContain('prio')
    expect(route('m', eps, { promptM: 1, completionM: 0, minUptime: 0, standardOnly: false, tools: false }).best!.e.provider).toBe('prio')
  })
  it('handles a job no endpoint meets', () => {
    const r = route('m', eps, { promptM: 1, completionM: 1, minUptime: 101, standardOnly: true, tools: true })
    expect(r.best).toBeNull()
    expect(r.saving).toBe(0)
  })
})

describe('hedonic fit', () => {
  // Synthetic market: log price = model FE + 0.5·priority − 0.2·4bit, no noise.
  const truth = { priority: 0.5, b4: -0.2 }
  const eps: EndpointRow[] = []
  ;['m1', 'm2', 'm3'].forEach((m, i) => {
    const base = Math.log(0.2 * (i + 1))
    for (const [tier, quant] of [['standard', 'fp8'], ['priority', 'fp8'], ['standard', 'fp4'], ['priority', 'fp4'], ['standard', 'fp8']] as const) {
      const lp = base + (tier === 'priority' ? truth.priority : 0) + (quant === 'fp4' ? truth.b4 : 0)
      eps.push(ep({ model: m, provider: `${m}-${eps.length}`, prompt: Math.exp(lp), completion: Math.exp(lp), tier, quant }))
    }
  })
  const fit = hedonic(eps, 1e-6)
  const c = (k: string) => fit.coef[FEATURES.findIndex((f) => f.key === k)]
  it('recovers known coefficients with model fixed effects', () => {
    expect(c('priority')).toBeCloseTo(truth.priority, 4)
    expect(c('b4')).toBeCloseTo(truth.b4, 4)
    expect(fit.r2).toBeCloseTo(1, 6)
    expect(fit.models).toBe(3)
  })
  it('decomposes a price gap exactly into explained and unexplained', () => {
    const d = decompose(fit, eps[1], eps[2])!
    expect(d.explained + d.unexplained).toBeCloseTo(d.gap, 8)
    expect(d.gap).toBeCloseTo(100 * (truth.priority - truth.b4), 4)
  })
  it('converts log coefficients to price effects', () => expect(pctEffect(Math.log(1.5))).toBeCloseTo(50))
  it('ranks hosts by median distance from fair value', () => {
    const lb = leaderboard(fit, 1)
    expect(lb.length).toBe(15)
    lb.forEach((r) => expect(Math.abs(r.vsFair)).toBeLessThan(0.01))
  })
})

describe('snapshot', () => {
  const s = snapshot as Snapshot
  it('has models, open weights and endpoints with positive prices', () => {
    expect(s.models.length).toBeGreaterThan(100)
    expect(s.models.some((m) => m.open)).toBe(true)
    expect(s.endpoints.every((e) => blended(e) > 0)).toBe(true)
  })
  it('fits the real snapshot with model identity explaining most of the price', () => {
    const f = hedonic(s.endpoints)
    expect(f.r2).toBeGreaterThan(f.r2Model)
    expect(f.r2Model).toBeGreaterThan(0.5)
  })
})
