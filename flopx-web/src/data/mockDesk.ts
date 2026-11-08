// The mock book: illustrative positions, orders and P&L for the Trading desk and the Systematic
// Fund, shown when the signed-in reader's paper account is empty (the demo account holds nothing,
// so both pages read as dead) or when nobody is signed in. It is never the paper account and every
// panel drawing from it says so with `mockDeskLabel` as a quiet note.
//
// Shape: the same bundle the pages already read for a signed-in account (src/live/account.ts:
// LiveAccount positions, the design's Order rows, a FreshTag), so the pages swap one object and
// render through their live-account path, which carries no simulated actions on the rows.
//
// Consistency with the live store: the eight tickers are the backend's covered set
// (/flopx/companies). The five the design also tracks are marked at `lastPx`, which is the
// design's close or the archived close once src/live/adapt-w2.ts has placed one for a signed-in
// reader; the other three are marked at the backend's latest close when it has landed, else at an
// authored fallback. Quantities are sized to the $1.000M paper NAV (the real account's opening
// equity) off those marks, cost bases sit a fixed, small drift from them, and the dozen orders are
// the fills that would have built the book over the last five sessions, stamped by weekday back
// from the axis anchor (src/lib/designDates.ts). Nothing here uses a clock or a random source at
// call time: the book is a pure function of the closes and the anchor, so it reads the same on
// every load.
import type { LiveAccount, LivePosition } from '@/live/account'
import { W2_LIVE } from '@/live/adapt-w2'
import type { FreshTag } from '@/live/provenance'
import type { Order } from '@/lib/store'
import { lastPx } from '@/data/fund'
import { secByTicker, type Group } from '@/data/finance'
import { fmtStampAt, lastTradingClose, weekdaysAgo } from '@/lib/designDates'

/** The one sentence every mock panel carries, as a quiet note (never a chip per row). */
export const mockDeskLabel = 'Mock data: illustrative positions and orders, not the paper account'

/** The paper account's opening equity, which the mock book is sized to. */
export const MOCK_NAV0 = 1_000_000

/** The backend's covered equities (/flopx/companies), in weight order. */
export const MOCK_TICKERS = ['NVDA', 'CRWV', 'NBIS', 'IREN', 'APLD', 'CORZ', 'VRT', 'SMCI'] as const
export type MockTicker = (typeof MOCK_TICKERS)[number]

/** Names, clusters and fallback closes for the three covered tickers the design does not track (closes: the backend's authored sample, 06 Nov 2026). */
const EXTRA: Record<string, { name: string; group: Group; px: number }> = {
  CORZ: { name: 'Core Scientific', group: 'Neocloud', px: 14.9 },
  VRT: { name: 'Vertiv', group: 'DC Operator', px: 131.3 },
  SMCI: { name: 'Super Micro Computer', group: 'Chips', px: 38.55 },
}

/**
 * Target weight (% of NAV, negative short), the cost drift from the current mark (positive: the
 * book is in profit), the owner and the FLOPX signal each position is attributed to.
 */
interface Leg { t: MockTicker; w: number; drift: number; owner: 'Model' | 'User'; signal?: string }
const LEGS: Leg[] = [
  { t: 'NVDA', w: 0.14, drift: 0.021, owner: 'Model', signal: 'sig-h100' },
  { t: 'CRWV', w: 0.11, drift: 0.034, owner: 'Model', signal: 'sig-neo' },
  { t: 'NBIS', w: 0.08, drift: -0.018, owner: 'Model', signal: 'sig-neo' },
  { t: 'IREN', w: 0.06, drift: 0.027, owner: 'Model', signal: 'sig-ercot' },
  { t: 'APLD', w: 0.05, drift: -0.031, owner: 'Model', signal: 'sig-ercot' },
  { t: 'CORZ', w: 0.07, drift: 0.012, owner: 'Model', signal: 'sig-neo' },
  { t: 'VRT', w: 0.09, drift: 0.009, owner: 'User' },
  { t: 'SMCI', w: -0.04, drift: 0.015, owner: 'Model', signal: 'sig-h100' },
]

