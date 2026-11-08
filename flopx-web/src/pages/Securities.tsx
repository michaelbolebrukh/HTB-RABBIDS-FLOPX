import { useMemo, useState } from 'react'
import { Landmark } from 'lucide-react'
import { regionById } from '@/data/core'
import { BASKETS, basketSeries, chg, GROUP_COLOR, last, NEWS, PRED, SECURITIES, secBars, secByTicker, WEEKS, type Group } from '@/data/finance'
import { lastPx, SIGNALS } from '@/data/fund'
import { TimeChart } from '@/components/charts'
import { StockChart } from '@/components/StockChart'
import { ChartTypeMenu } from '@/components/ChartTypeMenu'
import { IndicatorChips, IndicatorMenu } from '@/components/IndicatorMenu'
import type { IndicatorId } from '@/lib/indicators'
import type { ChartType } from '@/lib/chartTypes'
import { Btn, cx, Delta, Empty, EntityLink, Fresh, freshLabel, HorizonStatus, Panel, ResetLayout, Seg, Spark, TruthTag, Validation } from '@/components/ui'
import { useStore } from '@/lib/store'
import { closeBadgeOf, W2_LIVE } from '@/live/adapt-w2'
import { fmtDay } from '@/live/provenance'
import { QuarterlySection } from '@/components/Fundamentals'

const GROUPS: ('All' | Group | 'Baskets')[] = ['All', 'Chips', 'Hyperscaler', 'Neocloud', 'DC Operator', 'Power', 'Baskets']
const RANGES = { '3M': 13, '6M': 26, '1Y': 52, '2Y': 115 } as const
// News events mapped to weekly index for chart annotations
const NEWS_WK: Record<string, number> = { n1: 111, n2: 113, n3: 114, n4: 114, n5: 105, n6: 108, n7: 114, n8: 110 }

export default function Securities() {
  const { route, navigate } = useStore()
  // Default to a ticker the backend covers (SEC fundamentals for NVDA, CRWV, NBIS, IREN, APLD), so
  // the page opens on something with a real record rather than on VST, which FLOPX has no data for.
  const sel = route.params.t ?? 'NVDA'
  const [group, setGroup] = useState<(typeof GROUPS)[number]>('All')
  const list = SECURITIES.filter((s) => s.group !== 'Index' && (group === 'All' || s.group === group))
  // Each row's status comes from the provenance of the close it shows (WF7 #1). The list says its
  // status once, as a quiet note in the eyebrow; only a row whose close differs (an archived or live
  // close) keeps a chip, the N-BADGE rule applied here (WF8 #3).
  const badges = Object.fromEntries(list.map((s) => [s.ticker, closeBadgeOf(W2_LIVE.companies[s.ticker])]))
  const anyObserved = Object.values(badges).some((b) => b.f !== 'illustrative')
  return (
    <div className="grid grid-cols-1 gap-5 xl:grid-cols-[280px_1fr]">
      <div>
        {/* The note is short because the column is 280 px: "unless marked" is the row's own chip. */}
        <div className="eyebrow mb-2 flex flex-wrap items-center gap-x-2 gap-y-0.5"><span className="flex items-center gap-2"><Landmark size={12} />Equities, funds & derivatives</span><Fresh quiet f="illustrative" label={anyObserved ? 'Illustrative unless marked' : undefined} /></div>
        <div className="mb-2 flex flex-wrap gap-1" role="radiogroup" aria-label="Category">
          {GROUPS.map((g) => {
            const c = g === 'All' ? 'var(--color-bone)' : GROUP_COLOR[g]
            const on = group === g
            return (
              <button key={g} role="radio" aria-checked={on} onClick={() => setGroup(g)}
                className={cx('flex cursor-pointer items-center gap-1.5 rounded-[4px] border px-2 py-1 text-[11px] transition-colors', on ? 'text-bone' : 'border-line text-mute hover:border-line2 hover:text-bone')}
                style={on ? { borderColor: c, background: `color-mix(in oklab, ${c} 14%, transparent)` } : undefined}>
                {g !== 'All' && <span className="inline-block size-[7px] shrink-0 rounded-[1px]" style={{ background: c }} />}
                {g}
              </button>
            )
          })}
        </div>
        <div className="overflow-hidden rounded-md border border-line bg-panel">
          {group !== 'Baskets' && list.map((s) => (
            <button key={s.ticker} onClick={() => navigate('securities', { t: s.ticker })} className={cx('grid w-full cursor-pointer grid-cols-[52px_1fr_auto] items-center gap-x-2 gap-y-0.5 border-b border-line/60 px-3 py-2 text-left last:border-0', sel === s.ticker ? 'bg-raise' : 'hover:bg-hover')}>
              <span className="row-span-2 font-mono text-[12px]">{s.ticker}</span>
              <Spark values={s.px.slice(-26)} width={70} height={16} color={GROUP_COLOR[s.group]} />
              <span className="flex items-baseline justify-end gap-1.5"><span className="num text-[11.5px]">{last(s.px).toFixed(2)}</span><Delta v={chg(s.px)} className="text-[10px]" /></span>
              {/* Only a row whose close differs from the list's note keeps a chip; it spans the spark and price columns, since "Archived · 06 Nov" does not fit beside the price in a 280 px list. */}
              {badges[s.ticker].f !== 'illustrative' && <span className="col-span-2 col-start-2 justify-self-end" title={badges[s.ticker].title}><Fresh f={badges[s.ticker].f} label={badges[s.ticker].label} className="text-[8.5px]" /></span>}
            </button>
          ))}
          {(group === 'All' || group === 'Baskets') && (
            <>
              <div className="eyebrow border-b border-line bg-ink px-3 py-1.5">Baskets</div>
              {BASKETS.map((b) => { const sr = basketSeries(b); return (
                <button key={b.id} onClick={() => navigate('securities', { t: b.id })} className={cx('grid w-full cursor-pointer grid-cols-[1fr_auto] items-center gap-2 border-b border-line/60 px-3 py-2 text-left last:border-0', sel === b.id ? 'bg-raise' : 'hover:bg-hover')}>
                  <span className="flex items-center gap-2 text-[12px]">{b.name}<Spark values={sr.slice(-26)} width={44} height={14} color={GROUP_COLOR.Baskets} /></span><Delta v={chg(sr)} className="text-[10.5px]" />
                </button>
              ) })}
            </>
          )}
        </div>
      </div>
      {sel.startsWith('b-') ? <BasketView id={sel} /> : <SecurityView t={sel} />}
    </div>
  )
}

