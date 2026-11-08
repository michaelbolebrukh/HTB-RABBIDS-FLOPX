import { useState } from 'react'
import { Plus } from 'lucide-react'
import { lastPx, NAV0, signalById } from '@/data/fund'
import { secByTicker } from '@/data/finance'
import { HBars } from '@/components/charts'
import { Btn, cx, Delta, Empty, Fresh, PageHeader, Panel, Seg, StripCell, TruthGlyph } from '@/components/ui'
import { Row } from '@/components/collapse'
import { useStore } from '@/lib/store'
import { ACCOUNT_LIVE } from '@/live/account'
import { MockNote, MockToggle, useMockBook } from '@/components/MockToggle'
import { mockDeskLabel, mockPx } from '@/data/mockDesk'
import { OrdersList, PositionsTable } from './Fund'

export default function Trading() {
  const { positions, orders: designOrders, nav: designNav, cash: designCash, marketOpen, marketLabel, toggleMarket, setTicket, reducePosition, inspect } = useStore()
  const [filter, setFilter] = useState<'open' | 'filled' | 'rejected' | 'all'>('all')
  // The signed-in reader's paper account (src/live/account.ts) replaces the design's simulated
  // account for NAV, cash, buying power, holdings and orders; everything priced off the design's
  // closes (strategy contribution, VaR, arrival prices) stays the design's and is badged as such.
  const acct = ACCOUNT_LIVE.state
  // The mock book (src/data/mockDesk.ts) stands in through the same path when the paper account is
  // empty or nobody is signed in; every panel it feeds carries mockDeskLabel as a quiet note.
  const { live, mock, book, showMock, setShowMock } = useMockBook(acct)
  const nav = book ? book.account.nav : designNav
  const cash = book ? book.account.cash : designCash
  const orders = book ? book.orders : designOrders
  const shown = orders.filter((o) => filter === 'all' || (filter === 'open' ? o.status === 'Pending' || o.status === 'Queued' : filter === 'filled' ? o.status === 'Filled' : o.status === 'Rejected' || o.status === 'Cancelled'))
  const byOwner = mock ? mock.pnlByOwner.map((r, i) => ({ label: r.label, value: r.value / 1000, color: i === 0 ? 'var(--color-dec)' : 'var(--color-bone)' })) : (['CPX-1', 'User'] as const).map((o) => ({ label: o === 'CPX-1' ? 'Model · CPX-1' : 'User-initiated', value: positions.filter((p) => p.strategy === o).reduce((s, p) => s + p.qty * (lastPx(p.ticker) - p.avg), 0) / 1000, color: o === 'CPX-1' ? 'var(--color-dec)' : 'var(--color-bone)' }))
  const bySignal = mock ? mock.pnlByCluster.map((r) => ({ label: r.label, value: r.value / 1000 })) : Object.values(positions.reduce<Record<string, { label: string; value: number }>>((m, p) => { const k = p.signal ? signalById(p.signal)!.name : 'Discretionary'; m[k] = m[k] ?? { label: k, value: 0 }; m[k].value += (p.qty * (lastPx(p.ticker) - p.avg)) / 1000; return m }, {}))
  const marks = book ? book.account.positions.map((p) => ({ qty: p.qty, mv: p.mv ?? 0 })) : positions.map((p) => ({ qty: p.qty, mv: p.qty * lastPx(p.ticker) }))
  const gross = marks.reduce((s, p) => s + Math.abs(p.mv), 0)
  const longs = marks.filter((p) => p.qty > 0).reduce((s, p) => s + p.mv, 0)
  const shorts = marks.filter((p) => p.qty < 0).reduce((s, p) => s + p.mv, 0)
  const fills = orders.filter((o) => o.status === 'Filled' && o.fillPx)
  const holdingsEmpty = book ? book.account.positions.length === 0 : positions.length === 0

  return (
    <div>
      <PageHeader
        eyebrow={<>{mock ? 'Mock book' : <>Paper account <span className="num">{live ? live.email : 'FX-0042'}</span></>} · simulated venue{' '}
          {live ? <Fresh quiet f={live.fresh.freshness} label={live.fresh.label} /> : acct.status === 'failed' ? <Fresh quiet f="unavailable" label="Account read failed · figures illustrative" /> : <Fresh quiet f="illustrative" label={acct.status === 'pending' ? 'Prices illustrative · reading account' : 'Prices illustrative'} />}</>}
        title="Paper portfolio & execution"
        sub="Paper holdings, tickets, orders and post-trade review. Nothing here is live."
        about="Model recommendations, user actions and completed paper fills are always labelled separately. Nothing here is a live trade."
        right={<><MockToggle on={showMock} onChange={setShowMock} /><Btn variant="decision" onClick={() => setTicket({ ticker: 'VST' })}><Plus size={14} /> New paper order</Btn></>}
      />
      {acct.status === 'failed' && (
        <div className="mb-5 rounded-md border border-warn/35 bg-warn/[0.04] px-4 py-3 text-[12.5px] text-warn">
          The paper account for <span className="font-mono">{acct.email}</span> could not be read: {acct.error}. {mock ? 'The figures below are the mock book, not this account\'s.' : "The figures below are the design's illustrative account, not this account's."}
        </div>
      )}
      {mock && <MockNote className="mb-1.5" />}
      <Panel variant="strip" id="Account" title="Account" className="mb-5">
        {[
          ['NAV', `$${(nav / 1e6).toFixed(3)}M`, book ? <span key="d">P&amp;L <Delta v={(book.account.realised + book.account.unrealised) / 1000} d={1} suffix="k" /></span> : <Delta key="d" v={((nav - NAV0) / NAV0) * 100} d={2} />],
          ['Cash', `$${(cash / 1e6).toFixed(2)}M`, `${nav ? ((cash / nav) * 100).toFixed(0) : '—'}% NAV`],
          ['Buying power', `$${((book ? book.account.buyingPower : cash + nav * 0.5) / 1e6).toFixed(2)}M`, mock ? 'Reg-T style, mock' : live ? 'FLOPX paper desk' : 'Reg-T style, simulated'],
          ['Long', `$${(longs / 1e6).toFixed(2)}M`, `${marks.filter((p) => p.qty > 0).length} names`],
          ['Short', `$${(Math.abs(shorts) / 1e6).toFixed(2)}M`, `${marks.filter((p) => p.qty < 0).length} names`],
        ].map(([k, v, s], i) => <StripCell key={k as string} lead={i === 0} label={k} sub={s}>{v}</StripCell>)}
        <button onClick={toggleMarket} className="min-w-[140px] flex-1 cursor-pointer px-4 py-3 text-left hover:bg-hover" title={`NYSE regular session 14:30 to 21:00 UTC on weekdays, from the clock (no holiday table). ${marketLabel}. Demo control: click to override.`}>
          <div className="text-[11px] leading-4 text-dim">US equities</div>
          <div className={cx('mt-1 flex items-center gap-2 text-[15px] leading-6 font-medium', marketOpen ? 'text-up' : 'text-warn')}><span className={cx('size-2 rounded-full', marketOpen ? 'bg-up pulse-dot' : 'bg-warn')} />{marketOpen ? 'Open' : 'Closed'}</div>
          <div className="mt-1 truncate text-[11px] leading-4 text-mute">{marketOpen ? marketLabel : `orders queue · ${marketLabel}`}</div>
        </button>
      </Panel>

      <Panel className="mb-5" title="Holdings" eyebrow={<span title="Reduce, close or ticket a position" className="inline-flex items-center gap-1.5">Paper positions · Own: <TruthGlyph truth="dec" size={7} /> CPX-1 · U user{book && <> <Fresh quiet f={book.fresh.freshness} label={`${book.email} · ${book.fresh.label}`} /></>}</span>} bodyClass="p-0">
        {holdingsEmpty ? <Empty className="m-4" title="Portfolio empty" body={book ? 'This account holds no paper positions.' : 'No paper positions. Open one with a ticket or approve a CPX-1 proposal in the Fund.'} action={book ? undefined : <Btn size="sm" variant="decision" onClick={() => setTicket({ ticker: 'VST' })}>New paper order</Btn>} /> : <PositionsTable live={book?.account.positions} liveNav={book?.account.nav} onReduce={reducePosition} onTicket={(t) => setTicket({ ticker: t })} onInspect={(t) => inspect({ kind: 'security', id: t, label: t })} />}
      </Panel>

      <Row cols={[1.4, 1]} at="xl" className="mb-5">
        <Panel variant="rail" title="Orders" eyebrow={book ? <>Simulated {book.ordersError ? <Fresh quiet f="unavailable" label={`Orders read failed: ${book.ordersError}`} /> : <Fresh quiet f={book.fresh.freshness} label={mock ? book.fresh.label : `${book.email} · FLOPX paper desk`} />}</> : 'Simulated'} right={<Seg size="sm" value={filter} onChange={setFilter} options={[{ id: 'all', label: 'All' }, { id: 'open', label: 'Open' }, { id: 'filled', label: 'Filled' }, { id: 'rejected', label: 'Rejected' }]} />} bodyClass="p-0">
          <OrdersList orders={shown} />
        </Panel>
        <div className="space-y-5">
          <Panel title="Strategy contribution" eyebrow={<>Unrealised P&L · $k{mock && ' · by owner, then by cluster'} {mock ? <Fresh quiet f="illustrative" label={mockDeskLabel} /> : live && <Fresh quiet f="illustrative" label="Design account, not yours" />}</>}>
            <HBars signed rows={byOwner} fmt={(v) => `${v > 0 ? '+' : ''}${v.toFixed(1)}k`} />
            <div className="mt-4 border-t border-line pt-3"><HBars signed rows={bySignal.map((x) => ({ label: x.label, value: x.value }))} fmt={(v) => `${v > 0 ? '+' : ''}${v.toFixed(1)}k`} /></div>
          </Panel>
          <Panel title="Risk exposures" eyebrow={<>Estimated {mock ? <Fresh quiet f="illustrative" label={mockDeskLabel} /> : live && <Fresh quiet f="illustrative" label="VaR illustrative" />}</>}>
            <div className="grid grid-cols-3 gap-3">
              {[['Gross', `${((gross / nav) * 100).toFixed(0)}%`], ['Net', `${(((longs + shorts) / nav) * 100).toFixed(0)}%`], ['1d VaR 95%', `$${Math.round(nav * 0.0118 / 1000)}k`]].map(([k, v]) => <div key={k}><div className="text-[11px] text-dim font-medium">{k}</div><div className="num text-[15px]">{v}</div></div>)}
            </div>
          </Panel>
        </div>
      </Row>

      <Panel title="Post-trade review" eyebrow={<>Fill vs arrival · simulated {mock ? <Fresh quiet f="illustrative" label={mockDeskLabel} /> : live && <Fresh quiet f="illustrative" label="Arrival prices illustrative" />}</>} bodyClass="p-0">
        <div className="grid h-8 grid-cols-[90px_70px_1fr_90px_90px_90px_1fr] items-center gap-3 border-b border-line px-4 text-[11px] font-medium text-dim"><span>Order</span><span>Ticker</span><span>Origin</span><span className="text-right">Fill ($)</span><span className="text-right">Arrival ($)</span><span className="text-right">Slippage</span><span>Review</span></div>
        {fills.length === 0 && <div className="p-4 text-[12px] text-mute">No fills yet.</div>}
        {fills.map((o) => {
          const arrival = mock ? mockPx(o.ticker) : lastPx(o.ticker)
          const slip = ((o.fillPx! - arrival) / arrival) * 10000 * (o.side === 'Buy' || o.side === 'Cover' ? 1 : -1)
          return (
            <div key={o.id} className="grid grid-cols-[90px_70px_1fr_90px_90px_90px_1fr] items-center gap-3 border-b border-line-soft px-4 py-2 text-[12px]">
              <span className="font-mono text-[10.5px] text-dim">{o.id}</span><span className="font-mono">{o.ticker}</span>
              <span className={cx('text-[11.5px]', o.origin === 'Model' ? 'text-dec' : 'text-mute')}>{o.origin === 'Model' ? `Model${o.note ? ` · ${o.note}` : ''}` : 'User'}</span>
              <span className="num text-right">{o.fillPx!.toFixed(2)}</span><span className="num text-right text-mute">{arrival.toFixed(2)}</span>
              <span className={cx('num text-right', Math.abs(slip) > 50 ? 'text-warn' : 'text-mute')}>{Math.abs(slip) > 200 ? 'n/a' : slip.toFixed(1)}</span>
              <span className="text-[11.5px] text-mute"><span title={Math.abs(slip) > 200 ? 'Historical fill; price has since moved' : o.origin === 'Model' ? 'Followed model target' : 'User discretion, excluded from strategy attribution'}>{Math.abs(slip) > 200 ? 'Stale fill' : o.origin === 'Model' ? 'On target' : 'Discretionary'}</span></span>
            </div>
          )
        })}
      </Panel>
      <div className="mt-2 text-[11px] text-dim">{secByTicker('VST') ? 'Paper fills assume last price + 3 bps; no market impact modelled.' : ''}</div>
    </div>
  )
}
