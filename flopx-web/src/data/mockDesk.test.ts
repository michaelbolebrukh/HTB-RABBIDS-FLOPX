// The mock book (N-MOCK): deterministic, sized to the $1.000M paper NAV, consistent with the live
// store's marks, and shown by default only where the paper account would otherwise read empty.
import { afterEach, describe, expect, it } from 'vitest'
import { __resetW2, applyCloses, W2_LIVE } from '@/live/adapt-w2'
import seriesNvda from '@/live/__fixtures__/w2/series_NVDA.json'
import type { SeriesResponseW2 } from '@/live/adapt-w2'
import { lastPx } from './fund'
import { secByTicker } from './finance'
import { fmtDM, weekdaysAgo } from '@/lib/designDates'
import { __resetMockDesk, MOCK_NAV0, MOCK_TICKERS, mockBook, mockDefault, mockDeskLabel, mockGroupOf, mockPx, setMockPref, showMockBook } from './mockDesk'

const S = seriesNvda as unknown as SeriesResponseW2
const fixed = (t: string) => ({ NVDA: 180, CRWV: 95, NBIS: 90, IREN: 44, APLD: 22, CORZ: 15, VRT: 130, SMCI: 40 })[t] ?? 0

afterEach(() => { __resetMockDesk(); __resetW2() })