export type PriceOf = (t: string) => number

/** Cluster of a mock ticker: the design's group for its five, the authored one for the other three. */
export function mockGroupOf(t: string): Group | 'Other' {
  return secByTicker(t)?.group ?? EXTRA[t]?.group ?? 'Other'
}

/** Company name for a mock ticker; empty for a ticker neither the design nor the mock knows. */
export function mockNameOf(t: string): string {
  return secByTicker(t)?.name ?? EXTRA[t]?.name ?? ''
}

/**
 * The mark for a mock ticker: `lastPx` for a design-tracked ticker (the archived close when one is
 * placed), the backend's latest close for the others when it has landed, else the authored fallback.
 */
export function mockPx(t: string): number {
  const design = lastPx(t)
  if (design > 0) return design
  const live = W2_LIVE.companies[t]?.close?.value
  if (live != null && Number.isFinite(live) && live > 0) return live
  return EXTRA[t]?.px ?? 0
}

/** Provenance of the mark `mockPx` returns, in the design's vocabulary. */
export function mockMarkFresh(t: string): FreshTag {
  const c = W2_LIVE.companies[t]
  if (secByTicker(t)) {
    const p = c?.placed
    return p && p.latestWeek && p.fresh.freshness !== 'illustrative' ? { ...p.fresh, label: `mark ${p.source}` } : { freshness: 'illustrative' }
  }
  return c?.close && c.close.fresh.freshness !== 'illustrative' ? c.close.fresh : { freshness: 'illustrative' }
}

export interface MockOrder extends Order {
  /** The signal the fill is attributed to; absent on user-initiated orders. */
  signal?: string
}

export interface MockBook {
  /** The live-account shape the pages read (src/live/account.ts). */
  account: LiveAccount
  orders: MockOrder[]
  /** Illustrative, labelled with `mockDeskLabel`. */
  fresh: FreshTag
  /** ISO instant of the last completed trading close before the anchor: the book's "as of". */
  generatedAt: string
  /** Realised P&L from the closed legs in `orders`, USD. */
  realised: number
  /** Net exposure by cluster, % of NAV, largest first. */
  exposureByCluster: { label: string; value: number }[]
  /** Unrealised P&L by cluster, USD, largest first. */
  pnlByCluster: { label: string; value: number }[]
  /** Unrealised P&L by owner, USD: the model's book and the user-initiated leg. */
  pnlByOwner: { label: string; value: number }[]
  /** Which positions are marked at an observed close (archived or live) rather than a design or sample one. */
  observedMarks: number
}

const round2 = (v: number) => +v.toFixed(2)

/** Session stamps: `k` weekdays back from the anchor at a fixed clock time inside the NYSE session (14:30 to 21:00 UTC). */
const stamp = (k: number, hhmm: string) => fmtStampAt(weekdaysAgo(k), hhmm)

let cache: { key: string; book: MockBook } | null = null

/**
 * Build the mock book off the given marks (default: `mockPx`). Pure apart from the marks and the
 * axis anchor, and memoised on both so repeated renders get the same object.
 */
