import { describe, expect, it } from 'vitest'
import { AGENT_TOOLS, BREV, ENERGY_SUMMARY, FUND_TESTS, NEWS_SUMMARY, PM_CALIBRATION, PROCUREMENT, TOKENS, XRAY_CAPACITY, derived } from '@/data/handoff'
import type { Store } from '@/lib/store'
import type { AskResponse } from '@/live/adapt-w4'
import h100RouteJson from '@/live/__fixtures__/w4/ask-h100-route.json'
import h100Json from '@/live/__fixtures__/w4/ask-h100.json'
import texasJson from '@/live/__fixtures__/w4/ask-texas.json'
import { AGENT_SUGGESTED, answer, askFlopx } from './ask'

const nav: string[] = []
const store = { inspector: null, xray: {}, navigate: (p: string) => { nav.push(p) } } as unknown as Store
const ask = (q: string) => answer(q, store)
const text = (q: string) => ask(q).facts.map((f) => f.text).join(' ')

describe('ask router', () => {
  const cases: [string, string][] = [
    ['What do labs charge per million tokens?', 'token_prices'],
    ['Frontier vs open model price', 'token_prices'],
    ['Spread between providers of the same model', 'token_providers'],
    ['Is Brev a useful second venue?', 'brev_venue'],
    ['Best procurement plan for the job', 'plan_job'],
    ['Is the timing gate open?', 'timing_gate'],
    ['Where is hidden capacity? scarcity', 'scarcity'],
    ['How much does power cost?', 'power_cost'],
    ['Do prediction markets help?', 'prediction_markets'],
    ['Any news headlines?', 'news'],
    ['Do we have an edge?', 'fund_tests'],
  ]
  it.each(cases)('%s routes to %s', (q, tool) => {
    const a = ask(q)
    expect(a.tool).toBe(tool)
    expect(AGENT_TOOLS.some((t) => t.id === a.tool)).toBe(true)
    expect(a.facts.length).toBeGreaterThan(0)
    expect(a.facts.every((f) => f.source && f.time)).toBe(true)
  })

  it('every prepared Agent Desk question names one target in the shape the backend grammar briefs (recorded payloads: live/adapt-w4.test.ts)', () => {
    expect(AGENT_SUGGESTED.length).toBeGreaterThan(0)
    for (const q of AGENT_SUGGESTED) expect(q, q).toMatch(/^(What is happening in|Tell me about the|How is|What is the state of) /)
  })

  it('unknown question is Insufficient with no facts', () => {
    const a = ask('What is the meaning of life?')
    expect(a.confidence).toBe('Insufficient')
    expect(a.facts).toEqual([])
    expect(a.tool).toBeUndefined()
  })

  it('facts match hand-off constants', () => {
    expect(text('provider spread same model')).toContain(`${derived.providerRatio.toFixed(1)}×`)
    expect(derived.providerRatio.toFixed(1)).toBe('6.8')
    expect(text('procurement plan')).toContain(PROCUREMENT.plans.find((p) => p.recommended)!.cost.toFixed(2))
    expect(text('procurement plan')).toContain('10.8%')
    expect(text('token prices')).toContain(`$${TOKENS.labs[0].median.toFixed(2)}`)
    expect(text('brev')).toContain(BREV.rows.toLocaleString())
    expect(text('timing gate')).toContain('closed')
    expect(text('scarcity')).toContain('39%')
    expect(text('scarcity')).toContain(String(XRAY_CAPACITY.trackedGW))
    expect(text('scarcity')).toContain('65 (51 to 76)')
    expect(text('power cost')).toContain(`$${ENERGY_SUMMARY.cheapest.price}/MWh`)
    expect(text('power cost')).toContain(`$${ENERGY_SUMMARY.dearest.price}/MWh`)
    expect(text('prediction markets')).toContain(`${PM_CALIBRATION.week.cutPct}%`)
    expect(text('news')).toContain(String(NEWS_SUMMARY.headlines))
    expect(text('fund edge')).toContain(`${FUND_TESTS.edges} of ${FUND_TESTS.tests}`)
  })

  it('never claims timing alpha or mispricing exploitation', () => {
    expect(ask('procurement plan').interpretation).toContain('not a claim that the market is mispriced')
    expect(ask('timing gate').interpretation).toContain('may not time')
  })

  it('actions navigate to a page', () => {
    nav.length = 0
    ask('scarcity').actions[0].run(store)
    ask('fund edge').actions[0].run(store)
    expect(nav).toEqual(['xray', 'fund'])
  })
})

