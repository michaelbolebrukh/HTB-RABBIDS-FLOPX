import { useMemo, useState, type ReactNode } from 'react'
import { ArrowRight, Check, Pencil, Send, X } from 'lucide-react'
import { ANOMALY_CARDS, AS_OF, PRESSURE_MODEL, regionById } from '@/data/core'
import { daysAgo, fmtDM, fmtDMY, weekdaysAgo } from '@/lib/designDates'
import { backtestSpanLabel, BASKETS, PAPER_START_WK, paperStartLabel, secByTicker, WEEK_DATES, WEEKS } from '@/data/finance'
import { W2_LIVE } from '@/live/adapt-w2'
import { ACCOUNT_LIVE, type LivePosition } from '@/live/account'
import { MockNote, MockToggle, useMockBook } from '@/components/MockToggle'
import { mockDeskLabel, mockNameOf } from '@/data/mockDesk'
import { StrategiesPanel } from '@/components/Strategies'
import { BENCH, DECISIONS, DRAWDOWN, EQUITY, EQUITY_OHLC, lastPx, NAV0, prevPx, SIGNALS, signalById, signalSupport, VOLUME, type HorizonId } from '@/data/fund'
import { Distribution, HBars, TimeChart } from '@/components/charts'
import { StockChart } from '@/components/StockChart'
import { ChartTypeMenu } from '@/components/ChartTypeMenu'
import { IndicatorChips, IndicatorMenu } from '@/components/IndicatorMenu'
import type { IndicatorId } from '@/lib/indicators'
import type { ChartType } from '@/lib/chartTypes'
import { BacktestCosts, Failures, ForecastVsActual, ModelComparison, ValidationMatrix } from '@/components/Eval'
import { AIBlock, Btn, IcSharpe, cx, Delta, DirectionTag, Empty, EntityLink, Fresh, Meter, PageHeader, Panel, ResetLayout, Seg, StripCell, Tabs, TruthGlyph, TruthTag, Validation } from '@/components/ui'
import { useStore } from '@/lib/store'
import { Foldable, Row } from '@/components/collapse'
import { FUND_TESTS as FT, SNAPSHOT } from '@/data/handoff'

const HZ_WEEKS: Record<HorizonId, number> = { '1W': 1, '1M': 4, '3M': 13 }
/** Deepest drawdown (%, negative) over a slice of the weekly drawdown series: the one number the KPI and the chart readout share. */
export function maxDrawdown(dd: number[], from = 0, to = dd.length): number {
  let m = 0
  for (let i = from; i < Math.min(to, dd.length); i++) if (dd[i] < m) m = dd[i]
  return m
}
const fmtDD = (v: number) => `${v < 0 ? '−' : ''}${Math.abs(v).toFixed(1)}%`

