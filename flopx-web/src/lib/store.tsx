import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import type { EntityKind } from '@/data/core'
import { NOW_IDX } from '@/data/core'
import { INITIAL_CASH, INITIAL_POSITIONS, INITIAL_PROPOSALS, lastPx, navOf, type HorizonId, type Position, type Proposal } from '@/data/fund'
import { useLiveVersion } from '@/live/store'
import { fmtObserved } from '@/live/provenance'
import { fmtStampAt, weekdaysAgo } from '@/lib/designDates'
import { nyseRegularOpen, nyseStateLabel } from '@/lib/marketHours'
import { parse as parseTour, serialise as serialiseTour, STORAGE_KEY as TOUR_KEY, type TourState } from '@/tour/model'
import { TOUR_LENGTHS } from '@/tour/tours'

export type PageId = 'overview' | 'xray' | 'fund' | 'markets' | 'energy' | 'securities' | 'news' | 'research' | 'trading' | 'procurement' | 'handoff' | 'intelligence' | 'agents'

export interface EntityRef {
  kind: EntityKind
  id: string
  label: string
}

export type OrderStatus = 'Pending' | 'Filled' | 'Rejected' | 'Cancelled' | 'Queued'
export interface Order {
  id: string
  time: string
  ticker: string
  side: 'Buy' | 'Sell' | 'Short' | 'Cover'
  qty: number
  type: 'Market' | 'Limit'
  limit?: number
  status: OrderStatus
  origin: 'Model' | 'User'
  fillPx?: number
  reason?: string
  proposalId?: string
  signal?: string
  note?: string
}

export type XLayer = 'pressure' | 'rental' | 'power' | 'pipeline' | 'effective' | 'exposure'
export interface Scenario {
  region: string
  slipMonths: number
  powerShock: number // % change in power price
}
export interface XrayState {
  layers: XLayer[]
  reveal: number // 0 raw → 100 inferred
  t: number
  region: string | null
  project: string | null
  compare: string[] // up to two region ids
  compareOpen: boolean
  scenario: Scenario | null
  tab: 'brief' | 'evidence' | 'pipeline' | 'exposure' | 'whatif'
  gpu: string
}
export interface FundState {
  tab: 'portfolio' | 'signals' | 'decisions' | 'evaluation'
  signal: string
  horizon: HorizonId
  record: string | null
}

interface Toast {
  id: number
  text: string
  tone: 'info' | 'ok' | 'warn' | 'bad'
}

// Real UTC time for new paper orders ("08 Nov 01:05"), the design's per-order format.
const nowStamp = () => fmtObserved(new Date().toISOString())

