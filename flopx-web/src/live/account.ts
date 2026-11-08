// Live module 'account': the signed-in reader's paper account (NAV, cash, buying power, positions,
// orders) from /flopx/paper/account and /flopx/paper/orders. Both reads need a token; without one
// the module records 'anonymous' and the pages keep the design's illustrative account with its
// badge. With one, the pages (Trading, Fund) read ACCOUNT_LIVE instead of the design's simulated
// store for those figures, badged with the backend's freshness. A failed read while signed in is
// recorded as 'failed' with the reason, and the pages say so rather than showing the design's
// numbers as the account.
import type { Getter, LiveModule } from './boot'
import type { Order } from '@/lib/store'
import { fmtObserved, freshOf, type FreshTag } from './provenance'
import { getTokens } from './token'
import type { FreshnessBlock, Provenance } from './types'

// ---------- Wire types (only the fields read here) ----------
export interface PaperPositionWire {
  ticker: string
  quantity: number
  avgCost: number
  lastPrice: number | null
  marketValueUsd: number | null
  unrealisedPnlUsd: number | null
  weight: number | null
  priceProvenance: Provenance | null
}
export interface PaperAccountWire {
  paper: true
  cashUsd: number
  equityUsd: number
  buyingPowerUsd: number
  realisedPnlUsd: number
  unrealisedPnlUsd: number
  maxDrawdownPct: number
  positions: PaperPositionWire[]
  exposureByTheme: Record<string, number>
  openOrders: number
  freshness: FreshnessBlock
}
export interface PaperOrderWire {
  id: string
  ticker: string
  side: 'buy' | 'sell' | string
  orderType: 'market' | 'limit' | string
  quantity: number
  notionalUsd: number | null
  limitPrice: number | null
  status: 'pending' | 'accepted' | 'filled' | 'rejected' | 'cancelled' | string
  origin: string
  reason: string | null
  fillPrice: number | null
  fillProvenance: Provenance | null
  filledAt: string | null
  createdAt: string | null
}
export interface PaperOrdersWire { orders: PaperOrderWire[]; freshness: FreshnessBlock }

// ---------- What the pages read ----------
export interface LivePosition {
  ticker: string
  qty: number
  avg: number
  last: number | null
  mv: number | null
  pnl: number | null
  /** Fraction of equity, as the backend reports it (null when it does not). */
  weight: number | null
  fresh: FreshTag
}
export interface LiveAccount {
  nav: number
  cash: number
  buyingPower: number
  realised: number
  unrealised: number
  maxDrawdownPct: number
  openOrders: number
  positions: LivePosition[]
}
export type AccountState =
  | { status: 'anonymous' }
  | { status: 'pending'; email: string }
  | { status: 'live'; email: string; account: LiveAccount; orders: Order[]; ordersError: string | null; generatedAt: string; fresh: FreshTag }
  | { status: 'failed'; email: string; error: string }

export const ACCOUNT_LIVE: { state: AccountState } = { state: { status: 'anonymous' } }

/** The account's own badge, from the marks on its positions: any sample mark makes the value illustrative; cash only is exact. */
export function accountFresh(positions: PaperPositionWire[], generatedAt: string): FreshTag {
  const tags = positions.map((p) => freshOf(p.priceProvenance))
  const when = fmtObserved(generatedAt)
  if (!positions.length) return { freshness: 'historical', label: `Paper account · cash only · ${when}` }
  if (tags.some((t) => t.freshness === 'illustrative')) return { freshness: 'illustrative', label: `Paper account · marks are sample prices · ${when}` }
  if (tags.every((t) => t.freshness === 'live')) return { freshness: 'live', label: `Paper account · ${when}` }
  return { freshness: 'historical', label: `Paper account · archived marks · ${when}` }
}

export function adaptAccount(p: PaperAccountWire): LiveAccount {
  return {
    nav: p.equityUsd,
    cash: p.cashUsd,
    buyingPower: p.buyingPowerUsd,
    realised: p.realisedPnlUsd,
    unrealised: p.unrealisedPnlUsd,
    maxDrawdownPct: p.maxDrawdownPct,
    openOrders: p.openOrders,
    positions: p.positions.map((x) => ({
      ticker: x.ticker,
      qty: x.quantity,
      avg: x.avgCost,
      last: x.lastPrice,
      mv: x.marketValueUsd ?? (x.lastPrice != null ? x.quantity * x.lastPrice : null),
      pnl: x.unrealisedPnlUsd,
      weight: x.weight,
      fresh: freshOf(x.priceProvenance),
    })),
  }
}

const STATUS: Record<string, Order['status']> = { pending: 'Pending', accepted: 'Pending', filled: 'Filled', rejected: 'Rejected', cancelled: 'Cancelled' }

/** The backend's orders in the design's order shape, newest first, so the design's list renders them unchanged. */
export function adaptOrders(rows: PaperOrderWire[]): Order[] {
  return [...rows]
    .sort((a, b) => Date.parse(b.createdAt ?? '') - Date.parse(a.createdAt ?? ''))
    .map((o) => ({
      id: o.id.slice(0, 8).toUpperCase(),
      time: fmtObserved(o.createdAt),
      ticker: o.ticker,
      side: o.side === 'sell' ? 'Sell' : 'Buy',
      qty: o.quantity,
      type: o.orderType === 'limit' ? 'Limit' : 'Market',
      limit: o.limitPrice ?? undefined,
      status: STATUS[o.status] ?? 'Pending',
      origin: o.origin === 'manual' ? 'User' : 'Model',
      fillPx: o.fillPrice ?? undefined,
      reason: o.reason ?? undefined,
      note: o.origin === 'manual' ? undefined : o.origin,
    }))
}

export function setAccountState(s: AccountState): void {
  ACCOUNT_LIVE.state = s
}

/** Test helper. */
export function __resetAccount(): void {
  ACCOUNT_LIVE.state = { status: 'anonymous' }
}

export const accountModule: LiveModule = {
  id: 'account',
  async load(get: Getter, apply: () => void) {
    const t = getTokens()
    if (!t) {
      setAccountState({ status: 'anonymous' })
      apply()
      throw new Error('sign-in required')
    }
    setAccountState({ status: 'pending', email: t.email })
    apply()
    const [acc, ord] = await Promise.all([get<PaperAccountWire>('/flopx/paper/account'), get<PaperOrdersWire>('/flopx/paper/orders')])
    if (!acc.ok) {
      // On a 401 the client has already dropped the token (client.ts), so the chip reads signed out; the page says why.
      setAccountState({ status: 'failed', email: t.email, error: acc.status === 401 ? 'the backend rejected the token (401), so you are signed out; sign in again' : acc.error })
      apply()
      return false
    }
    setAccountState({
      status: 'live',
      email: t.email,
      account: adaptAccount(acc.data),
      orders: ord.ok ? adaptOrders(ord.data.orders) : [],
      ordersError: ord.ok ? null : ord.error,
      generatedAt: acc.data.freshness.generatedAt,
      fresh: accountFresh(acc.data.positions, acc.data.freshness.generatedAt),
    })
    apply()
    return true
  },
}