export function mockBook(priceOf: PriceOf = mockPx): MockBook {
  const marks = LEGS.map((l) => priceOf(l.t))
  const freshes: FreshTag[] = LEGS.map((l) => (priceOf === mockPx ? mockMarkFresh(l.t) : { freshness: 'illustrative' }))
  const key = `${marks.join(',')}|${weekdaysAgo(0).toISOString()}|${freshes.map((f) => f.freshness + (f.label ?? '')).join(',')}|${priceOf === mockPx ? 'marks' : 'custom'}`
  if (cache && cache.key === key) return cache.book

  const positions: LivePosition[] = []
  const orders: MockOrder[] = []
  let cost = 0
  let realised = 0
  let seq = 1001
  const id = () => `MK-${seq++}`

  LEGS.forEach((l, i) => {
    const px = marks[i]
    if (!(px > 0)) return
    const avg = round2(px * (1 - l.drift))
    const sign = l.w < 0 ? -1 : 1
    const qty = sign * Math.max(1, Math.round((Math.abs(l.w) * MOCK_NAV0) / avg))
    const mv = qty * px
    const pnl = qty * (px - avg)
    positions.push({ ticker: l.t, qty, avg, last: px, mv, pnl, weight: null, fresh: freshes[i] })
    cost += qty * avg
  })

  // The fills that built the book, newest first: eight opening legs over sessions 4 to 1, a second
  // NVDA leg and a partial CRWV take-profit today, one rejected short and one expired limit.
  const pos = (t: string) => positions.find((p) => p.ticker === t)
  const note = (l: Leg) => (l.owner === 'User' ? undefined : 'Sized to target weight')
  const open = (l: Leg, k: number, hhmm: string, qtyOverride?: number, pxOverride?: number): void => {
    const p = pos(l.t)
    if (!p) return
    const q = qtyOverride ?? Math.abs(p.qty)
    orders.push({ id: id(), time: stamp(k, hhmm), ticker: l.t, side: p.qty < 0 ? 'Short' : 'Buy', qty: q, type: 'Market', status: 'Filled', origin: l.owner, fillPx: pxOverride ?? p.avg, note: note(l), signal: l.signal })
  }
  // Session 4: the two largest legs. NVDA in two lots whose VWAP is the position's cost basis.
  const nvda = pos('NVDA')
  if (nvda) {
    const q1 = Math.round(Math.abs(nvda.qty) * 0.6)
    const p1 = round2(nvda.avg * 0.988)
    const q2 = Math.abs(nvda.qty) - q1
    const p2 = q2 > 0 ? round2((nvda.avg * Math.abs(nvda.qty) - p1 * q1) / q2) : nvda.avg
    open(LEGS[0], 4, '14:41', q1, p1)
    open(LEGS[0], 1, '15:12', q2, p2)
  }
  // CRWV: bought a larger lot in session 4, sold a quarter today above cost (the realised leg).
  const crwv = pos('CRWV')
  if (crwv) {
    const sold = Math.round(Math.abs(crwv.qty) * 0.25)
    const sellPx = round2(crwv.avg * 1.028)
    open(LEGS[1], 4, '16:05', Math.abs(crwv.qty) + sold)
    realised += sold * (sellPx - crwv.avg)
    orders.push({ id: id(), time: stamp(0, '15:47'), ticker: 'CRWV', side: 'Sell', qty: sold, type: 'Market', status: 'Filled', origin: 'Model', fillPx: sellPx, note: 'Trimmed to target weight', signal: 'sig-neo' })
  }
  open(LEGS[2], 3, '14:36')
  open(LEGS[5], 3, '17:20')
  open(LEGS[6], 3, '19:02')
  open(LEGS[3], 2, '15:28')
  open(LEGS[4], 2, '18:11')
  open(LEGS[7], 1, '16:33')
  // A short the venue could not locate, and a user limit that expired unfilled.
  const iren = pos('IREN')
  if (iren) orders.push({ id: id(), time: stamp(1, '19:48'), ticker: 'IREN', side: 'Short', qty: Math.round(Math.abs(iren.qty) * 0.5), type: 'Market', status: 'Rejected', origin: 'User', reason: 'No borrow available (simulated locate failed)' })
  const vrt = pos('VRT')
  if (vrt) orders.push({ id: id(), time: stamp(0, '14:33'), ticker: 'VRT', side: 'Buy', qty: Math.round(Math.abs(vrt.qty) * 0.3), type: 'Limit', limit: round2(vrt.last! * 0.97), status: 'Cancelled', origin: 'User', reason: 'Expired (day order)' })
  orders.sort((a, b) => sessionRank(b) - sessionRank(a))

  realised = round2(realised)
  const cash = round2(MOCK_NAV0 - cost + realised)
  const unrealised = round2(positions.reduce((s, p) => s + (p.pnl ?? 0), 0))
  const nav = round2(cash + positions.reduce((s, p) => s + (p.mv ?? 0), 0))

  const byGroup = (f: (p: LivePosition) => number) => {
    const m = new Map<string, number>()
    positions.forEach((p) => { const g = mockGroupOf(p.ticker); m.set(g, (m.get(g) ?? 0) + f(p)) })
    return [...m.entries()].map(([label, value]) => ({ label, value })).sort((a, b) => Math.abs(b.value) - Math.abs(a.value))
  }
  const owner = (o: Leg['owner']) => positions.filter((p) => LEGS.find((l) => l.t === p.ticker)!.owner === o).reduce((s, p) => s + (p.pnl ?? 0), 0)

  const book: MockBook = {
    account: {
      nav,
      cash,
      buyingPower: round2(cash + nav * 0.5),
      realised,
      unrealised,
      maxDrawdownPct: 2.4,
      openOrders: orders.filter((o) => o.status === 'Pending' || o.status === 'Queued').length,
      positions,
    },
    orders,
    fresh: { freshness: 'illustrative', label: mockDeskLabel },
    generatedAt: lastTradingClose().toISOString(),
    realised,
    exposureByCluster: byGroup((p) => (nav ? ((p.mv ?? 0) / nav) * 100 : 0)).map((r) => ({ ...r, value: +r.value.toFixed(2) })),
    pnlByCluster: byGroup((p) => p.pnl ?? 0).map((r) => ({ ...r, value: round2(r.value) })),
    pnlByOwner: [
      { label: 'Model · CPX-1', value: round2(owner('Model')) },
      { label: 'User-initiated', value: round2(owner('User')) },
    ],
    observedMarks: positions.filter((p) => p.fresh.freshness !== 'illustrative').length,
  }
  cache = { key, book }
  return book
}