export default function Fund() {
  const s = useStore()
  const { fund, setFund, positions } = s
  // The signed-in reader's paper account (src/live/account.ts) supplies NAV, cash, P&L, exposure and
  // paper drawdown; the design's simulated account stands in, badged, for everyone else. The 1W P&L
  // and the benchmark comparison are priced off the design's closes, so a live account shows them
  // as not available rather than mixing the two.
  const acct = ACCOUNT_LIVE.state
  // The mock book (src/data/mockDesk.ts) stands in through the same path when the paper account is
  // empty or nobody is signed in; every panel it feeds carries mockDeskLabel as a quiet note.
  const { live, mock, book, showMock, setShowMock } = useMockBook(acct)
  const nav = book ? book.account.nav : s.nav
  const cash = book ? book.account.cash : s.cash
  const unreal = book ? book.account.unrealised : positions.reduce((a, p) => a + p.qty * (lastPx(p.ticker) - p.avg), 0)
  const realised = book ? book.account.realised : positions.reduce((a, p) => a + p.realised, 0) + 41_250
  const day = positions.reduce((a, p) => a + p.qty * (lastPx(p.ticker) - prevPx(p.ticker)), 0)
  const gross = book ? book.account.positions.reduce((a, p) => a + Math.abs(p.mv ?? 0), 0) : positions.reduce((a, p) => a + Math.abs(p.qty * lastPx(p.ticker)), 0)
  const netExp = book ? book.account.positions.reduce((a, p) => a + (p.mv ?? 0), 0) : positions.reduce((a, p) => a + p.qty * lastPx(p.ticker), 0)
  const sox = BENCH[0].values
  const spx = BENCH[1].values
  const soxRet = ((sox[sox.length - 1] - sox[PAPER_START_WK]) / sox[PAPER_START_WK]) * 100
  const spxRet = ((spx[spx.length - 1] - spx[PAPER_START_WK]) / spx[PAPER_START_WK]) * 100
  const openProps = s.proposals.filter((p) => p.state === 'open').length

  return (
    <div>
      <PageHeader
        eyebrow={<span title={`As of ${AS_OF}`}>Flagship · HRT</span>}
        title={<>Systematic compute fund <span className="text-mute">· CPX-1</span></>}
        sub={<span className="flex flex-wrap items-center gap-1.5">
          {/* The page's own statuses are quiet notes (WF8 #3); the account read, when it differs from them, keeps a chip. */}
          <Fresh quiet f="simulated" label={`Paper since ${paperStartLabel()}`} />
          <span className="rounded-[2px] border border-dashed border-line2 px-1.5 py-[1px] text-[11px] font-medium text-mute">Backtest {backtestSpanLabel()} · hypothetical</span>
          {/* Signed in with the account read, the bare note would sit beside the account badge and read as one claim (WF7 #3): name what stays illustrative instead. */}
          <Fresh quiet f="illustrative" label={book ? 'Illustrative · backtest, equity curve & benchmarks' : undefined} />
          {live && <Fresh f={live.fresh.freshness} label={live.fresh.label} />}
          {mock && <Fresh quiet f="illustrative" label={mockDeskLabel} />}
          {acct.status === 'failed' && <Fresh f="unavailable" label="Account read failed" />}
          <span className="text-[11px] text-dim">· prices {Object.values(W2_LIVE.companies).some((c) => c.observedWeeks > 0) ? 'partly observed' : 'illustrative'} · {AS_OF}</span>
        </span>}
        right={<><MockToggle on={showMock} onChange={setShowMock} /><ResetLayout /></>}
      />
      {acct.status === 'failed' && (
        <div className="mb-5 rounded-md border border-warn/35 bg-warn/[0.04] px-4 py-3 text-[12.5px] text-warn">
          The paper account for <span className="font-mono">{acct.email}</span> could not be read: {acct.error}. {mock ? 'The account figures below are the mock book, not this account\'s.' : "The account figures below are the design's illustrative account, not this account's."}
        </div>
      )}

      <Panel variant="callout" tone="warn" id="Pre-registered tests" className="mb-5"
        title={<span className="text-warn">Edge in <span className="num">{FT.edges}</span> of <span className="num">{FT.tests}</span> forecast tests (8 tickers x 3 models) · no edge found</span>}
        right={<Fresh f="historical" label={SNAPSHOT.short} />}>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2" title={`Across ${FT.tickers} tickers. Prices: ${FT.prices}.`}>
          <div className="grid grid-cols-12 gap-1" role="img" aria-label={`${FT.edges} of ${FT.tests} tests show an edge`}>
            {Array.from({ length: FT.tests }, (_, i) => <span key={i} className="h-3 w-3 rounded-[2px] border border-warn/50" title={`Test ${i + 1}: no edge found`} />)}
          </div>
          <span className="text-[11.5px] text-mute" title="Half-size paper orders kept as exploratory"><span className="num text-bone">{FT.exploratoryOrders}</span> exploratory half-size orders</span>
        </div>
      </Panel>

      {mock && <MockNote className="mb-1.5" />}
      <Panel variant="strip" id="Key figures" title="Paper performance" className="mb-5">
        <KPI lead label="Paper NAV" value={`$${(nav / 1e6).toFixed(3)}M`} sub={book ? <span>P&L <Delta v={(unreal + realised) / 1000} d={1} suffix="k" /></span> : <Delta v={((nav - NAV0) / NAV0) * 100} d={2} />} truth="dec" />
        <KPI label="1W P&L" value={book ? <span className="text-mute">—</span> : <Delta v={day / 1000} d={1} suffix="k" />} sub={mock ? 'not in the mock book' : live ? 'not in the account read' : <span>{((day / nav) * 100).toFixed(2)}%</span>} />
        <KPI label="Unrealised" value={<Delta v={unreal / 1000} d={1} suffix="k" />} sub="open" />
        <KPI label="Realised" value={<Delta v={realised / 1000} d={1} suffix="k" />} sub={book ? 'since inception' : 'paper to date'} />
        <KPI label="Cash" value={`$${(cash / 1e6).toFixed(2)}M`} sub={`${nav ? ((cash / nav) * 100).toFixed(0) : '—'}% NAV`} />
        <KPI label="Gross / Net" value={nav ? `${((gross / nav) * 100).toFixed(0)}% / ${((netExp / nav) * 100).toFixed(0)}%` : '—'} sub="exposure" />
        <KPI label="vs SOX · SPX" value={book ? <span className="text-mute">—</span> : <span className="text-[13px]"><Delta v={((nav - NAV0) / NAV0) * 100 - soxRet} d={1} suffix="pp" /> · <Delta v={((nav - NAV0) / NAV0) * 100 - spxRet} d={1} suffix="pp" /></span>} sub={mock ? 'not in the mock book' : live ? 'needs the fund summary read' : 'paper period · 10 wks'} />
        {/* Signed in, the account's paper drawdown is the headline and the design's backtest the small print (WF7 #3). */}
        <KPI label="Max DD" value={book ? fmtDD(-Math.abs(book.account.maxDrawdownPct)) : `${fmtDD(maxDrawdown(DRAWDOWN, 0, PAPER_START_WK))}`} sub={book ? `paper (${mock ? 'mock' : 'account'}) · backtest ${fmtDD(maxDrawdown(DRAWDOWN, 0, PAPER_START_WK))}` : `backtest · paper ${fmtDD(maxDrawdown(DRAWDOWN, PAPER_START_WK))}`} />
      </Panel>

      <Tabs className="mb-5" value={fund.tab} onChange={(t) => setFund({ tab: t })} tabs={[{ id: 'portfolio', label: 'Portfolio' }, { id: 'signals', label: 'Signals', count: SIGNALS.length }, { id: 'decisions', label: 'Decisions', count: openProps }, { id: 'evaluation', label: 'Evaluation' }]} />
      {fund.tab === 'portfolio' && <PortfolioTab />}
      {fund.tab === 'signals' && <SignalsTab />}
      {fund.tab === 'decisions' && <DecisionsTab />}
      {fund.tab === 'evaluation' && <EvaluationTab />}
    </div>
  )
}

function KPI({ label, value, sub, truth, lead }: { label: string; value: ReactNode; sub?: ReactNode; truth?: 'dec'; lead?: boolean }) {
  return (
    <StripCell lead={lead} className="min-w-[112px]" label={<span className="flex items-center gap-1.5">{truth && <TruthGlyph truth={truth} size={7} />}{label}</span>} sub={sub}>
      {value}
    </StripCell>
  )
}