function SecurityView({ t }: { t: string }) {
  const { setTicket, inspect, positions, navigate, setXray, live } = useStore()
  const [range, setRange] = useState<keyof typeof RANGES>('1Y')
  const [chartType, setChartType] = useState<ChartType>('candles')
  const [indicators, setIndicators] = useState<IndicatorId[]>([])
  const s = secByTicker(t)
  const maxDD = useMemo(() => { if (!s) return 0; let pk = -Infinity, dd = 0; for (const v of s.px.slice(-52)) { pk = Math.max(pk, v); dd = Math.min(dd, (v - pk) / pk) } return dd * 100 }, [s, live])
  if (!s) return <Empty title="Unknown instrument" body="Search for a ticker with ⌘K." />
  const n = RANGES[range]
  const off = s.px.length - n
  const px = s.px.slice(-n)
  const bars = secBars(s)
  const news = NEWS.filter((x) => x.tickers.includes(t))
  const pms = PRED.filter((x) => x.tickers.includes(t))
  const peers = SECURITIES.filter((x) => x.group === s.group && x.group !== 'Index')
  const sigs = SIGNALS.filter((x) => x.asset === t || BASKETS.find((b) => b.id === x.asset)?.members.some((m) => m.t === t))
  const pos = positions.find((p) => p.ticker === t)
  // Backend coverage for this ticker: observed weekly closes (only when the backend has market data)
  // and SEC EDGAR revenue growth. Everything else on this page is the design's illustrative data.
  const lc = W2_LIVE.companies[t]
  // The headline close, its chip and the candle readout read one series: the close placed in the
  // latest week (the series read, the same point the list row is badged from) when one landed; the
  // companies list's latest close otherwise. The two payloads can tag the same close differently
  // (the list said live where the series said archived, WF8 #4), so the placed one wins.
  const placed = lc?.placed?.latestWeek ? lc.placed : null
  const observed = placed ? { t: placed.t, fresh: placed.fresh, source: placed.source as string | null } : lc && lc.observedWeeks > 0 && lc.close ? { t: lc.close.t, fresh: lc.close.fresh, source: null } : null
  const rg = lc?.revGrowth ?? null
  const peersSec = peers.some((p) => W2_LIVE.companies[p.ticker]?.revGrowth)
  // The candle readout names the series its close comes from: for a week that carries an observed
  // close, the archive (one source, the backend's); the caption says the OHLC around it is drawn.
  const realWeeks = lc?.realWeeks ?? []
  const closeNote = observed && realWeeks.length
    ? (i: number) => (realWeeks[off + i] ? <span className="text-dim"> · close {freshLabel(observed.fresh.freshness).toLowerCase()} · {observed.source ?? 'FLOPX'}</span> : null)
    : undefined
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <div className="eyebrow mb-1"><span style={{ color: GROUP_COLOR[s.group] }}>{s.group}</span> · {s.exchange}</div>
          <h1 className="font-serif text-[34px] leading-none">{s.name} <span className="font-mono text-[18px] text-mute">{s.ticker}</span></h1>
          <div className="mt-2 flex items-center gap-3"><span className="num text-[24px]">${last(s.px).toFixed(2)}</span><Delta v={chg(s.px)} className="text-[14px]" /><span className="text-[11px] text-dim">1W</span>{observed ? <span title={`${observed.source ?? 'FLOPX'} close ${fmtDay(observed.t)}`}><Fresh f={observed.fresh.freshness} label={`${freshLabel(observed.fresh.freshness)} · close ${fmtDay(observed.t)}`} /></span> : <Fresh f="illustrative" />}{pos && <span className="rounded-[3px] border border-dec/40 px-1.5 font-mono text-[10px] text-dec">PAPER {pos.qty > 0 ? 'LONG' : 'SHORT'} {Math.abs(pos.qty).toLocaleString()}</span>}</div>
        </div>
        <div className="flex items-center gap-2"><ResetLayout /><Btn variant="decision" onClick={() => setTicket({ ticker: t })}>Paper trade</Btn></div>
      </div>

      <Panel title="Price history" eyebrow={observed ? `Weekly · ◆ = linked events (click) · closes observed for the last ${lc!.observedWeeks} weeks, earlier closes, OHLC and volume illustrative` : 'Weekly · ◆ = linked events (click) · closes, OHLC and volume illustrative'}
        right={<div className="flex items-center gap-2"><Seg size="sm" value={range} onChange={setRange} options={Object.keys(RANGES).map((k) => ({ id: k as keyof typeof RANGES, label: k }))} /><IndicatorMenu value={indicators} onChange={setIndicators} /><ChartTypeMenu value={chartType} onChange={setChartType} /></div>}>
        <IndicatorChips value={indicators} onChange={setIndicators} />
        <StockChart
          labels={WEEKS.slice(-n)}
          candles={bars.candles.slice(-n)}
          volume={bars.volume.slice(-n)}
          drawdown={bars.drawdown.slice(-n)}
          type={chartType}
          indicators={indicators}
          history={bars}
          priceLabel={s.ticker}
          volumeLabel={s.group === 'Index' ? 'Volume · not traded (index)' : 'Volume · $ traded'}
          note={closeNote}
          fmtPrice={(v) => `$${v >= 1000 ? v.toFixed(0) : v.toFixed(2)}`}
          fmtVolume={(v) => (v >= 1e9 ? `$${(v / 1e9).toFixed(1)}B` : `$${(v / 1e6).toFixed(0)}M`)}
          lines={[{ id: 'px', label: s.ticker, values: px, color: GROUP_COLOR[s.group], width: 1.8 }]}
          markers={news.filter((x) => NEWS_WK[x.id] >= off).map((x) => ({ i: NEWS_WK[x.id] - off, id: x.id, label: x.title }))}
          onMarker={(id) => inspect({ kind: 'news', id, label: NEWS.find((x) => x.id === id)!.title })}
        />
      </Panel>

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-3">
        <Panel title="Fundamentals" eyebrow={<Fresh quiet f="illustrative" label={rg && lc?.quarterly ? 'Illustrative except rev growth & quarterlies' : rg ? 'Illustrative except rev growth' : lc?.quarterly ? 'Illustrative except quarterlies' : undefined} />}>
          <div className="grid grid-cols-2 gap-3">
            {[['Market cap', s.mcap ? `$${(s.mcap / 1e9).toFixed(0)}B` : '—'], ['P/E (fwd)', s.pe != null ? s.pe.toFixed(0) : 'n/m'], ['EV/Sales', `${s.evSales}×`], ['Rev growth', `${s.revGrowth}%`], ['Gross margin', `${s.grossMargin}%`], ['Short interest', `${s.shortInt}%`]].map(([k, v]) => <div key={k}><div className="text-[11px] text-dim font-medium">{k}</div><div className="num text-[15px]">{v}</div></div>)}
          </div>
          <div className="mt-3 text-[11.5px] text-mute">{s.description}</div>
          {rg && <div className="mt-1.5 flex items-start gap-1.5 text-[11px] text-mute"><TruthTag truth="obs" compact /><span title={`Period to ${fmtDay(rg.latest.periodEnd)} against the same period a year earlier`}>Rev growth: SEC {rg.latest.form} filed {fmtDay(rg.latest.filedAt)}, year on year</span></div>}
          {/* Signed-in readers: the archived WRDS Compustat quarterlies the detail read carries with a token (WF7 #2); nothing for anyone else. */}
          <QuarterlySection quarterly={lc?.quarterly} />
        </Panel>
        <Panel title="Derivatives" eyebrow={<>Listed options <Fresh quiet f="illustrative" /></>}>
          {s.iv30 == null ? (
            <Empty title="Options data unavailable" body={`No reliable listed-options feed for ${s.ticker}. FLOPX shows nothing rather than estimating implied volatility.`} />
          ) : (
            <>
              <div className="grid grid-cols-2 gap-3">
                {[['IV 30d', `${s.iv30}%`], ['RV 30d', `${s.vol30}%`], ['25Δ skew', `${s.ivSkew} vol pts`], ['Put/Call (OI)', `${s.putCall}`]].map(([k, v]) => <div key={k}><div className="text-[11px] text-dim font-medium">{k}</div><div className="num text-[15px]">{v}</div></div>)}
              </div>
              <div className="mt-3">
                <div className="eyebrow mb-1">IV term structure</div>
                <div className="flex h-12 items-end gap-1.5">{[1, 2, 3, 6, 9, 12].map((m, i) => { const iv = s.iv30! * (1 - i * 0.025); return <div key={m} className="flex flex-1 flex-col items-center gap-0.5"><div className="w-full rounded-t-sm bg-mute/50" style={{ height: `${(iv / (s.iv30! * 1.05)) * 40}px` }} /><span className="font-mono text-[8.5px] text-dim">{m}M</span></div> })}</div>
              </div>
            </>
          )}
        </Panel>
        <Panel title="Risk" eyebrow={<>Trailing <Fresh quiet f="illustrative" /></>}>
          <div className="grid grid-cols-2 gap-3">
            {[['Beta (SPX)', s.beta.toFixed(2)], ['Vol 30d', `${s.vol30}%`], ['Max DD 1Y', `${maxDD.toFixed(1)}%`], ['52w range', `${Math.min(...s.px.slice(-52)).toFixed(0)}–${Math.max(...s.px.slice(-52)).toFixed(0)}`]].map(([k, v]) => <div key={k}><div className="text-[11px] text-dim font-medium">{k}</div><div className="num text-[15px]">{v}</div></div>)}
          </div>
        </Panel>
      </div>

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-2">
        <Panel title="Compute-market intelligence" eyebrow="Documented ties (solid) vs modelled sensitivities (dashed)" right={<Fresh quiet f="illustrative" />}>
          {s.computeLinks.length === 0 && <div className="text-[12px] text-mute">No compute links.</div>}
          {s.computeLinks.map((l, i) => (
            <div key={i} className={cx('mb-2 rounded-[4px] border p-2.5 text-[12px]', l.kind === 'documented' ? 'border-line' : 'border-dashed border-inf/35')}>
              <div className="mb-0.5 flex items-center justify-between"><span className="eyebrow">{l.kind}</span>{l.region && <button onClick={() => { setXray({ region: l.region!, tab: 'exposure' }); navigate('xray') }} className="cursor-pointer font-mono text-[10.5px] text-inf hover:underline">{regionById(l.region)!.name} in X-Ray →</button>}</div>
              {l.text}
            </div>
          ))}
          {sigs.length > 0 && <div className="mt-3"><div className="eyebrow mb-1">Strategy signals</div>{sigs.map((x) => <div key={x.id} className="flex items-center justify-between py-0.5"><EntityLink r={{ kind: 'signal', id: x.id, label: x.name }} className="text-[12px]" /><Validation s={x.horizons['1M'].status} /></div>)}</div>}
        </Panel>
        <Panel title="News & prediction markets" eyebrow="Linked to this instrument">
          {news.length + pms.length === 0 && <div className="text-[12px] text-mute">No linked events in the last 90 days.</div>}
          {news.map((x) => <button key={x.id} onClick={() => navigate('news', { id: x.id })} className="block w-full cursor-pointer border-b border-line/60 py-2 text-left hover:bg-hover"><div className="text-[11px] text-dim font-medium">{x.type} · {x.ago} ago · {x.verification}</div><div className="text-[12px]">{x.title}</div></button>)}
          {pms.map((x) => <button key={x.id} onClick={() => navigate('news', { id: x.id })} className="flex w-full cursor-pointer items-center justify-between border-b border-line/60 py-2 text-left hover:bg-hover"><span className="text-[12px]"><span className="mr-1.5 font-mono text-[9.5px] text-fct">PM</span>{x.question}</span><span className="num ml-2 text-fct">{Math.round(x.prob * 100)}%</span></button>)}
        </Panel>
      </div>

      <Panel title="Comparable companies" eyebrow={<>{s.group} <Fresh quiet f="illustrative" label={peersSec ? 'Illustrative except rev. growth (SEC)' : undefined} /></>} bodyClass="p-0">
        <table className="w-full text-[12px]">
          <thead><tr className="border-b border-line text-[11px] text-dim font-medium">{['Ticker', 'Price', '1W', 'Mkt cap', 'P/E', 'EV/S', 'Rev g.', 'Beta', 'IV30'].map((h, i) => <th key={h} className={cx('px-4 py-2 font-normal', i ? 'text-right' : 'text-left')}>{h}</th>)}</tr></thead>
          <tbody>{peers.map((p) => (
            <tr key={p.ticker} onClick={() => navigate('securities', { t: p.ticker })} className={cx('cursor-pointer border-b border-line/60 hover:bg-hover', p.ticker === t && 'bg-raise')}>
              <td className="px-4 py-2 font-mono">{p.ticker}</td><td className="num px-4 py-2 text-right">{lastPx(p.ticker).toFixed(2)}</td><td className="px-4 py-2 text-right"><Delta v={chg(p.px)} /></td>
              <td className="num px-4 py-2 text-right">${(p.mcap / 1e9).toFixed(0)}B</td><td className="num px-4 py-2 text-right">{p.pe ?? 'n/m'}</td><td className="num px-4 py-2 text-right">{p.evSales}×</td><td className="num px-4 py-2 text-right">{p.revGrowth}%</td><td className="num px-4 py-2 text-right">{p.beta}</td><td className="num px-4 py-2 text-right">{p.iv30 ?? '—'}</td>
            </tr>
          ))}</tbody>
        </table>
      </Panel>
    </div>
  )
}