function useStoreValue() {
  // Live data version: bumps whenever a backend read lands (src/live/store.ts). Subscribing here
  // re-renders every useStore() consumer; pages add `live` to useMemo deps over design data.
  const live = useLiveVersion()
  const parse =(): { page: PageId; params: Record<string, string> } => {
    const h = window.location.hash.replace(/^#\/?/, '')
    const [p, q] = h.split('?')
    const params: Record<string, string> = {}
    new URLSearchParams(q ?? '').forEach((v, k) => (params[k] = v))
    return { page: ((p as PageId) || 'overview') as PageId, params }
  }
  const [route, setRoute] = useState(parse)
  const [inspector, setInspector] = useState<EntityRef | null>(null)
  const [trail, setTrail] = useState<EntityRef[]>([])
  const [assistant, setAssistant] = useState<{ open: boolean; seed?: string; nonce: number }>({ open: false, nonce: 0 })
  const [palette, setPalette] = useState(false)
  const [positions, setPositions] = useState<Position[]>(INITIAL_POSITIONS)
  const [cash, setCash] = useState(INITIAL_CASH)
  // The design's seed orders carried March 2026 stamps, before the paper account existed. They are
  // illustrative worked examples, so they keep their distance from "now" in weekdays (1, 2, 3 and 6
  // sessions back) and slide onto the real anchor (src/lib/designDates.ts).
  const [orders, setOrders] = useState<Order[]>(() => [
    { id: 'ORD-1041', time: fmtStampAt(weekdaysAgo(1), '15:58'), ticker: 'VST', side: 'Buy', qty: 600, type: 'Market', status: 'Filled', origin: 'Model', fillPx: +(lastPx('VST') * 0.985).toFixed(2), proposalId: 'd5', signal: 'sig-ercot', note: 'Approved by user' },
    { id: 'ORD-1040', time: fmtStampAt(weekdaysAgo(2), '19:31'), ticker: 'IREN', side: 'Short', qty: 4000, type: 'Market', status: 'Rejected', origin: 'User', reason: 'No borrow available (simulated locate failed)' },
    { id: 'ORD-1039', time: fmtStampAt(weekdaysAgo(3), '14:02'), ticker: 'MSFT', side: 'Buy', qty: 120, type: 'Limit', limit: +(lastPx('MSFT') * 0.97).toFixed(2), status: 'Cancelled', origin: 'User', reason: 'Expired (day order)' },
    { id: 'ORD-1037', time: fmtStampAt(weekdaysAgo(6), '14:45'), ticker: 'AMD', side: 'Sell', qty: 900, type: 'Market', status: 'Filled', origin: 'Model', fillPx: +(lastPx('AMD') * 1.04).toFixed(2), signal: 'sig-chips', note: 'Closed: no edge found → realised +$41,250' },
  ])
  const [proposals, setProposals] = useState<(Proposal & { state: 'open' | 'sent' | 'rejected'; modifiedQty?: number })[]>(INITIAL_PROPOSALS.map((p) => ({ ...p, state: 'open' })))
  // US equities state comes from the clock (NYSE regular hours, weekdays 14:30 to 21:00 UTC, no
  // holiday table; src/lib/marketHours.ts). The sidebar toggle is a demo override on top of it;
  // toggling back to the clock's own state clears the override.
  const [clockTick, setClockTick] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setClockTick(Date.now()), 30_000)
    return () => clearInterval(id)
  }, [])
  const clockOpen = nyseRegularOpen(new Date(clockTick))
  const [marketOverride, setMarketOverride] = useState<boolean | null>(null)
  const marketOpen = marketOverride ?? clockOpen
  const marketOverridden = marketOverride !== null && marketOverride !== clockOpen
  const marketLabel = nyseStateLabel(marketOpen, new Date(clockTick), marketOverridden)
  const [toasts, setToasts] = useState<Toast[]>([])
  const [xray, setXrayRaw] = useState<XrayState>({ layers: ['pressure', 'pipeline'], reveal: 100, t: NOW_IDX, region: null, project: null, compare: [], compareOpen: false, scenario: null, tab: 'brief', gpu: 'h100' })
  const [fund, setFundRaw] = useState<FundState>({ tab: 'portfolio', signal: 'sig-ercot', horizon: '1M', record: null })
  // The presenter tour (src/tour). Resumes after a reload mid-walkthrough from sessionStorage;
  // autoplay never resumes on its own.
  const [tour, setTour] = useState<TourState | null>(() => {
    try { return parseTour(window.sessionStorage.getItem(TOUR_KEY), TOUR_LENGTHS) } catch { return null }
  })
  useEffect(() => {
    try {
      if (tour) window.sessionStorage.setItem(TOUR_KEY, serialiseTour(tour))
      else window.sessionStorage.removeItem(TOUR_KEY)
    } catch { /* storage unavailable: the tour simply does not resume */ }
  }, [tour])
  const [ticket, setTicket] = useState<{ ticker: string; side?: Order['side']; qty?: number } | null>(null)
  const orderSeq = useRef(1042)
  const marketRef = useRef(marketOpen)
  marketRef.current = marketOpen

  useEffect(() => {
    const fn = () => setRoute(parse())
    window.addEventListener('hashchange', fn)
    return () => window.removeEventListener('hashchange', fn)
  }, [])

  const navigate = useCallback((page: PageId, params: Record<string, string> = {}) => {
    const q = new URLSearchParams(params).toString()
    const h = `#/${page}${q ? `?${q}` : ''}`
    if (window.location.hash !== h) window.history.pushState(null, '', h)
    document.getElementById('main-scroll')?.scrollTo({ top: 0 })
    setRoute({ page, params })
  }, [])

  const toast = useCallback((text: string, tone: Toast['tone'] = 'info') => {
    const id = Date.now() + Math.random()
    setToasts((t) => [...t, { id, text, tone }])
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 4200)
  }, [])

  const inspect = useCallback((ref: EntityRef) => {
    setInspector(ref)
    setTrail((t) => [...t.filter((x) => !(x.kind === ref.kind && x.id === ref.id)), ref].slice(-6))
  }, [])

  const setXray = useCallback((p: Partial<XrayState> | ((s: XrayState) => Partial<XrayState>)) => setXrayRaw((s) => ({ ...s, ...(typeof p === 'function' ? p(s) : p) })), [])
  const setFund = useCallback((p: Partial<FundState>) => setFundRaw((s) => ({ ...s, ...p })), [])

  const fill = useCallback((o: Order) => {
    const px = lastPx(o.ticker)
    const slip = o.side === 'Buy' || o.side === 'Cover' ? 1.0003 : 0.9997
    const fillPx = o.type === 'Limit' && o.limit ? o.limit : +(px * slip).toFixed(2)
    const signed = o.side === 'Buy' || o.side === 'Cover' ? o.qty : -o.qty
    setPositions((ps) => {
      const ex = ps.find((p) => p.ticker === o.ticker)
      if (!ex) return [...ps, { ticker: o.ticker, qty: signed, avg: fillPx, strategy: o.origin === 'Model' ? 'CPX-1' : 'User', signal: o.signal, realised: 0 }]
      const newQty = ex.qty + signed
      const reducing = Math.sign(signed) !== Math.sign(ex.qty)
      const realised = reducing ? Math.min(Math.abs(signed), Math.abs(ex.qty)) * (fillPx - ex.avg) * Math.sign(ex.qty) : 0
      if (newQty === 0) return ps.filter((p) => p !== ex)
      const avg = reducing ? ex.avg : (ex.avg * ex.qty + fillPx * signed) / newQty
      return ps.map((p) => (p === ex ? { ...p, qty: newQty, avg: +avg.toFixed(2), realised: p.realised + realised } : p))
    })
    setCash((c) => c - signed * fillPx)
    setOrders((os) => os.map((x) => (x.id === o.id ? { ...x, status: 'Filled', fillPx } : x)))
    toast(`Paper fill: ${o.side} ${o.qty.toLocaleString()} ${o.ticker} @ $${fillPx.toFixed(2)}`, 'ok')
  }, [toast])

  const submitOrder = useCallback(
    (o: Omit<Order, 'id' | 'time' | 'status'>) => {
      const id = `ORD-${orderSeq.current++}`
      const order: Order = { ...o, id, time: nowStamp(), status: 'Pending' }
      setOrders((os) => [order, ...os])
      const nav = navOf(positions, cash)
      const notional = o.qty * lastPx(o.ticker)
      let reject: string | null = null
      if (!lastPx(o.ticker)) reject = 'Unknown instrument'
      else if ((o.side === 'Short') && ['IREN', 'APLD'].includes(o.ticker)) reject = 'No borrow available (simulated locate failed)'
      else if (notional > nav * 0.15) reject = `Exceeds single-order limit (15% NAV = ${Math.round(nav * 0.15).toLocaleString()} USD)`
      else if ((o.side === 'Buy' || o.side === 'Short') && notional > cash + nav * 0.5) reject = 'Insufficient buying power'
      setTimeout(() => {
        if (reject) {
          setOrders((os) => os.map((x) => (x.id === id ? { ...x, status: 'Rejected', reason: reject! } : x)))
          toast(`Paper order rejected: ${reject}`, 'bad')
          return
        }
        if (!marketRef.current) {
          setOrders((os) => os.map((x) => (x.id === id ? { ...x, status: 'Queued', reason: 'Market closed — will execute at next open' } : x)))
          toast(`Market closed — ${o.ticker} order queued for next open`, 'warn')
          return
        }
        fill(order)
      }, 1400)
      return id
    },
    [positions, cash, fill, toast],
  )

  const cancelOrder = useCallback((id: string) => {
    setOrders((os) => os.map((x) => (x.id === id && (x.status === 'Pending' || x.status === 'Queued') ? { ...x, status: 'Cancelled', reason: 'Cancelled by user' } : x)))
  }, [])

  const toggleMarket = useCallback(() => {
    const next = !marketRef.current
    marketRef.current = next
    setMarketOverride(next === clockOpen ? null : next)
    if (next) orders.filter((x) => x.status === 'Queued').forEach((x, i) => setTimeout(() => fill(x), 500 + i * 150))
  }, [fill, orders, clockOpen])

  const reducePosition = useCallback(
    (ticker: string, fraction: number) => {
      const p = positions.find((x) => x.ticker === ticker)
      if (!p) return
      const qty = Math.max(1, Math.round(Math.abs(p.qty) * fraction))
      submitOrder({ ticker, side: p.qty > 0 ? 'Sell' : 'Cover', qty, type: 'Market', origin: 'User', note: fraction >= 1 ? 'Close position' : `Reduce ${Math.round(fraction * 100)}%` })
    },
    [positions, submitOrder],
  )

  const liquidateAll = useCallback(() => {
    // Liquidation bypasses the single-order limit by splitting per position.
    positions.forEach((p, i) => {
      setTimeout(() => {
        const id = `ORD-${orderSeq.current++}`
        const o: Order = { id, time: nowStamp(), ticker: p.ticker, side: p.qty > 0 ? 'Sell' : 'Cover', qty: Math.abs(p.qty), type: 'Market', status: 'Pending', origin: 'User', note: 'Liquidate all' }
        setOrders((os) => [o, ...os])
        setTimeout(() => {
          if (!marketRef.current) setOrders((os) => os.map((x) => (x.id === id ? { ...x, status: 'Queued', reason: 'Market closed — will execute at next open' } : x)))
          else fill(o)
        }, 900 + i * 120)
      }, i * 60)
    })
  }, [positions, fill])

  const actOnProposal = useCallback(
    (id: string, action: 'send' | 'reject', qty?: number) => {
      const p = proposals.find((x) => x.id === id)
      if (!p) return
      if (action === 'reject') {
        setProposals((ps) => ps.map((x) => (x.id === id ? { ...x, state: 'rejected' } : x)))
        toast(`Proposal rejected: ${p.side} ${p.ticker}. Logged as user override.`, 'info')
        return
      }
      const q = qty ?? p.modifiedQty ?? p.qty
      setProposals((ps) => ps.map((x) => (x.id === id ? { ...x, state: 'sent', modifiedQty: q } : x)))
      submitOrder({ ticker: p.ticker, side: p.side, qty: q, type: 'Market', origin: 'Model', proposalId: id, signal: p.signal, note: q !== p.qty ? `Modified by user (${p.qty} → ${q})` : 'Approved by user' })
    },
    [proposals, submitOrder, toast],
  )
  const modifyProposal = useCallback((id: string, qty: number) => setProposals((ps) => ps.map((x) => (x.id === id ? { ...x, modifiedQty: qty } : x))), [])

  const ask = useCallback((seed?: string) => setAssistant((a) => ({ open: true, seed, nonce: a.nonce + 1 })), [])

  const nav = useMemo(() => navOf(positions, cash), [positions, cash])

  return {
    live,
    route, navigate, inspector, setInspector, inspect, trail, setTrail,
    assistant, setAssistant, ask, palette, setPalette,
    positions, cash, nav, orders, submitOrder, cancelOrder, reducePosition, liquidateAll,
    proposals, actOnProposal, modifyProposal,
    marketOpen, marketOverridden, marketLabel, toggleMarket, toasts, toast,
    xray, setXray, fund, setFund, tour, setTour, ticket, setTicket,
  }
}

export type Store = ReturnType<typeof useStoreValue>
const Ctx = createContext<Store | null>(null)
export function StoreProvider({ children }: { children: ReactNode }) {
  const v = useStoreValue()
  return <Ctx.Provider value={v}>{children}</Ctx.Provider>
}
export function useStore() {
  const v = useContext(Ctx)
  if (!v) throw new Error('StoreProvider missing')
  return v
}
/** The store when a provider is mounted, else null: for shared chrome (Panel) that must also render in isolation, e.g. static tests. */
export function useStoreOptional() {
  return useContext(Ctx)
}
