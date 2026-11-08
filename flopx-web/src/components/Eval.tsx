import { MONTHS, NOW_IDX } from '@/data/core'
import { BACKTEST_STATS, FAILURES, H100_ACTUAL, H100_FC, LOW_INFO, MODEL_COMPARE, MODEL_COMPARE_META, SIGNALS, type HorizonId } from '@/data/fund'
import { backtestSpanLong } from '@/data/finance'
import { W2_LIVE } from '@/live/adapt-w2'
import { TimeChart } from './charts'
import { cx, Fresh, Panel, TruthGlyph, Validation } from './ui'

/** Badge for a panel fed by the backend evaluation (badge and words follow the payload's price basis: sample closes, or archived WRDS CRSP closes) or by design data. */
function EvalFresh({ backend }: { backend: boolean }) {
  const basis = W2_LIVE.evaluation?.priceBasis
  if (backend && basis) return <Fresh quiet f={basis.freshness} label={basis.label} />
  return <Fresh quiet f="illustrative" label={backend ? 'Illustrative · sample closes' : undefined} />
}
/** "scored on WRDS CRSP closes through 2025-12-31" / "scored on authored sample closes, not market data". */
const basisCaption = () => W2_LIVE.evaluation?.priceBasis.caption ?? 'scored on authored sample closes, not market data'
const signed = (v: number, d = 0) => `${v >= 0 ? '+' : '−'}${Math.abs(v).toFixed(d)}`

export function ForecastVsActual({ horizon }: { horizon: HorizonId }) {
  const h = horizon === '3M' ? '3M' : '1M'
  const fc = H100_FC[h]
  const labels = MONTHS.slice(0, NOW_IDX + 1)
  const misses = H100_ACTUAL.map((a, i) => (fc.compute[i] != null && Math.abs((fc.compute[i]! - a) / a) > (h === '1M' ? 0.08 : 0.12) ? i : -1)).filter((i) => i >= 0)
  return (
    <Panel
      title={<>H100 index · forecasts made {h} earlier vs what happened</>}
      id="H100 forecasts vs actual"
      eyebrow={<><TruthGlyph truth="fct" size={7} />Out-of-sample · walk-forward <Fresh quiet f="illustrative" /></>}
      right={horizon === '1W' && <span className="text-[11px] text-dim">1W shown at 1M granularity</span>}
    >
      <TimeChart
        labels={labels}
        height={220}
        legend
        shade={LOW_INFO}
        yFmt={(v) => `$${v.toFixed(2)}`}
        annotations={misses.map((i) => ({ i, id: `m${i}`, label: `Miss: ${labels[i]}`, color: 'var(--color-down)', hollow: true }))}
        series={[
          { id: 'actual', label: 'Actual (illustrative)', values: H100_ACTUAL, color: 'var(--color-obs)', width: 2 },
          { id: 'base', label: 'Baseline forecast', values: fc.base, color: 'var(--color-mute)', style: 'dotted' },
          { id: 'comp', label: '+ Compute data forecast', values: fc.compute, color: 'var(--color-fct)', style: 'dashed' },
        ]}
      />
      <div className="mt-2 flex flex-wrap gap-4 text-[11px] text-dim">
        <span><span className="text-down">◇</span> forecast missed band ({misses.length})</span>
        <span><span className="text-warn">▮</span> low-information period — forecasts withheld or flagged</span>
      </div>
    </Panel>
  )
}

/** The harness table before the backend evaluation lands, or after it times out: the design's MAE, hit rates and "Validated" statuses are never shown (WF4 #6). */
const NOT_LOADED = 'not loaded · awaiting the FLOPX evaluation'

