// The mock book on the pages (N-MOCK): shown by default signed out and for an empty paper account,
// labelled on every panel it feeds, hidden by the toggle, and never shown over a non-empty account.
// Server-rendered inside the store provider, like src/pages/labels.test.tsx.
import { afterEach, describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { StoreProvider } from '@/lib/store'
import Trading from './Trading'
import Fund from './Fund'
import { __resetAccount, setAccountState } from '@/live/account'
import { __resetMockDesk, MOCK_TICKERS, mockBook, mockDeskLabel, setMockPref } from '@/data/mockDesk'

;(globalThis as { window?: unknown }).window = { location: { hash: '#/trading' }, sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} } }

const render = (el: React.ReactElement) => renderToStaticMarkup(<StoreProvider>{el}</StoreProvider>)
const labels = (html: string) => html.split(mockDeskLabel).length - 1
/** The data-fresh kind of the element each rendered label sits in (the toggle's title attribute is not a rendered note): a quiet note, never a chip. */
const labelKinds = (html: string) =>
  [...html.matchAll(new RegExp(mockDeskLabel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'g'))]
    .filter((m) => html[m.index + mockDeskLabel.length] !== '"')
    .map((m) => /data-fresh="(\w+)"[^>]*>(?:(?!data-fresh=)[\s\S])*$/.exec(html.slice(0, m.index))?.[1] ?? 'none')
const held = () => setAccountState({
  status: 'live', email: 'founder@example.com', ordersError: null, generatedAt: '2026-11-08T09:00:00Z', fresh: { freshness: 'historical', label: 'Paper account · archived marks · 08 Nov 09:00' },
  account: { nav: 1_001_234, cash: 901_234, buyingPower: 1_401_851, realised: 0, unrealised: 1_234, maxDrawdownPct: 0.3, openOrders: 0, positions: [{ ticker: 'NVDA', qty: 500, avg: 184.5, last: 186.7, mv: 93_350, pnl: 1_100, weight: 0.093, fresh: { freshness: 'historical' } }] },
  orders: [{ id: 'A1B2C3D4', time: '07 Nov 15:01', ticker: 'NVDA', side: 'Buy', qty: 500, type: 'Market', status: 'Filled', origin: 'User', fillPx: 184.5 }],
})

afterEach(() => { __resetAccount(); __resetMockDesk() })

describe('Trading with the mock book', () => {
  it('signed out: the toggle is on, every mock panel carries the label once, the eight tickers show and the design holdings do not', () => {
    const html = render(<Trading />)
    expect(html).toContain('Show mock book')
    expect(html).toContain('aria-checked="true"')
    // KPI strip, Holdings, Orders, Strategy contribution, Risk exposures, Post-trade review
    expect(labels(html)).toBeGreaterThanOrEqual(6)
    expect(html).toContain('Mock book · simulated venue')
    for (const t of MOCK_TICKERS) expect(html).toContain(`>${t}<`)
    expect(html).not.toContain('>VST<')
    expect(html).not.toContain('>CEG<')
    expect(html).not.toContain('Portfolio empty')
    expect(html).toContain('MK-1001')
    expect(labelKinds(html).every((k) => k === 'quiet')).toBe(true) // a quiet note per panel, never a chip
    const nav = mockBook().account.nav
    expect(html).toContain(`$${(nav / 1e6).toFixed(3)}M`)
  })
  it('toggle off, signed out: the design account comes back and the label goes', () => {
    setMockPref(false)
    const html = render(<Trading />)
    expect(html).toContain('aria-checked="false"')
    expect(labels(html)).toBe(0)
    expect(html).toContain('>VST<')
    expect(html).not.toContain('Mock book · simulated venue')
    expect(html).toContain('Paper account <span class="num">FX-0042</span>')
  })
  it('signed in with a non-empty paper account: the real book wins, the toggle defaults off', () => {
    held()
    const html = render(<Trading />)
    expect(html).toContain('aria-checked="false"')
    expect(labels(html)).toBe(0)
    expect(html).toContain('founder@example.com')
    expect(html).toContain('A1B2C3D4')
    expect(html).not.toContain('MK-1001')
    expect(html).toContain('$1.001M')
  })
  it('signed in with an empty paper account: the mock book shows, the account badge stays', () => {
    setAccountState({ status: 'live', email: 'founder@example.com', ordersError: null, generatedAt: '2026-11-08T09:00:00Z', fresh: { freshness: 'historical', label: 'Paper account · cash only · 08 Nov 09:00' }, account: { nav: 1_000_000, cash: 1_000_000, buyingPower: 1_500_000, realised: 0, unrealised: 0, maxDrawdownPct: 0, openOrders: 0, positions: [] }, orders: [] })
    const html = render(<Trading />)
    expect(html).toContain('aria-checked="true"')
    expect(labels(html)).toBeGreaterThanOrEqual(6)
    expect(html).toContain('Paper account · cash only')
    expect(html).toContain('MK-1001')
  })
})

describe('Fund with the mock book', () => {
  it('signed out: the label on the header, the KPI row, Positions, Exposure by cluster, Risk and Recent orders; no extra chip', () => {
    const html = render(<Fund />)
    expect(html).toContain('Show mock book')
    expect(labels(html)).toBeGreaterThanOrEqual(6)
    expect(labelKinds(html).every((k) => k === 'quiet')).toBe(true)
    for (const t of MOCK_TICKERS) expect(html).toContain(`>${t}<`)
    expect(html).not.toContain('Liquidate all') // no simulated action on mock rows
    expect(html).toContain('not in the mock book')
    expect(html).toContain('paper (mock)')
    expect(html).toContain('Sized to target weight') // a mock order's note in Recent orders (the list shows no ids)
    expect(html).toContain('Strategies') // the catalogue panel, saying it has not answered in a static render
    expect(html).toContain('has not answered yet')
  })
  it('signed in with a non-empty paper account: the real book wins', () => {
    held()
    const html = render(<Fund />)
    expect(labels(html)).toBe(0)
    expect(html).toContain('07 Nov 15:01') // the account's own order in Recent orders
    expect(html).toContain('founder@example.com')
    expect(html).not.toContain('Sized to target weight')
    expect(html).toContain('paper (account)')
  })
})