describe('askFlopx (the backend path, recorded payloads)', () => {
  const texas = texasJson as unknown as AskResponse
  const h100Route = h100RouteJson as unknown as AskResponse
  const h100 = h100Json as unknown as AskResponse
  const ok = (data: AskResponse) => ({ ok: true as const, data, ms: 1 })
  const fail = { ok: false as const, error: 'timeout after 25000 ms', ms: 25000 }
  const xrayPatches: unknown[] = []
  const st = { inspector: null, xray: {}, navigate: (p: string) => { nav.push(p) }, setXray: (p: unknown) => { xrayPatches.push(p) } } as unknown as Store

  it('a brief answer carries the backend facts with per-row truth and freshness, the engine line, and no illustrative flag', async () => {
    const calls: string[] = []
    const a = await askFlopx('What is happening in Texas?', st, 'Agents', { post: async (q) => { calls.push(q); return ok(texas) } })
    expect(calls).toEqual(['What is happening in Texas?'])
    expect(a.illustrative).toBeUndefined()
    expect(a.offline).toBeUndefined()
    expect(a.engine).toBe('composed brief · composed-from-evidence-v1')
    expect(a.facts).toHaveLength(11)
    expect(a.facts[0].freshness).toBe('illustrative')
    expect(a.facts[0].truth).toBeNull()
    expect(a.facts.filter((f) => f.freshness === 'live').every((f) => f.truth === 'obs')).toBe(true)
    expect(a.confidence).toBe('Low')
    expect(a.context).toBe('Agents')
  })

  it('a route-only answer naming a target fetches that target\'s brief with a second call and keeps the first screen first', async () => {
    const calls: string[] = []
    const a = await askFlopx('Why are H100 quotes higher here?', st, '', { post: async (q) => { calls.push(q); return ok(calls.length === 1 ? h100Route : h100) } })
    expect(calls).toEqual(['Why are H100 quotes higher here?', 'tell me about h100-sxm-80gb'])
    expect(a.facts).toHaveLength(7)
    expect(a.actions[0].label).toBe('Open GPU Markets')
    expect(a.engine).toBe('grammar · intent gpu + composed brief')
    expect(a.tool).toBeUndefined()
    nav.length = 0
    a.actions[0].run(st)
    expect(nav).toEqual(['markets'])
  })

  it('X-Ray destinations patch the X-Ray state before navigating', async () => {
    const a = await askFlopx('What is happening in Texas?', st, '', { post: async () => ok(texas) })
    const x = a.actions.find((ac) => ac.label.includes('X-Ray'))!
    nav.length = 0
    xrayPatches.length = 0
    x.run(st)
    expect(nav).toEqual(['xray'])
    expect(xrayPatches).toEqual([{ region: 'us-tx' }])
  })

  it('when the backend does not answer, the design\'s canned answer stands in, badged illustrative and saying why', async () => {
    const a = await askFlopx('Is the timing gate open?', st, '', { post: async () => fail })
    expect(a.illustrative).toBe(true)
    expect(a.offline).toBe('timeout after 25000 ms')
    expect(a.tool).toBe('timing_gate')
    expect(a.interpretation).toContain('Offline: the FLOPX backend did not answer (timeout after 25000 ms)')
    expect(a.facts[0].freshness).toBeUndefined()
  })
})