export function ModelComparison({ horizon }: { horizon: HorizonId }) {
  const rows = MODEL_COMPARE[horizon]
  const meta = MODEL_COMPARE_META
  const live = meta.basis === 'backend' ? W2_LIVE.evaluation : null
  const loaded = !!live
  const maxHit = 0.7
  const hzLabel = live && meta.horizonDays ? `${horizon} = ${meta.horizonDays[horizon]} trading days` : `${horizon} horizon`
  return (
    <Panel title="Does compute data improve predictions?" eyebrow={<>Baseline vs + compute vs + news events {loaded ? <EvalFresh backend /> : <Fresh quiet f="unavailable" label="Not loaded · awaiting the FLOPX evaluation" />}</>} right={<span className="text-[11px] text-dim" title={live ? `Backend harness, ${basisCaption()}` : 'The FLOPX evaluation has not answered yet; no figure is shown until it does'}>{hzLabel} · {loaded ? meta.scope : NOT_LOADED}</span>}>
      <div className="grid h-8 grid-cols-[1.6fr_repeat(5,minmax(0,1fr))_auto] items-center gap-x-3 border-b border-line text-[11px] font-medium text-dim">
        <span>Model</span><span className="text-right">MAE {loaded ? meta.maeUnit : ''}</span><span>Hit rate</span><span className="text-right">IC</span><span className="text-right">Sharpe net</span><span className="text-right">n · CI</span><span>Status</span>
      </div>
      {rows.map((r, i) => (
        <div key={r.model} className={cx('grid grid-cols-[1.6fr_repeat(5,minmax(0,1fr))_auto] items-center gap-x-3 border-b border-line-soft py-1.5', (!loaded || r.status === 'insufficient') && 'opacity-70')}>
          <span className="text-[12px]"><span className="num mr-2 text-[10.5px] text-dim">{String.fromCharCode(65 + i)}</span>{loaded ? r.model : ['Trailing-return baseline', '+ Compute-market data', '+ News event intensity'][i] ?? r.model}</span>
          <span className="num text-right text-[12px]">{loaded ? r.mae?.toFixed(3) ?? '—' : '—'}</span>
          <span className="flex items-center gap-2">
            <span className="relative h-2 flex-1 rounded-sm bg-line">
              {loaded && r.hit != null && <span className="absolute inset-y-0 left-0 rounded-sm" style={{ width: `${(r.hit / maxHit) * 100}%`, background: i === 0 ? 'var(--color-mute)' : 'var(--color-fct)', opacity: r.status === 'validated' ? 1 : 0.5 }} />}
              <span className="absolute inset-y-[-2px] w-px bg-down/70" style={{ left: `${(0.5 / maxHit) * 100}%` }} title="Coin flip (50%)" />
            </span>
            <span className="num w-8 text-right text-[11px]">{loaded && r.hit != null ? `${Math.round(r.hit * 100)}%` : '—'}</span>
          </span>
          <span className="num text-right text-[12px]">{loaded ? r.ic?.toFixed(2) ?? '—' : '—'}</span>
          <span className="num text-right text-[12px]">{loaded ? r.sharpe?.toFixed(2) ?? '—' : '—'}</span>
          <span className="num text-right text-[11px] text-mute">{loaded ? `${r.n} · ${r.ci}` : '—'}</span>
          {!loaded ? <span className="text-[11px] text-dim" title={NOT_LOADED}>not loaded</span> : r.status === 'reference' ? <span className="text-[11px] text-dim" title="The yardstick the other models are scored against">Reference</span> : <Validation s={r.status} />}
        </div>
      ))}
      <div className="mt-3 grid grid-cols-2 gap-3">
        <div className="rounded-md border border-line p-3">
          <div className="eyebrow mb-1">Ablation · what compute data added</div>
          {loaded && rows[0].hit != null && rows[1].hit != null && rows[0].mae != null && rows[1].mae != null ? (
            <div className="num text-[13px]">Hit {signed(Math.round((rows[1].hit - rows[0].hit) * 100))} pp · MAE {signed(((rows[1].mae - rows[0].mae) / rows[0].mae) * 100)}%{rows[1].ic != null && rows[0].ic != null && <> · IC {signed(rows[1].ic - rows[0].ic, 2)}</>}</div>
          ) : <div className="text-[12px] text-mute">{loaded ? 'Not enough history.' : 'Not loaded.'}</div>}
          <div className="mt-1 text-[11px] text-mute">{live ? computeVerdictText(rows[1], live.augmented[horizon], live.tickers.length) : 'Awaiting the FLOPX evaluation: no ablation result is shown until the backend harness answers.'}</div>
        </div>
        <div className="rounded-md border border-line p-3">
          <div className="eyebrow mb-1">Ablation · what news events added</div>
          {loaded && rows[2].hit != null && rows[1].hit != null && rows[1].mae != null && rows[2].mae != null ? <div className="num text-[13px]">Hit {signed(Math.round((rows[2].hit - rows[1].hit) * 100))} pp · MAE {signed(((rows[2].mae - rows[1].mae) / rows[1].mae) * 100)}%</div> : <div className="text-[12px] text-mute">{live ? `Only ${rows[2].n} scored forecasts: insufficient.` : 'Not loaded.'}</div>}
          <div className="mt-1 text-[11px] text-mute">{live ? `The event feature (feed items naming the ticker in the last 14 days) varied enough to score for ${live.combinedScored[horizon]} of ${live.tickers.length} tickers.` : 'Awaiting the FLOPX evaluation.'}</div>
        </div>
      </div>
    </Panel>
  )
}