function BasketView({ id }: { id: string }) {
  const { navigate } = useStore()
  const b = BASKETS.find((x) => x.id === id)!
  const sr = basketSeries(b)
  const sig = SIGNALS.find((x) => x.asset === id)
  return (
    <div className="space-y-5">
      <div>
        <div className="eyebrow mb-1">Basket · cluster research</div>
        <h1 className="font-serif text-[34px] leading-none">{b.name}</h1>
        <p className="mt-2 max-w-2xl text-[13px] text-mute">{b.thesis}</p>
        <div className="mt-2"><ResetLayout /></div>
      </div>
      <Panel title="Basket index" eyebrow={<>Rebased to 100 · weekly · fixed weights <Fresh quiet f="illustrative" /></>}>
        <TimeChart labels={WEEKS} height={220} yFmt={(v) => v.toFixed(0)} series={[{ id: 'b', label: b.name, values: sr, color: 'var(--color-obs)', area: true }, ...b.members.map((m, i) => ({ id: m.t, label: m.t, values: secByTicker(m.t)!.px.map((p, k, a) => (p / a[0]) * 100), color: ['var(--color-dim)', 'var(--color-dim)', 'var(--color-line2)'][i % 3], width: 1 }))]} legend />
      </Panel>
      <div className="grid grid-cols-1 gap-5 xl:grid-cols-2">
        <Panel title="Members" bodyClass="p-0">
          {b.members.map((m) => { const s = secByTicker(m.t)!; return (
            <button key={m.t} onClick={() => navigate('securities', { t: m.t })} className="grid w-full cursor-pointer grid-cols-[60px_1fr_60px_60px] items-center gap-3 border-b border-line/60 px-4 py-2 text-left hover:bg-hover">
              <span className="font-mono">{m.t}</span><span className="text-[12px] text-mute">{s.name}</span><span className="num text-right text-[12px]">{(m.w * 100).toFixed(0)}%</span><Delta v={chg(s.px)} className="text-right text-[11px]" />
            </button>
          ) })}
        </Panel>
        <Panel title="Linked signal" eyebrow={<Fresh quiet f="illustrative" />}>
          {sig ? <><div className="mb-2 text-[13px] font-medium">{sig.name}</div><HorizonStatus horizons={sig.horizons} /><p className="mt-2 text-[12px] text-mute">{sig.summary}</p></> : <div className="text-[12px] text-mute">No strategy signal targets this basket.</div>}
        </Panel>
      </div>
    </div>
  )
}
