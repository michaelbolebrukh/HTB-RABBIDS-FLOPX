// The strategy catalogue (N-MOCK, founder: "24 strategies are mentioned, I don't see them anywhere"):
// read from /flopx/research/strategies, never authored, with a status built from counts only.
import { afterEach, describe, expect, it } from 'vitest'
import fixture from './__fixtures__/w2/strategies.json'
import { makeGetter, runModules } from './boot'
import { __resetLiveStore, domain } from './store'
import { __resetStrategies, adaptStrategies, indexWord, statusOf, STRATEGIES_LIVE, strategiesModule, tradesOnOf, type StrategiesResponse } from './strategies'

const P = fixture as unknown as StrategiesResponse

afterEach(() => { __resetStrategies(); __resetLiveStore() })

describe('strategies adapter', () => {
  it('lists the backend catalogue: three strategies today, with name, what each trades on and its forward status', () => {
    const rows = adaptStrategies(P)
    expect(rows.map((r) => r.id)).toEqual(['divergence', 'supply-regime', 'event-reaction'])
    expect(rows[0]).toMatchObject({ name: 'Compute / equity divergence', tradesOn: 'CRWV, NBIS · H100 on-demand index vs the CRWV and NBIS pair', status: 'Forward paper · no decision yet', statusKind: 'idle', hasBacktest: false })
    expect(rows[1].tradesOn).toBe('IREN · H100 on-demand index')
    expect(rows[2].tradesOn).toBe('8 covered names · verified announcement and earnings items')
    expect(rows.every((r) => r.status === 'Forward paper · no decision yet')).toBe(true)
  })
  it('names the index from its series id', () => {
    expect(indexWord('gpu:h100-sxm-80gb:on-demand:global')).toBe('H100 on-demand index')
    expect(indexWord('gpu:b200:on-demand:global')).toBe('B200 on-demand index')
    expect(indexWord('index:fcpi:h100')).toBe('index:fcpi:h100')
  })
  it('the status comes from the counts and nothing else', () => {
    const base = { mode: 'forward', decisions: [], orders: 0, proposed: 0, replays: 0 }
    expect(statusOf(null)).toEqual({ status: 'Not running', kind: 'idle' })
    expect(statusOf({ ...base, proposed: 2 })).toEqual({ status: 'Forward paper · 2 proposed', kind: 'proposed' })
    expect(statusOf({ ...base, decisions: [{}] })).toEqual({ status: 'Forward paper · 1 decision, no order', kind: 'proposed' })
    expect(statusOf({ ...base, orders: 3, proposed: 1 })).toEqual({ status: 'Forward paper · 3 orders · 1 proposed', kind: 'active' })
    expect(statusOf({ ...base, orders: 1 })).toEqual({ status: 'Forward paper · 1 order', kind: 'active' })
    expect(tradesOnOf({ ...P.strategies[0], parameters: {}, universe: ['X'] })).toBe('X')
  })
})

describe('strategies module', () => {
  const answer = (body: unknown, status = 200) => (async () => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })) as unknown as typeof fetch

  it('starts pending with no rows, lands the catalogue, and records when', async () => {
    expect(STRATEGIES_LIVE).toMatchObject({ status: 'pending', strategies: [], when: null })
    await runModules([strategiesModule], { fetchImpl: answer(P), base: 'http://test' })
    expect(STRATEGIES_LIVE.status).toBe('live')
    expect(STRATEGIES_LIVE.strategies).toHaveLength(3)
    expect(STRATEGIES_LIVE.when).toBe('08 Nov 00:40')
    expect(domain('strategies').status).toBe('live')
  })
  it('a failed read is recorded with its reason, and no row is invented', async () => {
    const get = makeGetter({ fetchImpl: answer({ detail: 'down' }, 503), base: 'http://test', retryDelayMs: 1 })
    const ok = await strategiesModule.load(get, () => {})
    expect(ok).toBe(false)
    expect(STRATEGIES_LIVE.status).toBe('failed')
    expect(STRATEGIES_LIVE.error).toBeTruthy()
    expect(STRATEGIES_LIVE.strategies).toEqual([])
  })
})