export function BacktestCosts() {
  const b = BACKTEST_STATS
  const live = b.basis === 'backend' ? W2_LIVE.portfolio : null
  const na = (v: number | null | undefined, f: (x: number) => string) => (v == null ? '—' : f(v))
  const tiles: [string, string][] = live
    ? [
        ['Gross Sharpe', '—'],
        ['Net Sharpe', '—'],
        ['Return (net, period)', na(b.annRet, (x) => `${x}%`)],
        ['Volatility', '—'],
        ['Max drawdown', na(b.maxDD, (x) => `${x}%`)],
        ['Hit rate', na(b.hitRate, (x) => `${Math.round(x * 100)}%`)],
        ['Turnover', na(b.turnover, (x) => `${x}% (period)`)],
        ['Cost assumption', `${b.costBps} bps/side`],
        ['Costs', na(b.costsUsd, (x) => `$${x.toFixed(0)}`)],
      ]
    : [
        ['Gross Sharpe', na(b.grossSharpe, (x) => x.toFixed(2))],
        ['Net Sharpe', na(b.netSharpe, (x) => x.toFixed(2))],
        ['Ann. return (net)', na(b.annRet, (x) => `${x}%`)],
        ['Volatility', na(b.vol, (x) => `${x}%`)],
        ['Max drawdown', na(b.maxDD, (x) => `${x}%`)],
        ['Hit rate', na(b.hitRate, (x) => `${Math.round(x * 100)}%`)],
        ['Turnover', na(b.turnover, (x) => `${x}%/yr`)],
        ['Cost assumption', `${b.costBps} bps/side`],
        ['Cost drag', na(b.costDrag, (x) => `−${x}%/yr`)],
      ]
  return (
    <Panel title="Backtested strategy behaviour" eyebrow={<><span className="rounded-[2px] border border-dashed border-line2 px-1 text-[10.5px]">Hypothetical</span> {live ? b.period : backtestSpanLong()} <EvalFresh backend={!!live} /></>}>
      <div className="grid grid-cols-3 gap-3">
        {tiles.map(([k, v]) => <div key={k}><div className="text-[11px] text-dim font-medium">{k}</div><div className="num text-[15px]">{v}</div></div>)}
      </div>
      <div className="mt-3 text-[11px] leading-relaxed text-dim">{live
        ? `The ${live.sleeves} FLOPX strategies with their compute signal, equal capital each, charged ${b.costBps} bps per fill, ${basisCaption()}. Over the same window the price-only variants returned ${live.priceOnlyReturnPct}% on average and an equal-weight hold of the ${live.benchmarkTickers} names ${live.benchmarkReturnPct}%. Sharpe and volatility are not computed by the backend; max drawdown is the worst strategy's. No realised profit is claimed.`
        : 'Backtests use point-in-time data where available. Rental quotes before Jun 2024 are reconstructed from 3 sources, so results in that period are less reliable. Past simulated performance does not imply future returns.'}</div>
    </Panel>
  )
}

function computeVerdictText(row: (typeof MODEL_COMPARE)[HorizonId][number], counts: Record<string, number>, tickers: number): string {
  const skill = row.skill != null ? ` (pooled RMSE skill ${signed(row.skill * 100)}%)` : ''
  const parts = [`worsens for ${counts.worsens ?? 0}`, `inconclusive for ${counts.inconclusive ?? 0}`, `improves for ${counts.improves ?? 0}`]
  if (counts['insufficient-sample']) parts.push(`insufficient for ${counts['insufficient-sample']}`)
  const head = row.status === 'validated' ? 'Beats the baseline' : row.status === 'insufficient' ? 'Too few scored forecasts to judge' : 'Does not beat the baseline'
  return `${head}${skill}: ${parts.join(', ')} of ${tickers} tickers.`
}

export function ValidationMatrix() {
  return (
    <Panel title="Signal validation status by horizon" eyebrow={<>Validated ≠ exploratory ≠ no edge <Fresh quiet f="illustrative" /></>}>
      <div className="grid h-8 grid-cols-[1.5fr_repeat(3,minmax(0,1fr))] items-center gap-2 border-b border-line text-[11px] font-medium text-dim"><span>Signal</span><span>1W</span><span>1M</span><span>3M</span></div>
      {SIGNALS.map((s) => (
        <div key={s.id} className="grid grid-cols-[1.5fr_repeat(3,minmax(0,1fr))] items-center gap-2 border-b border-line-soft py-1.5">
          <span className="truncate text-[12px]">{s.name}</span>
          {(['1W', '1M', '3M'] as const).map((h) => <Validation key={h} s={s.horizons[h].status} short fill />)}
        </div>
      ))}
    </Panel>
  )
}

export function Failures() {
  return (
    <Panel variant="rail" title="Failed predictions & blind spots" eyebrow={<>Logged, not hidden <EvalFresh backend={!!W2_LIVE.evaluation} /></>}>
        {FAILURES.map((f) => (
          <div key={f.date + f.what} className="grid grid-cols-[80px_1fr] gap-3 py-2.5">
            <span className="num text-[10.5px] text-dim">{f.date}</span>
            <div>
              <div className="text-[12.5px] text-bone">{f.what}</div>
              <div className="text-[11.5px] text-mute">Why: {f.why}</div>
              <div className="text-[11.5px] text-mute">Change: {f.lesson}</div>
            </div>
          </div>
        ))}
    </Panel>
  )
}