function PortfolioTab() {
  const { fund, setFund, positions, nav: designNav, orders: designOrders, reducePosition, liquidateAll, setTicket, inspect, navigate, setXray, live: liveVersion } = useStore()
  const [confirmLiq, setConfirmLiq] = useState(false)
  const rec = fund.record ? DECISIONS.find((d) => d.id === fund.record) : null
  const [chartType, setChartType] = useState<ChartType>('candles')
  const [indicators, setIndicators] = useState<IndicatorId[]>([])
  const acct = ACCOUNT_LIVE.state
  const { live, mock, book } = useMockBook(acct)
  const nav = book ? book.account.nav : designNav
  const orders = book ? book.orders : designOrders
  const holdingsEmpty = book ? book.account.positions.length === 0 : positions.length === 0
  const clusters = useMemo(() => {
    if (mock) return mock.exposureByCluster
    const m: Record<string, number> = {}
    if (book) book.account.positions.forEach((p) => { const g = secByTicker(p.ticker)?.group ?? 'Other'; m[g] = (m[g] ?? 0) + (p.mv ?? 0) })
    else positions.forEach((p) => { const g = secByTicker(p.ticker)!.group; m[g] = (m[g] ?? 0) + p.qty * lastPx(p.ticker) })
    return Object.entries(m).map(([k, v]) => ({ label: k, value: nav ? (v / nav) * 100 : 0 }))
  }, [positions, nav, live, mock, book, liveVersion]) // liveVersion: ACCOUNT_LIVE is mutated in place; the version is what changes
  const regionExp = useMemo(() => {
    const m: Record<string, number> = {}
    positions.forEach((p) => secByTicker(p.ticker)!.computeLinks.forEach((l) => { if (l.region) m[l.region] = (m[l.region] ?? 0) + p.qty * lastPx(p.ticker) }))
    return Object.entries(m).map(([k, v]) => ({ id: k, label: regionById(k)!.name, value: (v / nav) * 100 }))
  }, [positions, nav])

  return (
    <div className="space-y-5">
      <Row cols={[1, '380px']} at="2xl">
        <Panel
          title="Equity curve"
          eyebrow={<span title="Click a ◆ marker to open its decision record">Weekly · <span className="text-dec">◆</span> decisions <Fresh quiet f="illustrative" /></span>}
          right={<div className="flex items-center gap-2"><IndicatorMenu value={indicators} onChange={setIndicators} /><ChartTypeMenu value={chartType} onChange={setChartType} /></div>}
        >
          <IndicatorChips value={indicators} onChange={setIndicators} />
          <StockChart
            labels={WEEKS}
            candles={EQUITY_OHLC}
            volume={VOLUME}
            drawdown={DRAWDOWN}
            type={chartType}
            indicators={indicators}
            splitIdx={PAPER_START_WK}
            splitLabel="Paper"
            markers={DECISIONS.map((d) => ({ i: d.week, id: d.id, label: d.title, hollow: d.mode === 'backtest', active: fund.record === d.id }))}
            onMarker={(id) => setFund({ record: id })}
            lines={[
              { id: 'cpx', label: 'CPX-1', values: EQUITY, color: 'var(--color-dec)', width: 2 },
              { id: 'sox', label: 'SOX', values: BENCH[0].values, color: 'var(--color-mute)', opacity: 0.55 },
              { id: 'spx', label: 'SPX', values: BENCH[1].values, color: 'var(--color-mute)', opacity: 0.55, dash: '4 3' },
            ]}
          />
        </Panel>
        <DecisionRecordPanel rec={rec ?? null} onClose={() => setFund({ record: null })} />
      </Row>

      <Panel title="Positions" eyebrow={<span title="Paper holdings, model- and user-owned. Own column: ◆ = CPX-1, U = User" className="inline-flex items-center gap-1.5">Paper holdings · Own: <TruthGlyph truth="dec" size={7} /> CPX-1 · U user {book ? <Fresh quiet f={book.fresh.freshness} label={`${book.email} · ${book.fresh.label}`} /> : <Fresh quiet f="illustrative" />}</span>} right={!book && positions.length > 0 && (confirmLiq ? <div className="flex items-center gap-2"><span className="text-[11.5px] text-down">Sell/cover all {positions.length} positions?</span><Btn size="sm" variant="danger" onClick={() => { liquidateAll(); setConfirmLiq(false) }}>Confirm liquidate</Btn><Btn size="sm" variant="ghost" onClick={() => setConfirmLiq(false)}>Cancel</Btn></div> : <Btn size="sm" variant="danger" onClick={() => setConfirmLiq(true)}>Liquidate all</Btn>)} bodyClass="p-0">
        {holdingsEmpty ? (
          <Empty className="m-4" title="Portfolio empty" body={book ? 'This account holds no paper positions.' : 'All paper positions are closed. CPX-1 will propose new targets at the next rebalance, or you can open a position manually.'} action={book ? undefined : <Btn size="sm" variant="decision" onClick={() => setTicket({ ticker: 'VST' })}>New paper order</Btn>} />
        ) : (
          <PositionsTable live={book?.account.positions} liveNav={book?.account.nav} onReduce={reducePosition} onTicket={(t) => setTicket({ ticker: t })} onInspect={(t) => inspect({ kind: 'security', id: t, label: t })} />
        )}
      </Panel>

      <Row cols={[1, 1, 1]}>
        <Panel title="Exposure by cluster" eyebrow={<>% NAV · net {book ? <Fresh quiet f={book.fresh.freshness} label={book.fresh.label} /> : <Fresh quiet f="illustrative" />}</>}>
          <HBars signed rows={clusters.map((c) => ({ label: c.label, value: c.value }))} fmt={(v) => `${v.toFixed(1)}%`} />
        </Panel>
        <Panel title="Exposure by compute region" eyebrow={<>Via documented company ties <Fresh quiet f="illustrative" /></>}>
          {regionExp.length === 0 ? <div className="text-[12px] text-mute">No regional exposure.</div> : (
            <div className="space-y-1">
              <HBars signed rows={regionExp.map((c) => ({ label: <button className="cursor-pointer hover:text-bone" onClick={() => { setXray({ region: c.id, tab: 'exposure' }); navigate('xray') }}>{c.label}</button>, value: c.value }))} fmt={(v) => `${v.toFixed(1)}%`} />
            </div>
          )}
          {(() => {
            // The live anomaly scan (/flopx/anomalies) when it has landed; the design's Texas worked example, badged, until then.
            const card = PRESSURE_MODEL.anomaliesLive ? ANOMALY_CARDS[0] : null
            return (
              <div className="mt-3 border-l-2 border-inf py-1 pl-2.5 text-[11.5px]">
                <div className="mb-0.5 flex items-center gap-1.5 text-[11px] text-inf font-medium"><TruthGlyph truth="inf" size={6} />{card ? 'Open compute anomaly' : 'Linked compute anomaly'} {card && <Fresh f="experimental" label="FLOPX anomaly scan" />}</div>
                {card ? <>{card.title}: {card.sub} CPX-1 has no proposal linked to it; the proposals below are design examples. </> : PRESSURE_MODEL.anomaliesLive ? <>No open anomaly in the FLOPX regions; the proposals below are design examples. </> : <>Texas pressure +14 pts → CPX-1 proposes VST +1.5 pp (design example). </>}
                <button className="cursor-pointer text-inf underline-offset-2 hover:underline" onClick={() => { setXray({ region: card?.region ?? 'us-tx', tab: 'brief' }); navigate('xray') }}>See anomaly</button> · <button className="cursor-pointer text-dec underline-offset-2 hover:underline" onClick={() => setFund({ tab: 'decisions' })}>Review proposal</button>
              </div>
            )
          })()}
        </Panel>
        <Panel title="Risk" eyebrow={<>Estimated · 60d window <Fresh quiet f="illustrative" label={mock ? mockDeskLabel : undefined} /></>}>
          <div className="grid grid-cols-2 gap-3">
            {[['Beta to SPX', '0.71'], ['Ann. vol (est.)', '11.4%'], ['1d VaR 95%', `$${Math.round(nav * 0.0118).toLocaleString()}`], ['Top-3 weight', '26.4%'], ['Risk budget used', '68%'], ['Liquidity (days)', '< 0.2']].map(([k, v]) => <div key={k}><div className="text-[11px] text-dim font-medium">{k}</div><div className="num text-[15px]">{v}</div></div>)}
          </div>
          <div className="mt-3"><Meter value={0.68} color="var(--color-mute)" /><div className="mt-1 text-[11px] text-dim">of 4% ann. vol budget</div></div>
        </Panel>
      </Row>

      <StrategiesPanel />

      <Row cols={[1, 1]}>
        <Panel variant="rail" title="Recent orders" eyebrow={<>Simulated {book ? (book.ordersError ? <Fresh f="unavailable" label={`Orders read failed: ${book.ordersError}`} /> : <Fresh quiet f={book.fresh.freshness} label={mock ? book.fresh.label : `FLOPX paper desk · ${book.email}`} />) : <Fresh quiet f="illustrative" label="Seed orders: design examples, re-dated" />}</>} bodyClass="p-0"><OrdersList orders={orders.slice(0, 8)} /></Panel>
        <Panel variant="rail" title="Strategy target changes" eyebrow={<>CPX-1 · last 30 days <Fresh quiet f="illustrative" label="Design examples, re-dated" /></>}>
            {[
              // The design's worked examples keep their distance from "now" in sessions (0, 0, 1 and 6 weekdays back); see src/lib/designDates.ts.
              { d: fmtDM(weekdaysAgo(0)), t: 'VST', from: 9.3, to: 10.8, why: 'TX pressure anomaly', state: 'Awaiting approval' },
              { d: fmtDM(weekdaysAgo(0)), t: 'CRWV', from: 5.2, to: 3.2, why: 'Neocloud softening (exploratory)', state: 'Awaiting approval' },
              { d: fmtDM(weekdaysAgo(1)), t: 'VST', from: 8.3, to: 9.3, why: 'TX pressure anomaly (first leg)', state: 'Filled' },
              { d: fmtDM(weekdaysAgo(6)), t: 'AMD', from: 2.0, to: 0, why: 'No edge found — signal retired', state: 'Filled' },
            ].map((c, i) => (
              <div key={i} className="grid grid-cols-[48px_48px_1fr_auto] items-center gap-3 py-2 text-[12px]">
                <span className="num text-[10.5px] text-dim">{c.d}</span>
                <span className="font-mono">{c.t}</span>
                <span><span className="num">{c.from.toFixed(1)}% → {c.to.toFixed(1)}%</span> <span className="text-mute">· {c.why}</span></span>
                <span className={cx('text-[11px]', c.state === 'Filled' ? 'text-up' : 'text-dec')}>{c.state}</span>
              </div>
            ))}
        </Panel>
      </Row>
    </div>
  )
}