describe('the mock book', () => {
  it('is deterministic: the same figures on every call, and again after the cache is dropped', () => {
    const a = mockBook()
    const b = mockBook()
    expect(b).toBe(a) // memoised on the marks and the anchor
    __resetMockDesk()
    const c = mockBook()
    expect(c).not.toBe(a)
    expect(c).toEqual(a)
    expect(JSON.stringify(mockBook(fixed))).toBe(JSON.stringify(mockBook(fixed)))
  })

  it('holds the eight covered tickers, one position each, with one short', () => {
    const book = mockBook(fixed)
    expect(book.account.positions.map((p) => p.ticker)).toEqual([...MOCK_TICKERS])
    expect(book.account.positions.filter((p) => p.qty < 0).map((p) => p.ticker)).toEqual(['SMCI'])
    expect(new Set(book.account.positions.map((p) => p.ticker)).size).toBe(8)
  })

  it('is sized to the $1.000M paper NAV: gross 55 to 95% of NAV, no leg over 15%, NAV within 3% of the opening equity, and the ledger balances', () => {
    const book = mockBook(fixed)
    const { nav, cash, positions, realised, unrealised } = book.account
    const gross = positions.reduce((s, p) => s + Math.abs(p.mv!), 0)
    expect(gross / nav).toBeGreaterThan(0.55)
    expect(gross / nav).toBeLessThan(0.95)
    for (const p of positions) expect(Math.abs(p.mv!) / nav).toBeLessThanOrEqual(0.15)
    expect(Math.abs(nav - MOCK_NAV0) / MOCK_NAV0).toBeLessThan(0.03)
    // cash + marks = opening equity + realised + unrealised, to the cent
    expect(cash + positions.reduce((s, p) => s + p.mv!, 0)).toBeCloseTo(MOCK_NAV0 + realised + unrealised, 1)
    expect(unrealised).toBeCloseTo(positions.reduce((s, p) => s + p.pnl!, 0), 1)
    expect(realised).toBeGreaterThan(0)
    expect(book.account.buyingPower).toBeCloseTo(cash + nav * 0.5, 1)
  })

  it('marks every position at the live store\'s price for the five design tickers and at the authored close for the other three', () => {
    const book = mockBook()
    for (const p of book.account.positions) {
      expect(p.last).toBe(mockPx(p.ticker))
      if (secByTicker(p.ticker)) expect(p.last).toBe(lastPx(p.ticker))
      expect(p.mv).toBeCloseTo(p.qty * p.last!, 6)
      expect(p.pnl).toBeCloseTo(p.qty * (p.last! - p.avg), 6)
      expect(p.fresh.freshness).toBe('illustrative')
    }
    expect(mockPx('CORZ')).toBe(14.9)
    expect(mockPx('VRT')).toBe(131.3)
    expect(mockPx('SMCI')).toBe(38.55)
    expect(book.observedMarks).toBe(0)
  })

  it('follows an archived close placed on NVDA (the mark, the quantity and the position badge move; the book is rebuilt)', () => {
    const nvdaSec = secByTicker('NVDA')!
    const designPx = [...nvdaSec.px]
    const before = mockBook()
    const archive = S.points.map((p) => ({ ...p, provenance: { ...p.provenance, kind: 'archived' as const, source: 'alpaca-data' } }))
    expect(applyCloses('NVDA', archive)).toBeGreaterThan(0)
    const after = mockBook()
    expect(after).not.toBe(before)
    const nvda = after.account.positions.find((p) => p.ticker === 'NVDA')!
    expect(nvda.last).toBe(lastPx('NVDA'))
    expect(nvda.last).not.toBe(before.account.positions.find((p) => p.ticker === 'NVDA')!.last)
    expect(nvda.fresh).toMatchObject({ freshness: 'historical', label: 'mark alpaca-data' })
    expect(after.observedMarks).toBe(1)
    // the other three take the backend's latest close once /flopx/companies has landed
    W2_LIVE.companies.CORZ = { ticker: 'CORZ', close: { value: 16.2, t: '2026-11-06T21:00:00Z', fresh: { freshness: 'live' } }, observedWeeks: 0, placed: null, realWeeks: [], revGrowth: null, quarterly: null }
    __resetMockDesk()
    expect(mockPx('CORZ')).toBe(16.2)
    expect(mockBook().account.positions.find((p) => p.ticker === 'CORZ')!.fresh.freshness).toBe('live')
    // restore the design closes (applyCloses rewrites SECURITIES[*].px in place) and forget what landed: the design book comes back, figure for figure
    designPx.forEach((v, i) => (nvdaSec.px[i] = v))
    __resetW2()
    __resetMockDesk()
    expect(mockBook()).toEqual(before)
  })

  it('carries a dozen orders over the last five sessions, newest first, with fills that reconcile to the cost bases', () => {
    const book = mockBook(fixed)
    const { orders, account } = book
    expect(orders).toHaveLength(12)
    const sessions = Array.from({ length: 5 }, (_, k) => fmtDM(weekdaysAgo(k)))
    for (const o of orders) expect(sessions).toContain(o.time.slice(0, 6))
    expect(new Set(orders.map((o) => o.time.slice(0, 6))).size).toBe(5)
    expect(new Set(orders.map((o) => o.id)).size).toBe(12)
    // newest first, by session then clock
    const rank = (t: string) => sessions.indexOf(t.slice(0, 6)) * -10_000 + +t.slice(-5).replace(':', '')
    for (let i = 1; i < orders.length; i++) expect(rank(orders[i - 1].time)).toBeGreaterThanOrEqual(rank(orders[i].time))
    expect(orders.filter((o) => o.status === 'Filled')).toHaveLength(10)
    expect(orders.filter((o) => o.status === 'Rejected')).toHaveLength(1)
    expect(orders.filter((o) => o.status === 'Cancelled')).toHaveLength(1)
    expect(orders.filter((o) => o.status === 'Pending' || o.status === 'Queued')).toHaveLength(0) // no dead cancel button on a mock row
    expect(account.openOrders).toBe(0)
    for (const o of orders.filter((o) => o.status === 'Filled')) expect(o.fillPx).toBeGreaterThan(0)
    // every position's opening fills average to its cost basis
    for (const p of account.positions) {
      const opens = orders.filter((o) => o.ticker === p.ticker && o.status === 'Filled' && (o.side === 'Buy' || o.side === 'Short'))
      const q = opens.reduce((s, o) => s + o.qty, 0)
      const vwap = opens.reduce((s, o) => s + o.qty * o.fillPx!, 0) / q
      expect(vwap).toBeCloseTo(p.avg, 1)
      expect(q).toBeGreaterThanOrEqual(Math.abs(p.qty))
    }
    const sell = orders.find((o) => o.side === 'Sell')!
    const crwv = account.positions.find((p) => p.ticker === 'CRWV')!
    expect(sell.ticker).toBe('CRWV')
    expect(account.realised).toBeCloseTo(sell.qty * (sell.fillPx! - crwv.avg), 1)
    expect(orders.every((o) => o.origin === 'Model' || o.origin === 'User')).toBe(true)
  })

  it('derives exposure and P&L by cluster from the positions, and the owner split', () => {
    const book = mockBook(fixed)
    const nav = book.account.nav
    const byG = new Map<string, number>()
    book.account.positions.forEach((p) => byG.set(mockGroupOf(p.ticker), (byG.get(mockGroupOf(p.ticker)) ?? 0) + p.mv!))
    for (const row of book.exposureByCluster) expect(row.value).toBeCloseTo((byG.get(row.label)! / nav) * 100, 1)
    expect(book.exposureByCluster.map((r) => r.label).sort()).toEqual(['Chips', 'DC Operator', 'Neocloud'])
    expect(book.exposureByCluster.find((r) => r.label === 'Other')).toBeUndefined()
    expect(book.pnlByCluster.reduce((s, r) => s + r.value, 0)).toBeCloseTo(book.account.unrealised, 0)
    expect(book.pnlByOwner.map((r) => r.label)).toEqual(['Model · CPX-1', 'User-initiated'])
    expect(book.pnlByOwner.reduce((s, r) => s + r.value, 0)).toBeCloseTo(book.account.unrealised, 0)
    expect(mockGroupOf('VRT')).toBe('DC Operator')
    expect(mockGroupOf('NVDA')).toBe('Chips')
  })

  it('is labelled as mock data, and its as-of is a completed trading close before now', () => {
    const book = mockBook(fixed)
    expect(mockDeskLabel).toBe('Mock data: illustrative positions and orders, not the paper account')
    expect(book.fresh).toEqual({ freshness: 'illustrative', label: mockDeskLabel })
    expect(Date.parse(book.generatedAt)).toBeLessThan(Date.now())
    expect([0, 6]).not.toContain(new Date(book.generatedAt).getUTCDay())
  })
})

describe('the toggle default', () => {
  const empty = { status: 'live' as const, account: { positions: [] }, orders: [] }
  const held = { status: 'live' as const, account: { positions: [{}] }, orders: [] }
  const ordered = { status: 'live' as const, account: { positions: [] }, orders: [{}] }

  it('is on signed out, while the account is read or failed, and for an empty paper account', () => {
    expect(mockDefault({ status: 'anonymous' })).toBe(true)
    expect(mockDefault({ status: 'pending' })).toBe(true)
    expect(mockDefault({ status: 'failed' })).toBe(true)
    expect(mockDefault(empty)).toBe(true)
  })
  it('is off once the paper account holds a position or an order: the real book wins', () => {
    expect(mockDefault(held)).toBe(false)
    expect(mockDefault(ordered)).toBe(false)
    expect(showMockBook(held)).toBe(false)
    expect(showMockBook(empty)).toBe(true)
  })
  it('the reader\'s choice overrides the default either way, and resets to it', () => {
    setMockPref(false)
    expect(showMockBook(empty)).toBe(false)
    setMockPref(true)
    expect(showMockBook(held)).toBe(true)
    setMockPref(null)
    expect(showMockBook(held)).toBe(false)
  })
})