/** Newest first: the stamp's weekday rank, then its clock time (the stamps are "07 Nov 15:12"). */
function sessionRank(o: Order): number {
  for (let k = 0; k < 6; k++) {
    if (o.time.startsWith(fmtStampAt(weekdaysAgo(k), '').trimEnd())) return (6 - k) * 10_000 + +o.time.slice(-5).replace(':', '')
  }
  return 0
}

// ---------- The toggle's preference and the default rule ----------

/** The subset of the account state the default rule reads (src/live/account.ts: AccountState). */
export type AccountLike =
  | { status: 'anonymous' | 'pending' | 'failed' }
  | { status: 'live'; account: { positions: readonly unknown[] }; orders: readonly unknown[] }

/** True when the signed-in account holds something: then the real book wins and the toggle defaults off. */
export function paperAccountNonEmpty(acct: AccountLike): boolean {
  return acct.status === 'live' && (acct.account.positions.length > 0 || acct.orders.length > 0)
}

/**
 * The toggle's default: on while nobody is signed in, while the account is still being read or
 * failed to read, and for a signed-in account that holds no positions and no orders; off the moment
 * the paper account holds anything.
 */
export function mockDefault(acct: AccountLike): boolean {
  return !paperAccountNonEmpty(acct)
}

/** The toggle's state: null follows `mockDefault`, a boolean is the reader's own choice. Shared by both pages, kept for the session. */
let pref: boolean | null = null
const listeners = new Set<() => void>()
export function getMockPref(): boolean | null {
  return pref
}
export function setMockPref(v: boolean | null): void {
  pref = v
  listeners.forEach((fn) => fn())
}
export function subscribeMockPref(fn: () => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

/** Whether the mock book is shown, from the account state and the reader's choice. */
export function showMockBook(acct: AccountLike, choice: boolean | null = pref): boolean {
  return choice ?? mockDefault(acct)
}

/** Test helper. */
export function __resetMockDesk(): void {
  pref = null
  listeners.clear()
  cache = null
}