/** A signed-in reader's holdings in the design's table: the backend's marks and P&L, no owner, signal or day change (the paper desk does not report them), and no simulated actions on real rows. */
function LivePositionRows({ rows, nav, onInspect }: { rows: LivePosition[]; nav: number; onInspect: (t: string) => void }) {
  const dash = <span className="text-dim">—</span>
  return (
    <>
      {rows.map((p) => (
        <tr key={p.ticker} className="border-b border-line/60 hover:bg-hover/50">
          <td className="px-4 py-2"><button onClick={() => onInspect(p.ticker)} className="cursor-pointer font-mono hover:underline">{p.ticker}</button> <span className="text-dim">{secByTicker(p.ticker)?.name ?? mockNameOf(p.ticker)}</span>{p.qty < 0 && <span className="ml-1.5 rounded-[2px] border border-down/40 px-1 font-mono text-[9px] text-down">SHORT</span>}</td>
          <td className="px-4 py-2">{dash}</td>
          <td className="px-4 py-2">{dash}</td>
          <td className="num px-4 py-2 text-right">{p.qty.toLocaleString()}</td>
          <td className="num px-4 py-2 text-right text-mute">{p.avg.toFixed(2)}</td>
          <td className="num px-4 py-2 text-right" title={p.fresh.label ?? p.fresh.freshness}>{p.last != null ? p.last.toFixed(2) : dash}</td>
          <td className="num px-4 py-2 text-right">{p.mv != null ? <>{p.mv < 0 ? '−' : ''}${Math.abs(Math.round(p.mv)).toLocaleString()}</> : dash}</td>
          <td className="num px-4 py-2 text-right">{p.mv != null && nav ? `${((p.mv / nav) * 100).toFixed(1)}%` : dash}</td>
          <td className="px-4 py-2 text-right">{p.pnl != null ? <Delta v={p.pnl / 1000} d={1} suffix="k" /> : dash}</td>
          <td className="px-4 py-2 text-right">{dash}</td>
          <td className="px-4 py-2 text-right whitespace-nowrap"><span className="font-mono text-[10px] text-dim">{p.fresh.freshness === 'illustrative' ? 'mark: sample price' : `mark ${p.fresh.freshness}`}</span></td>
        </tr>
      ))}
    </>
  )
}

