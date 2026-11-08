import { afterEach, describe, expect, it } from 'vitest'
import { __resetAccount, ACCOUNT_LIVE, accountFresh, accountModule, adaptAccount, adaptOrders, type PaperAccountWire, type PaperOrdersWire } from './account'
import { makeGetter, runModules } from './boot'
import { __resetLiveStore, domain, getVersion } from './store'
import { __resetTokens, setTokens } from './token'

const live = { kind: 'live' as const, source: 'alpaca-data', observedAt: '2026-11-07T21:00:00Z' }
const sample = { kind: 'sample' as const, source: 'flopx-sample', observedAt: '2026-11-07T21:00:00Z' }

const ACCOUNT: PaperAccountWire = {
  paper: true,
  cashUsd: 9_400_000,
  equityUsd: 10_120_000,
  buyingPowerUsd: 18_800_000,
  realisedPnlUsd: 12_500,
  unrealisedPnlUsd: 107_500,
  maxDrawdownPct: 1.8,
  positions: [
    { ticker: 'VST', quantity: 3000, avgCost: 150, lastPrice: 160, marketValueUsd: 480_000, unrealisedPnlUsd: 30_000, weight: 0.047, priceProvenance: live },
    { ticker: 'NVDA', quantity: 1200, avgCost: 180, lastPrice: 200, marketValueUsd: 240_000, unrealisedPnlUsd: 24_000, weight: 0.024, priceProvenance: live },
  ],
  exposureByTheme: { power: 0.047 },
  openOrders: 1,
  freshness: { generatedAt: '2026-11-08T08:40:00Z' },
}
const ORDERS: PaperOrdersWire = {
  orders: [
    { id: '0b1c2d3e-aaaa-bbbb-cccc-ddddeeeeffff', ticker: 'VST', side: 'buy', orderType: 'market', quantity: 3000, notionalUsd: 450_000, limitPrice: null, status: 'filled', origin: 'manual', reason: null, fillPrice: 150.04, fillProvenance: live, filledAt: '2026-11-06T15:00:00Z', createdAt: '2026-11-06T14:59:00Z' },
    { id: 'f1e2d3c4-aaaa-bbbb-cccc-ddddeeeeffff', ticker: 'NVDA', side: 'sell', orderType: 'limit', quantity: 100, notionalUsd: null, limitPrice: 210, status: 'pending', origin: 'strat-momentum', reason: null, fillPrice: null, fillProvenance: null, filledAt: null, createdAt: '2026-11-07T10:00:00Z' },
  ],
  freshness: { generatedAt: '2026-11-08T08:40:00Z' },
}

afterEach(() => {
  __resetAccount()
  __resetTokens()
  __resetLiveStore()
})

describe('account adapters', () => {
  it('maps the paper account to the page shape, market value from the backend', () => {
    const a = adaptAccount(ACCOUNT)
    expect(a).toMatchObject({ nav: 10_120_000, cash: 9_400_000, buyingPower: 18_800_000, realised: 12_500, unrealised: 107_500, openOrders: 1 })
    expect(a.positions[0]).toMatchObject({ ticker: 'VST', qty: 3000, avg: 150, last: 160, mv: 480_000, pnl: 30_000, fresh: { freshness: 'live' } })
  })
  it('the account badge follows the marks: live marks are live, a sample mark makes it illustrative, cash only is exact', () => {
    expect(accountFresh(ACCOUNT.positions, ACCOUNT.freshness.generatedAt)).toMatchObject({ freshness: 'live' })
    expect(accountFresh([{ ...ACCOUNT.positions[0], priceProvenance: sample }], ACCOUNT.freshness.generatedAt)).toMatchObject({ freshness: 'illustrative' })
    expect(accountFresh([], ACCOUNT.freshness.generatedAt).label).toMatch(/cash only/)
  })
  it('maps orders to the design list, newest first, with the backend vocabulary translated', () => {
    const o = adaptOrders(ORDERS.orders)
    expect(o.map((x) => x.ticker)).toEqual(['NVDA', 'VST'])
    expect(o[0]).toMatchObject({ side: 'Sell', type: 'Limit', limit: 210, status: 'Pending', origin: 'Model', note: 'strat-momentum' })
    expect(o[1]).toMatchObject({ side: 'Buy', type: 'Market', status: 'Filled', origin: 'User', fillPx: 150.04, id: '0B1C2D3E' })
  })
})

describe('account module', () => {
  const answer = (url: string) => {
    if (url.endsWith('/flopx/paper/account')) return ACCOUNT
    if (url.endsWith('/flopx/paper/orders')) return ORDERS
    return {}
  }
  const okFetch = (async (url: string, init?: RequestInit) => {
    const auth = (init?.headers as Record<string, string>)?.authorization
    if (!auth) return new Response('{"detail":"Not authenticated"}', { status: 401 })
    return new Response(JSON.stringify(answer(url)), { status: 200, headers: { 'content-type': 'application/json' } })
  }) as unknown as typeof fetch

  it('signed out: records anonymous and leaves the design account in place', async () => {
    await runModules([accountModule], { fetchImpl: okFetch, base: 'http://x' })
    expect(ACCOUNT_LIVE.state.status).toBe('anonymous')
    expect(domain('account')).toMatchObject({ status: 'failed', error: 'sign-in required' })
  })
  it('signed in: the re-run fetches with the token, lands the account and bumps the version', async () => {
    setTokens({ accessToken: 'a.b.c', accessExpiry: '2099-01-01T00:00:00Z', refreshToken: null, email: 'demo@flopx.vercel.app' })
    const v0 = getVersion()
    await runModules([accountModule], { fetchImpl: okFetch, base: 'http://x' })
    const s = ACCOUNT_LIVE.state
    expect(s.status).toBe('live')
    if (s.status === 'live') {
      expect(s.email).toBe('demo@flopx.vercel.app')
      expect(s.account.nav).toBe(10_120_000)
      expect(s.orders).toHaveLength(2)
      expect(s.fresh.freshness).toBe('live')
    }
    expect(getVersion()).toBeGreaterThan(v0)
    expect(domain('account').status).toBe('live')
  })
  it('signed in but refused: says so instead of showing the design numbers as the account', async () => {
    setTokens({ accessToken: 'stale.b.c', accessExpiry: '2099-01-01T00:00:00Z', refreshToken: null, email: 'demo@flopx.vercel.app' })
    const refuse = (async () => new Response('{"detail":"expired"}', { status: 401 })) as unknown as typeof fetch
    const get = makeGetter({ fetchImpl: refuse, base: 'http://x' })
    const ok = await accountModule.load(get, () => {})
    expect(ok).toBe(false)
    expect(ACCOUNT_LIVE.state).toMatchObject({ status: 'failed', email: 'demo@flopx.vercel.app', error: expect.stringMatching(/401/) })
  })
})