export function PositionsTable({ live, liveNav, onReduce, onTicket, onInspect }: { live?: LivePosition[]; liveNav?: number; onReduce: (t: string, f: number) => void; onTicket: (t: string) => void; onInspect: (t: string) => void }) {
  const { positions, nav, orders } = useStore()
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[12px]">
        <thead>
          <tr className="h-8 border-b border-line text-[11px] whitespace-nowrap text-dim">
            {['Instrument', 'Own', 'Signal', 'Qty', 'Avg ($)', 'Last ($)', 'Mkt value ($)', 'Weight', 'Unreal. P&L', 'Day', ''].map((h, i) => <th key={h + i} className={cx('px-3 font-medium', i >= 3 && i <= 9 ? 'text-right' : 'text-left')}>{h}</th>)}
          </tr>
        </thead>
        <tbody>
          {live && <LivePositionRows rows={live} nav={liveNav ?? nav} onInspect={onInspect} />}
          {!live && positions.map((p) => {
            const px = lastPx(p.ticker)
            const mv = p.qty * px
            const pnl = p.qty * (px - p.avg)
            const sig = p.signal ? signalById(p.signal) : null
            const pending = orders.some((o) => o.ticker === p.ticker && (o.status === 'Pending' || o.status === 'Queued'))
            return (
              <tr key={p.ticker} className="h-9 border-b border-line-soft hover:bg-hover/50">
                <td className="px-3 py-1"><button onClick={() => onInspect(p.ticker)} className="cursor-pointer font-mono hover:underline">{p.ticker}</button> <span className="text-dim">{secByTicker(p.ticker)!.name}</span>{p.qty < 0 && <span className="ml-1.5 text-[11px] text-down">short</span>}</td>
                <td className="px-3 py-1"><span title={p.strategy === 'User' ? 'Owner: User' : 'Owner: CPX-1 (model)'} className={cx('text-[11.5px]', p.strategy === 'User' ? 'text-bone' : 'text-dec')}>{p.strategy === 'User' ? 'U' : '◆'}</span></td>
                <td className="px-3 py-1">{sig ? <EntityLink r={{ kind: 'signal', id: sig.id, label: sig.name }} className="text-[11.5px] text-mute" /> : <span className="text-dim">—</span>}</td>
                <td className="num px-3 py-1 text-right">{p.qty.toLocaleString()}</td>
                <td className="num px-3 py-1 text-right text-mute">{p.avg.toFixed(2)}</td>
                <td className="num px-3 py-1 text-right">{px.toFixed(2)}</td>
                <td className="num px-3 py-1 text-right">{mv < 0 ? '−' : ''}{Math.abs(Math.round(mv)).toLocaleString()}</td>
                <td className="num px-3 py-1 text-right">{((mv / nav) * 100).toFixed(1)}%</td>
                <td className="px-3 py-1 text-right"><Delta v={pnl / 1000} d={1} suffix="k" /></td>
                <td className="px-3 py-1 text-right"><Delta v={((px - prevPx(p.ticker)) / prevPx(p.ticker)) * 100} /></td>
                <td className="px-3 py-1 text-right whitespace-nowrap">
                  {pending ? <span className="text-[11px] text-warn">order pending…</span> : (
                    <div className="flex justify-end gap-1">
                      <Btn size="sm" variant="ghost" title="Reduce by half" onClick={() => onReduce(p.ticker, 0.5)}>−50%</Btn>
                      <Btn size="sm" variant="ghost" title="Close position" aria-label={`Close ${p.ticker}`} onClick={() => onReduce(p.ticker, 1)}><X size={12} /></Btn>
                      <Btn size="sm" variant="ghost" title="Open order ticket" aria-label={`Ticket ${p.ticker}`} onClick={() => onTicket(p.ticker)}><Send size={12} /></Btn>
                    </div>
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export function OrdersList({ orders }: { orders: ReturnType<typeof useStore>['orders'] }) {
  const { cancelOrder } = useStore()
  if (!orders.length) return <div className="py-3 text-[12px] text-mute">No orders yet.</div>
  // Rows only: callers put them in a rail panel, which rules its direct children.
  return (
    <>
      {orders.map((o) => (
        <div key={o.id} className="grid grid-cols-[76px_1fr_auto] items-center gap-3 py-2">
          <span className="num text-[10.5px] text-dim">{o.time}</span>
          <div className="min-w-0">
            <div className="text-[12px]"><span className={cx('font-medium', o.side === 'Buy' || o.side === 'Cover' ? 'text-up' : 'text-down')}>{o.side}</span> <span className="num">{o.qty.toLocaleString()}</span> <span className="font-mono">{o.ticker}</span> <span className="text-dim">· {o.type}{o.limit ? ` @ ${o.limit}` : ''}</span>{o.fillPx && <span className="text-mute"> · filled ${o.fillPx.toFixed(2)}</span>}</div>
            <div className="truncate text-[11px] text-dim"><span className={o.origin === 'Model' ? 'text-dec' : 'text-mute'}>{o.origin === 'Model' ? 'Model · CPX-1' : 'User'}</span>{o.note && ` · ${o.note}`}{o.reason && <span className={o.status === 'Rejected' ? 'text-down' : 'text-warn'}> · {o.reason}</span>}</div>
          </div>
          <div className="flex items-center gap-2">
            <OrderStatus s={o.status} />
            {(o.status === 'Pending' || o.status === 'Queued') && <button onClick={() => cancelOrder(o.id)} className="cursor-pointer text-dim hover:text-bone" aria-label="Cancel order"><X size={12} /></button>}
          </div>
        </div>
      ))}
    </>
  )
}

export function OrderStatus({ s }: { s: string }) {
  return (
    <span className={cx('inline-flex items-center gap-1 text-[11px] font-medium whitespace-nowrap', (s === 'Queued' || s === 'Rejected') && 'rounded-[2px] border px-1.5 py-[1px]', s === 'Filled' && 'text-up', s === 'Pending' && 'text-dec', s === 'Queued' && 'border-warn/40 text-warn', s === 'Rejected' && 'border-down/40 text-down', s === 'Cancelled' && 'text-dim')}>
      {s === 'Queued' ? 'Queued · mkt closed' : s}
    </span>
  )
}

function DecisionRecordPanel({ rec, onClose }: { rec: (typeof DECISIONS)[number] | null; onClose: () => void }) {
  if (!rec) {
    return (
      <Panel variant="rail" title="Decision records" eyebrow={<span title="Every model decision stores a snapshot of its information set and forecast at the time, so it can be audited later.">What we knew · predicted · did · got</span>}>
        {DECISIONS.map((d) => <RecordLink key={d.id} d={d} />)}
      </Panel>
    )
  }
  const liveSig = SIGNALS.find((s) => s.asset === rec.prediction.asset)
  return (
    <Panel variant="callout" tone="dec" id="Decision records" className="fade-in" bodyClass="space-y-4"
      title={rec.title}
      eyebrow={<>{WEEKS[rec.week]} · {rec.mode === 'backtest' ? <span className="text-mute">backtest replay · hypothetical</span> : <span className="text-dec">paper</span>}</>}
      right={<button onClick={onClose} className="cursor-pointer text-dim hover:text-bone" aria-label="Close record"><X size={14} /></button>}>
        <div>
          <div className="eyebrow mb-1.5">1 · Information at the time</div>
          {rec.information.map((i) => <div key={i.label} className="flex items-center justify-between text-[12px]"><span className="flex items-center gap-1.5 text-mute"><TruthGlyph truth={i.truth} size={7} />{i.label}</span><span className="num">{i.value}</span></div>)}
        </div>
        <div>
          <div className="eyebrow mb-1.5 flex items-center gap-2">2 · Prediction <TruthTag truth="fct" compact /></div>
          <div className="flex items-center justify-between text-[12px]"><span>{rec.prediction.asset} · {rec.prediction.horizon}</span><span className="num text-fct">{rec.prediction.mean > 0 ? '+' : ''}{rec.prediction.mean}% <span className="text-dim">[{rec.prediction.p10}, {rec.prediction.p90}]</span></span></div>
          {/* The record is a design example; the validation badge is the live 1M status of the signal on this asset today (WF4 #3), never the design's. */}
          <div className="mt-1 flex items-center gap-2"><Validation s={liveSig ? liveSig.horizons['1M'].status : 'insufficient'} /><span className="font-mono text-[9.5px] text-dim" title={liveSig?.basisNote}>{liveSig ? 'status today, per the FLOPX evaluation' : 'no FLOPX evaluation of this asset'}</span></div>
        </div>
        <div>
          <div className="eyebrow mb-1.5 flex items-center gap-2">3 · Decision <TruthTag truth="dec" compact /></div>
          <div className="text-[12px]"><b>{rec.decision.action}</b> · {rec.decision.size}</div>
          <div className="text-[11px] text-dim">by {rec.decision.by}</div>
          <div className="mt-1 text-[12px] text-mute">{rec.decision.reasoning}</div>
        </div>
        <div className={cx('border-l-2 py-1 pl-3', rec.outcome.inBand === false ? 'border-down' : rec.outcome.inBand ? 'border-up' : 'border-dashed border-line2')}>
          <div className="eyebrow mb-1">4 · Outcome & attribution</div>
          {rec.outcome.realised != null && <div className="num text-[16px]"><Delta v={rec.outcome.realised} /> <span className="text-[11px] text-mute">{rec.outcome.inBand ? 'within band' : 'outside band'}</span></div>}
          <div className="text-[12px] text-bone/90">{rec.outcome.text}</div>
          <div className="mt-1 text-[11.5px] text-mute">{rec.outcome.attribution}</div>
        </div>
    </Panel>
  )
}
function RecordLink({ d }: { d: (typeof DECISIONS)[number] }) {
  const { setFund } = useStore()
  return (
    <button onClick={() => setFund({ record: d.id })} className="flex w-full cursor-pointer items-center gap-3 py-2 text-left hover:bg-hover">
      <span className={cx('size-2.5 rotate-45 border border-dec', d.mode === 'paper' && 'bg-dec')} />
      <span className="flex-1 text-[12px]">{d.title}</span>
      <span className="num text-[10.5px] text-dim">{WEEKS[d.week]}</span>
    </button>
  )
}

function SignalsTab() {
  const { fund, setFund, navigate, setXray, inspect } = useStore()
  const sig = signalById(fund.signal)!
  const f = sig.horizons[fund.horizon]
  const sec = secByTicker(sig.asset)
  const basket = BASKETS.find((b) => b.id === sig.asset)
  const hist = sec ? sec.px.slice(-30) : null
  const H = HZ_WEEKS[fund.horizon]
  const labels = hist ? [...WEEKS.slice(-30), ...Array.from({ length: H }, (_, k) => `+${k + 1}w`)] : []
  const lastV = hist ? hist[hist.length - 1] : 0
  const fcLine = hist ? [...hist.map((_, i) => (i === hist.length - 1 ? lastV : null)), ...Array.from({ length: H }, (_, k) => lastV * (1 + (f.mean / 100) * ((k + 1) / H)))] : []
  const lo = hist ? [...hist.map((_, i) => (i === hist.length - 1 ? lastV : null)), ...Array.from({ length: H }, (_, k) => lastV * (1 + (f.p10 / 100) * Math.sqrt((k + 1) / H)))] : []
  const hi = hist ? [...hist.map((_, i) => (i === hist.length - 1 ? lastV : null)), ...Array.from({ length: H }, (_, k) => lastV * (1 + (f.p90 / 100) * Math.sqrt((k + 1) / H)))] : []

  return (
    <Row cols={['300px', 1]} labels={['Signals', undefined]}>
      <div className="space-y-1.5">
        <div className="eyebrow mb-1 flex items-center gap-2 px-1">Signals · status at {fund.horizon} <Fresh quiet f="illustrative" /></div>
        {SIGNALS.map((x) => (
          <button key={x.id} onClick={() => setFund({ signal: x.id })} className={cx('w-full cursor-pointer rounded-md border p-3 text-left transition-colors', x.id === sig.id ? 'border-line2 bg-act/[0.06] shadow-[inset_2px_0_0_var(--color-act)]' : 'border-line bg-panel hover:bg-hover')}>
            <div className="flex items-start justify-between gap-2"><span className="text-[12.5px] font-medium">{x.name}</span><DirectionTag direction={x.direction} /></div>
            <div className="mt-0.5 text-[11px] text-dim">{x.assetLabel}</div>
            <div className="mt-2 flex items-center justify-between"><Validation s={x.horizons[fund.horizon].status} />{(x.basis === 'backend' || x.basis === 'pending') && x.horizons[fund.horizon].status === 'insufficient' ? <span className="font-mono text-[10.5px] text-dim" title={x.basisNote}>not issued</span> : <span className="num text-[11px] text-fct">{x.horizons[fund.horizon].mean > 0 ? '+' : ''}{x.horizons[fund.horizon].mean.toFixed(1)}%</span>}</div>
          </button>
        ))}
      </div>

      <div className="space-y-5">
        <Panel id="Signal detail" eyebrow="Signal detail" bodyClass="p-5">
          <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="eyebrow mb-1 flex items-center gap-2">{sig.cluster} · updated {sig.updated} <Fresh quiet f="illustrative" /></div>
              <h2 className="text-[22px] leading-tight font-semibold tracking-tight">{sig.name}</h2>
              <div className="mt-1.5 text-[12.5px] text-mute">{sig.direction} <button onClick={() => sec ? inspect({ kind: 'security', id: sec.ticker, label: sec.ticker }) : basket && inspect({ kind: 'basket', id: basket.id, label: basket.name })} className="cursor-pointer text-bone underline decoration-dotted underline-offset-2">{sig.assetLabel}</button></div>
            </div>
            <div className="text-right">
              <div className="eyebrow mb-1">Forecast horizon</div>
              <Seg value={fund.horizon} onChange={(h) => setFund({ horizon: h })} options={[{ id: '1W', label: '1W' }, { id: '1M', label: '1M' }, { id: '3M', label: '3M' }]} />
            </div>
          </div>
          <p className="mb-4 max-w-3xl text-[13px] leading-relaxed text-bone/90">{sig.summary}</p>

          <div className="grid grid-cols-1 gap-5 lg:grid-cols-[1fr_300px]">
            <div>
              {hist ? (
                <TimeChart labels={labels} height={200} nowIdx={hist.length - 1} yFmt={(v) => `$${v.toFixed(0)}`} forecastLabel={`${fund.horizon} forecast`} series={[
                  { id: 'px', label: `${sig.asset} (${(W2_LIVE.companies[sig.asset]?.observedWeeks ?? 0) >= 30 ? 'observed' : 'illustrative'})`, values: [...hist, ...Array(H).fill(null)], color: 'var(--color-obs)' },
                  { id: 'fc', label: 'Expected path', values: fcLine, color: 'var(--color-fct)', style: 'dashed', band: { lo, hi } },
                ]} />
              ) : (
                <div className="flex h-[200px] items-center justify-center rounded-md border border-dashed border-line2 text-[12px] text-mute">{basket ? `Basket forecast — members: ${basket.members.map((m) => m.t).join(', ')}` : 'Index-level forecast — see Evaluation tab for forecast vs actual'}</div>
              )}
            </div>
            <div className="rounded-md border border-dashed border-fct/30 p-3">
              <div className="mb-1 flex items-center justify-between"><TruthTag truth="fct" /><Validation s={f.status} /></div>
              <Distribution p10={f.p10} mean={f.mean} p90={f.p90} label={`${fund.horizon} return distribution (excess vs sector)`} />
              <div className="mt-3 grid grid-cols-3 gap-2 border-t border-line pt-2.5">
                <div><div className="text-[11px] text-dim font-medium">Hit rate</div><div className="num text-[14px]">{f.hit != null ? `${Math.round(f.hit * 100)}%` : '—'}</div><div className="text-[10.5px] text-dim">base {f.baselineHit != null ? `${Math.round(f.baselineHit * 100)}%` : '—'}</div></div>
                <div><div className="text-[11px] text-dim font-medium">IC</div><div className="num text-[14px]">{f.ic?.toFixed(2) ?? '—'}</div></div>
                <div><div className="text-[11px] text-dim font-medium">Windows</div><div className={cx('num text-[14px]', f.n < 12 && 'text-warn')}>{f.n}</div><div className="text-[10.5px] text-dim">independent</div></div>
              </div>
              {f.status === 'insufficient' && <div className="mt-2 rounded-[3px] border border-dashed border-line2 p-2 text-[11px] text-mute">{sig.basis === 'pending' ? 'Awaiting the FLOPX evaluation: no validation status has been issued for this signal yet. The distribution is the design\'s worked example.' : sig.basis === 'backend' ? `${sig.basisNote}: ${f.n} scored ${fund.horizon} forecasts, too few to judge.` : `Only ${f.n} non-overlapping ${fund.horizon} windows since data began.`} Shown for research, not used for sizing.</div>}
              {f.status === 'no-edge' && <div className="mt-2 rounded-[3px] border border-dashed border-line2 p-2 text-[11px] text-mute">No model edge found at this horizon: not better than baseline. CPX-1 takes no position based on it.</div>}
            </div>
          </div>
        </Panel>

        <Row cols={[1, 1]} at="lg">
          <Panel title="Features driving this forecast" eyebrow="Contribution to expected return · pp">
            <HBars signed rows={sig.features.map((x) => ({ label: <span className="flex items-center gap-1.5"><TruthGlyph truth={x.truth} size={7} />{x.name} <span className="text-dim">· {x.value}</span></span>, value: x.contribution, color: x.truth === 'inf' ? 'var(--color-inf)' : undefined, hatched: x.truth === 'inf' }))} fmt={(v) => `${v > 0 ? '+' : ''}${v.toFixed(1)}`} />
            {sig.regions.length > 0 && <div className="mt-3 flex flex-wrap gap-1.5">{sig.regions.map((r) => <Btn key={r} size="sm" variant="subtle" onClick={() => { setXray({ region: r, tab: 'brief' }); navigate('xray') }}>{regionById(r)!.short} in X-Ray <ArrowRight size={11} /></Btn>)}</div>}
          </Panel>
          <Panel title="Financial & fundamental context" eyebrow={<>Latest filings <Fresh quiet f="illustrative" /></>}>
            {sig.fundamentals.length ? <div className="grid grid-cols-2 gap-3">{sig.fundamentals.map((x) => <div key={x.k}><div className="text-[11px] text-dim font-medium">{x.k}</div><div className="num text-[14px]">{x.v}</div></div>)}</div> : <div className="text-[12px] text-mute">Not applicable for this asset.</div>}
            <div className="mt-4 space-y-2 border-t border-line pt-3">
              <div className="text-[12px]"><span className="eyebrow mr-2">News</span>{sig.news.text} <span className="text-[11px] text-dim">{sig.news.sources.join(' · ')}</span></div>
              <div className="text-[12px]"><span className="eyebrow mr-2">Prediction mkt</span>{sig.pm}</div>
            </div>
          </Panel>
        </Row>

        {sig.scenario.length > 0 && (
          <Foldable label="Scenarios">
          <AIBlock>
            <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
              {sig.scenario.map((x) => (
                <div key={x.name}>
                  <div className="mb-1 flex items-center justify-between"><span className="text-[12px] font-semibold">{x.name}</span><span className="num text-[11px] text-fct">{Math.round(x.prob * 100)}%</span></div>
                  <Meter value={x.prob} color="var(--color-fct)" className="mb-1.5" />
                  <div className="text-[11.5px] text-mute">{x.text}</div>
                </div>
              ))}
            </div>
            <div className="mt-2 text-[11px] text-dim">Scenario weights are subjective AI-synthesised judgements, not calibrated probabilities.</div>
          </AIBlock>
          </Foldable>
        )}

        <Row cols={[1, 1]} at="lg">
          <Panel variant="rail" title="Supporting evidence" eyebrow={<span className="text-up">For</span>}>
            {signalSupport(sig).length ? signalSupport(sig).map((x) => <div key={x} className="flex gap-2 py-1.5 text-[12px]"><Check size={13} className="mt-0.5 shrink-0 text-up" />{x}</div>) : <div className="py-1.5 text-[12px] text-mute">None.</div>}
          </Panel>
          <Panel variant="rail" title="Contradictory evidence" eyebrow={<span className="text-down">Against</span>}>
            {sig.contra.length ? sig.contra.map((x) => <div key={x} className="flex gap-2 py-1.5 text-[12px]"><X size={13} className="mt-0.5 shrink-0 text-down" />{x}</div>) : <div className="py-1.5 text-[12px] text-mute">None recorded.</div>}
          </Panel>
        </Row>
      </div>
    </Row>
  )
}

function DecisionsTab() {
  const { proposals, actOnProposal, modifyProposal, orders, marketOpen, inspect } = useStore()
  const [editing, setEditing] = useState<string | null>(null)
  return (
    <div className="space-y-5">
      <Foldable label="Approval policy">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-line bg-panel px-4 py-3">
        <div className="text-[12.5px] text-mute">CPX-1 proposes target changes and <b className="text-bone">you</b> approve them. Nothing executes without a decision. Every override is logged for later attribution. <Fresh quiet f="illustrative" /></div>
        <div className="flex items-center gap-2 text-[11.5px]"><span className={cx('size-1.5 rounded-full', marketOpen ? 'bg-up' : 'bg-warn')} />{marketOpen ? 'Market open · paper fills ~1.5s' : 'Market closed · orders will queue'}</div>
      </div>
      </Foldable>
      <Foldable label="Proposals">
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        {proposals.map((p) => {
          const sig = signalById(p.signal)!
          const order = orders.find((o) => o.proposalId === p.id)
          const q = p.modifiedQty ?? p.qty
          const change = p.targetW - p.currentW
          return (
            <div key={p.id} className={cx('rounded-md border border-line bg-panel', p.state === 'open' ? 'border-l-2 border-l-dec' : 'opacity-90')}>
              <div className="flex items-start justify-between border-b border-line px-4 py-3">
                <div>
                  <div className="mb-0.5 flex items-center gap-2"><TruthTag truth="dec" compact /><span className="text-[11px] text-dim">Proposal <span className="num">{p.id.toUpperCase()}</span> · CPX-1</span>{p.exploratory && <span className="rounded-[2px] border border-warn/40 px-1 text-[10.5px] text-warn">Exploratory · capped size</span>}</div>
                  <div className="text-[16px] font-semibold"><span className={p.side === 'Buy' || p.side === 'Cover' ? 'text-up' : 'text-down'}>{p.side}</span> <span className="num">{q.toLocaleString()}</span> <button onClick={() => inspect({ kind: 'security', id: p.ticker, label: p.ticker })} className="cursor-pointer font-mono hover:underline">{p.ticker}</button> <span className="text-[12px] font-normal text-mute">≈ ${Math.round(q * lastPx(p.ticker)).toLocaleString()}</span></div>
                </div>
                {p.state === 'open' ? <span className="text-[11px] font-medium text-dec">Awaiting decision</span> : p.state === 'rejected' ? <span className="text-[11px] text-dim">Rejected · override logged</span> : order && <OrderStatus s={order.status} />}
              </div>
              <div className="space-y-3 px-4 py-3">
                <div className="grid grid-cols-4 gap-2 text-center">
                  <div className="rounded-[4px] border border-line p-2"><div className="text-[11px] text-dim font-medium">Current</div><div className="num text-[14px]">{p.currentW.toFixed(1)}%</div></div>
                  <div className="rounded-[4px] border border-line p-2"><div className="text-[11px] text-dim font-medium">Target</div><div className="num text-[14px] text-dec">{p.targetW.toFixed(1)}%</div></div>
                  <div className="rounded-[4px] border border-line p-2"><div className="text-[11px] text-dim font-medium">Change</div><div className="num text-[14px]"><Delta v={change} d={1} suffix="pp" /></div></div>
                  <div className="rounded-[4px] border border-line p-2"><div className="text-[11px] text-dim font-medium">Risk budget</div><div className="num text-[14px]">{p.riskUse}%</div></div>
                </div>
                <div className="text-[12.5px] leading-relaxed text-bone/90">{p.reasoning}</div>
                <div className="flex items-center gap-2 text-[11.5px]"><span className="eyebrow">Signal</span><EntityLink r={{ kind: 'signal', id: sig.id, label: sig.name }} /><Validation s={sig.horizons['1M'].status} /></div>
                <div>
                  <div className="eyebrow mb-1">Risks</div>
                  {p.risks.map((r) => <div key={r} className="text-[11.5px] text-mute">· {r}</div>)}
                </div>
                {editing === p.id && (
                  <div className="flex items-center gap-2 rounded-[4px] border border-line2 p-2">
                    <span className="eyebrow">Modify qty</span>
                    <input type="number" min={1} value={q} onChange={(e) => modifyProposal(p.id, Math.max(1, +e.target.value))} className="num h-7 w-28 rounded-[3px] border border-line2 bg-ink px-2 text-[12px] outline-none" />
                    <span className="text-[11px] text-dim">model: <span className="num">{p.qty.toLocaleString()}</span></span>
                    <Btn size="sm" variant="ghost" onClick={() => setEditing(null)}>Done</Btn>
                  </div>
                )}
                {p.state === 'sent' && order && (
                  <div className="rounded-[4px] bg-ink p-2.5 text-[11.5px] text-mute">
                    {order.id} · {order.status === 'Filled' ? `Filled ${order.qty.toLocaleString()} @ $${order.fillPx?.toFixed(2)} — attribution will be tracked to ${sig.name}` : order.status === 'Rejected' ? <span className="text-down">Rejected: {order.reason}</span> : order.status === 'Queued' ? <span className="text-warn">{order.reason}</span> : 'Routing to paper venue…'}
                  </div>
                )}
              </div>
              {p.state === 'open' && (
                <div className="flex gap-2 border-t border-line px-4 py-3">
                  <Btn variant="decision" size="sm" onClick={() => { actOnProposal(p.id, 'send'); setEditing(null) }}><Send size={12} /> Send to paper</Btn>
                  <Btn size="sm" onClick={() => setEditing(editing === p.id ? null : p.id)}><Pencil size={12} /> Modify</Btn>
                  <Btn size="sm" variant="danger" onClick={() => actOnProposal(p.id, 'reject')}>Reject</Btn>
                </div>
              )}
            </div>
          )
        })}
      </div>
      </Foldable>
      <Panel title="Attribution · closed decisions" eyebrow={<>Realised P&L by originating signal <Fresh quiet f="illustrative" /></>}>
        <div className="grid grid-cols-[1fr_80px_90px_100px_1fr] gap-3 border-b border-line pb-1.5 text-[11px] text-dim font-medium"><span>Decision</span><span>Origin</span><span className="text-right">P&L</span><span className="text-right">Signal share</span><span>Note</span></div>
        {[
          { d: `Close AMD (${fmtDM(weekdaysAgo(6))})`, o: 'Model', pnl: 41250, share: '—', note: 'Signal retired: no edge found. P&L is market beta, not skill.' },
          { d: `Buy VST first leg (${fmtDM(weekdaysAgo(1))})`, o: 'Model', pnl: null, share: 'pending', note: `1M horizon resolves ${fmtDMY(daysAgo(-30, weekdaysAgo(1)))}` },
          { d: `MSFT (user, ${fmtDM(WEEK_DATES[PAPER_START_WK])})`, o: 'User', pnl: null, share: 'n/a', note: 'User-initiated; excluded from strategy attribution' },
        ].map((r) => (
          <div key={r.d} className="grid grid-cols-[1fr_80px_90px_100px_1fr] items-center gap-3 border-b border-line-soft py-2 text-[12px]">
            <span>{r.d}</span><span className={cx('text-[11.5px]', r.o === 'Model' ? 'text-dec' : 'text-mute')}>{r.o}</span>
            <span className="text-right">{r.pnl != null ? <Delta v={r.pnl / 1000} d={1} suffix="k" /> : <span className="text-dim">open</span>}</span>
            <span className="num text-right text-mute">{r.share}</span><span className="text-[11.5px] text-mute">{r.note}</span>
          </div>
        ))}
      </Panel>
    </div>
  )
}

function EvaluationTab() {
  const { fund, setFund } = useStore()
  return (
    <div className="space-y-5">
      <Foldable label="Method">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="max-w-2xl text-[12.5px] text-mute">Walk-forward out-of-sample forecasts vs baselines, with ablations and costs. Validated and exploratory results are kept apart.</p>
        <Seg value={fund.horizon} onChange={(h) => setFund({ horizon: h })} options={[{ id: '1W', label: '1W' }, { id: '1M', label: '1M' }, { id: '3M', label: '3M' }]} />
      </div>
      </Foldable>
      <ModelComparison horizon={fund.horizon} />
      <IcSharpe horizon={fund.horizon} />
      <Row cols={[1.4, 1]}>
        <ForecastVsActual horizon={fund.horizon} />
        <BacktestCosts />
      </Row>
      <Row cols={[1, 1]}>
        <ValidationMatrix />
        <Failures />
      </Row>
    </div>
  )
}
