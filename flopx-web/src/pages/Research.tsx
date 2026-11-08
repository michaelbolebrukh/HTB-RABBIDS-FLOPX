import { useId, useMemo, useState } from 'react'
import { FlaskConical } from 'lucide-react'
import { scaleBand, scaleLinear } from 'd3-scale'
import { line } from 'd3-shape'
import { mean, sum } from 'd3-array'
import { useChartStyle } from '@/lib/chartStyle'
import { band, GLOBAL_CPI, GPUS, MONTHS, NOW_IDX, PRESSURE_MODEL, REGIONS, regionById, rng } from '@/data/core'
import { LOW_INFO, type HorizonId } from '@/data/fund'
import { SCALED_HISTORY_LABEL, SCALED_LABEL, SCALED_SHORT_LABEL, TimeChart, useWidth } from '@/components/charts'
import { Failures, ForecastVsActual, ModelComparison, ValidationMatrix } from '@/components/Eval'
import { cx, Fresh, PageHeader, Panel, Seg, TruthGlyph, TruthTag } from '@/components/ui'
import { uncAt } from '@/lib/model'

const z = (a: (number | null)[]) => { const v = a.filter((x): x is number => x != null && !Number.isNaN(x)); const m = v.reduce((s, x) => s + x, 0) / v.length; const sd = Math.sqrt(v.reduce((s, x) => s + (x - m) ** 2, 0) / v.length) || 1; return a.map((x) => (x == null || Number.isNaN(x) ? null : +((x - m) / sd).toFixed(3))) }

export default function Research() {
  const [idx, setIdx] = useState('global')
  const [hz, setHz] = useState<HorizonId>('1M')
  const series = idx === 'global' ? GLOBAL_CPI : regionById(idx)!.pressure
  const unc = idx === 'global' ? 2.4 : regionById(idx)!.unc
  const b = idx === 'global' ? band(GLOBAL_CPI, unc) : { lo: series.map((_, i) => (i >= NOW_IDX ? series[i] - uncAt(regionById(idx)!, i) : null)), hi: series.map((_, i) => (i >= NOW_IDX ? series[i] + uncAt(regionById(idx)!, i) : null)) }
  const h100Gpu = GPUS.find((g) => g.id === 'h100')!
  const h100 = h100Gpu.index
  // G14: the series above are the design's months scaled to join the real present (src/live/README.md).
  const seriesScaled = idx === 'global' ? PRESSURE_MODEL.globalScaled : !!regionById(idx)!.live?.pressure?.scaled
  const realCpiMonths = PRESSURE_MODEL.globalReal ? 1 : 0
  const lags =[-3, -2, -1, 0, 1, 2, 3, 4, 6].map((l) => ({ l, r: +(l < 0 ? 0.08 + l * 0.02 : l === 0 ? 0.21 : l <= 2 ? 0.34 - (l - 1) * 0.05 : 0.22 - (l - 3) * 0.04).toFixed(2), ci: 0.14 }))
  return (
    <div>
      <PageHeader eyebrow={<><FlaskConical size={12} /> Research & forecast evaluation</>} title="Derived indicators, tested honestly" sub="Explore the Compute Pressure Index and how it relates to observed prices and returns, then inspect forecasts, errors, failures and low-information periods. Every relationship shown here is an estimated association." />

      <div className="mb-5 grid grid-cols-1 gap-5 xl:grid-cols-[1.5fr_1fr]">
        <Panel title="Compute Pressure Index" eyebrow={<><TruthGlyph truth="inf" size={7} />Inferred · with forecast band <Fresh quiet f="illustrative" label={seriesScaled ? SCALED_SHORT_LABEL : undefined} /></>} right={<select value={idx} onChange={(e) => setIdx(e.target.value)} className="h-7 rounded-[4px] border border-line2 bg-ink px-2 font-mono text-[11px]"><option value="global">Global (MW-weighted)</option>{REGIONS.filter((r) => r.quality !== 'none').map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}</select>}>
          <TimeChart labels={MONTHS} height={230} nowIdx={NOW_IDX} legend shade={LOW_INFO.map(([a, c, l]) => [a, c, l])} series={[
            { id: 'cpi', label: 'CPI (inferred)', values: series.map((v, i) => (i <= NOW_IDX ? v : null)), color: 'var(--color-inf)', width: 2, band: { lo: series.map((v, i) => (i <= NOW_IDX ? v - unc : null)), hi: series.map((v, i) => (i <= NOW_IDX ? v + unc : null)), hatched: true } },
            { id: 'fc', label: 'Forecast', values: series.map((v, i) => (i >= NOW_IDX ? v : null)), color: 'var(--color-fct)', style: 'dashed', band: b },
          ]} />
          <div className="mt-2 font-mono text-[10px] text-dim">Hatched band = posterior uncertainty of the hidden state (not measurement error). Shaded = low-information periods.</div>
        </Panel>
        <Panel title="Does pressure lead prices?" eyebrow={<>Correlation of CPI with future H100 index change, by lag <Fresh quiet f="illustrative" /></>}>
          <LagChart lags={lags} />
          <div className="mt-2 text-[11px] leading-relaxed text-mute">Strongest at a 1-month lead (r ≈ 0.34 ± 0.14, n = 25). This is consistent with pressure anticipating price, but it does not establish causation, and a common driver (demand) could explain both.</div>
        </Panel>
      </div>

      <div className="mb-5 grid grid-cols-1 gap-5 xl:grid-cols-[1fr_1.5fr]">
        <Panel title="Pressure vs next-month price change" eyebrow={<>Monthly · all covered regions <Fresh quiet f="illustrative" /></>}>
          <Scatter />
        </Panel>
        <Panel title="Standardised: pressure vs H100 index" eyebrow={<>z-scores · same axis for shape comparison <Fresh quiet f="illustrative" label={PRESSURE_MODEL.globalScaled || h100Gpu.live?.scaled ? SCALED_HISTORY_LABEL : `History before ${MONTHS[NOW_IDX]} illustrative`} /></>}>
          <TimeChart labels={MONTHS.slice(0, NOW_IDX + 1)} height={220} legend yFmt={(v) => v.toFixed(1)} zero series={[
            { id: 'cpi', label: 'Global CPI (z)', values: z(GLOBAL_CPI.slice(0, NOW_IDX + 1)), color: 'var(--color-cat2)' },
            { id: 'h', label: 'H100 index (z)', values: z(h100.slice(0, NOW_IDX + 1)), color: 'var(--color-obs)' },
          ]} />
        </Panel>
      </div>

      <div className="mb-3 flex items-center justify-between"><h2 className="font-serif text-[24px]">Forecast evaluation</h2><Seg value={hz} onChange={setHz} options={[{ id: '1W', label: '1W' }, { id: '1M', label: '1M' }, { id: '3M', label: '3M' }]} /></div>
      <div className="space-y-5">
        <ModelComparison horizon={hz} />
        <div className="grid grid-cols-1 gap-5 xl:grid-cols-[1.4fr_1fr]">
          <ForecastVsActual horizon={hz} />
          <Panel title="Periods with little information" eyebrow={<>Where the system knew less <Fresh quiet f="illustrative" /></>}>
            <div className="space-y-3">
              {[
                { p: 'Jan–Jun 2024', what: 'Only 3 rental price sources; no marketplace sold-out data.', eff: 'CPI uncertainty ±9 (vs ±4 now). Backtest results less reliable.' },
                { p: 'Jun 2025 (2 wks)', what: 'Scraper outage on 2 marketplaces.', eff: 'Forecasts withheld rather than issued on stale inputs.' },
                { p: 'Mar 2026 · Sydney', what: 'Provider API change, feed stale since 06 Mar.', eff: 'Regional band widened; flagged STALE on every surface.' },
                { p: 'Ongoing · UAE, Mumbai, São Paulo', what: 'Sparse or experimental coverage.', eff: 'Shown with wide dashed rings; excluded from strategy signals.' },
                { p: 'Ongoing · Johannesburg', what: 'No usable data.', eff: 'No estimate shown.' },
              ].map((x) => (
                <div key={x.p} className="border-b border-line/60 pb-2.5 last:border-0">
                  <div className="font-mono text-[10.5px] text-warn">{x.p}</div>
                  <div className="text-[12px]">{x.what}</div>
                  <div className="text-[11.5px] text-mute">{x.eff}</div>
                </div>
              ))}
            </div>
          </Panel>
        </div>
        <div className="grid grid-cols-1 gap-5 xl:grid-cols-2">
          <ValidationMatrix />
          <Failures />
        </div>
        <Panel title="CPI forecasts at each horizon" eyebrow={<><TruthTag truth="fct" compact /> Global · from {MONTHS[NOW_IDX]} <Fresh quiet f="illustrative" label={PRESSURE_MODEL.globalScaled ? SCALED_LABEL : undefined} /></>}>
          <div className="grid grid-cols-4 gap-3">
            {[1, 3, 6, 12].map((h) => (
              <div key={h} className={cx('rounded-md border border-dashed p-3', h <= 1 ? 'border-fct/40' : 'border-line2')}>
                <div className="font-mono text-[10px] text-dim">+{h}M · {MONTHS[NOW_IDX + h]}</div>
                <div className="num text-[20px] text-fct">{GLOBAL_CPI[NOW_IDX + h].toFixed(1)}</div>
                <div className="num text-[11px] text-mute">[{b.lo[NOW_IDX + h]?.toFixed(1)}, {b.hi[NOW_IDX + h]?.toFixed(1)}]</div>
                <div className="mt-1 font-mono text-[9.5px] text-dim">{PRESSURE_MODEL.globalReal ? `Not validated: ${realCpiMonths} real month${realCpiMonths === 1 ? '' : 's'} of CPI, no scored forecast` : 'Not validated: FLOPX pressure not loaded, no scored forecast'}</div>
              </div>
            ))}
          </div>
        </Panel>
      </div>
    </div>
  )
}

function LagChart({ lags }: { lags: { l: number; r: number; ci: number }[] }) {
  const [ref, w] = useWidth<HTMLDivElement>()
  const { grid, fontClass, strokeScale, tickScale, transition } = useChartStyle()
  const hatch = `lag-hatch-${useId().replace(/:/g, '')}`
  const h = 190
  const pad = { l: 34, b: 22, t: 22, r: 4 }
  const x = scaleBand<number>().domain(lags.map((d) => d.l)).range([pad.l, Math.max(pad.l + 1, w - pad.r)]).padding(0.44)
  const y = scaleLinear().domain([-0.2, 0.5]).range([h - pad.b, pad.t])
  const ticks = y.ticks(Math.max(2, Math.round(4 * tickScale)))
  const label = (l: number) => `${l > 0 ? '+' : ''}${l}M`
  const peak = lags.reduce((a, b) => (b.r > a.r ? b : a), lags[0] ?? { l: 0, r: 0, ci: 0 })
  const rMax = Math.max(...lags.map((d) => d.r), 0.01)
  // First lag where pressure leads price; the tinted zone starts half a step before it.
  const leadIdx = lags.findIndex((d) => d.l > 0)
  const leadX = leadIdx > 0 ? ((x(lags[leadIdx - 1]!.l) ?? 0) + x.bandwidth() + (x(lags[leadIdx]!.l) ?? 0)) / 2 : null
  const cap = Math.min(6, x.bandwidth() / 4)
  return (
    <div ref={ref}>
      <svg width={w} height={h}>
        <defs>
          <pattern id={hatch} width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <line x1="0" y1="0" x2="0" y2="5" stroke="var(--color-mute)" strokeOpacity={0.45} strokeWidth="1.5" />
          </pattern>
        </defs>
        {leadX != null && (
          <g>
            <rect x={leadX} y={pad.t - 14} width={Math.max(0, w - pad.r - leadX)} height={h - pad.b - pad.t + 14} fill="var(--color-cat1)" fillOpacity={0.05} />
            <line x1={leadX} x2={leadX} y1={pad.t - 14} y2={h - pad.b} stroke="var(--color-cat1)" strokeOpacity={0.35} strokeDasharray="2 3" />
            <text x={leadX - 6} y={pad.t - 4} textAnchor="end" className={`fill-dim ${fontClass}`} fontSize={9}>Price leads</text>
            <text x={leadX + 6} y={pad.t - 4} className={`fill-cat1 ${fontClass}`} fontSize={9}>Pressure leads</text>
          </g>
        )}
        <line x1={pad.l} y1={pad.t} x2={pad.l} y2={h - pad.b} stroke="var(--color-line2)" />
        {grid === 'xy' && lags.map((d) => <line key={`gx${d.l}`} x1={(x(d.l) ?? 0) + x.bandwidth() / 2} x2={(x(d.l) ?? 0) + x.bandwidth() / 2} y1={pad.t} y2={h - pad.b} stroke="var(--color-line)" strokeOpacity={0.5} />)}
        {ticks.map((v) => (
          <g key={v}>
            {grid !== 'none' && v !== 0 && <line x1={pad.l} x2={w - pad.r} y1={y(v)} y2={y(v)} stroke="var(--color-line)" strokeDasharray="2 4" />}
            <text x={pad.l - 6} y={y(v) + 3} textAnchor="end" className={`fill-dim ${fontClass}`} fontSize={9}>{v.toFixed(1)}</text>
          </g>
        ))}
        <line x1={pad.l} x2={w - pad.r} y1={y(0)} y2={y(0)} stroke="var(--color-line2)" />
        {lags.map((d) => {
          const cx0 = (x(d.l) ?? 0) + x.bandwidth() / 2
          const significant = d.r - d.ci > 0 || d.r + d.ci < 0
          const isPeak = d.l === peak.l
          const top = Math.min(y(d.r), y(0))
          const height = Math.abs(y(d.r) - y(0))
          return (
            <g key={d.l}>
              {significant ? (
                <rect x={x(d.l)} y={top} width={x.bandwidth()} height={height} fill="var(--color-cat1)" fillOpacity={isPeak ? 0.95 : 0.3 + 0.45 * (d.r / rMax)} style={{ transition }} />
              ) : (
                <rect x={(x(d.l) ?? 0) + 0.5} y={top + 0.5} width={Math.max(0, x.bandwidth() - 1)} height={Math.max(0, height - 1)} fill={`url(#${hatch})`} stroke="var(--color-mute)" strokeOpacity={0.6} style={{ transition }} />
              )}
              <g stroke="var(--color-bone)" strokeOpacity={significant ? 0.7 : 0.4} strokeWidth={strokeScale}>
                <line x1={cx0} x2={cx0} y1={y(d.r - d.ci)} y2={y(d.r + d.ci)} />
                <line x1={cx0 - cap} x2={cx0 + cap} y1={y(d.r + d.ci)} y2={y(d.r + d.ci)} />
                <line x1={cx0 - cap} x2={cx0 + cap} y1={y(d.r - d.ci)} y2={y(d.r - d.ci)} />
              </g>
              {isPeak && <text x={(x(d.l) ?? 0) + x.bandwidth() + 4} y={y(d.r) + 4} className={`fill-cat1 ${fontClass}`} fontSize={10} fontWeight={600}>{d.r.toFixed(2)}</text>}
              <text x={cx0} y={h - 6} textAnchor="middle" className={cx(isPeak ? 'fill-bone' : 'fill-dim', fontClass)} fontSize={9.5}>{label(d.l)}</text>
            </g>
          )
        })}
      </svg>
      <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-[10.5px] text-mute">
        <span className="flex items-center gap-1.5"><span className="inline-block size-2.5 bg-cat1" />Interval excludes zero</span>
        <span className="flex items-center gap-1.5"><span className="inline-block size-2.5 border border-mute/60" style={{ backgroundImage: 'repeating-linear-gradient(135deg, color-mix(in oklab, var(--color-mute) 45%, transparent) 0 1.5px, transparent 1.5px 4px)' }} />Interval includes zero</span>
        <span className="text-dim">Shade = strength of r</span>
      </div>
    </div>
  )
}

function Scatter() {
  const [ref, w] = useWidth<HTMLDivElement>()
  const { grid, fontClass, strokeScale, tickScale, weight, transition } = useChartStyle()
  const pts = useMemo(() => {
    const r = rng(404)
    return Array.from({ length: 140 }, () => { const x = (r() - 0.5) * 16; return { x, y: x * 0.32 + (r() - 0.5) * 9 } })
  }, [])
  const fit = useMemo(() => {
    const mx = mean(pts, (p) => p.x) ?? 0
    const my = mean(pts, (p) => p.y) ?? 0
    const sxy = sum(pts, (p) => (p.x - mx) * (p.y - my))
    const sxx = sum(pts, (p) => (p.x - mx) ** 2)
    const syy = sum(pts, (p) => (p.y - my) ** 2)
    const slope = sxy / sxx
    return { slope, intercept: my - slope * mx, r2: (sxy * sxy) / (sxx * syy) }
  }, [pts])
  const h = 220
  const pad = 30
  const right = 8
  const top = 8
  const x = scaleLinear().domain([-9, 9]).range([pad, Math.max(pad + 1, w - right)])
  const y = scaleLinear().domain([-9, 9]).range([h - pad, top])
  const tickN = Math.max(2, Math.round(6 * tickScale))
  const xt = x.ticks(tickN)
  const yt = y.ticks(tickN)
  const r = 2.4 * (weight === 'bold' ? 1.25 : 1)
  const fitLine = line<number>().x((v) => x(v)).y((v) => y(fit.intercept + fit.slope * v))
  /** Same sign = pressure and next-month price moved together (supports the link); opposite sign = moved apart. */
  const together = (pt: { x: number; y: number }) => pt.x * pt.y > 0
  const nTogether = pts.filter(together).length
  return (
    <div ref={ref}>
      <svg width={w} height={h}>
        {grid !== 'none' && yt.map((v) => <line key={`gy${v}`} x1={pad} x2={w - right} y1={y(v)} y2={y(v)} stroke="var(--color-line)" strokeOpacity={0.5} />)}
        {grid === 'xy' && xt.map((v) => <line key={`gx${v}`} x1={x(v)} x2={x(v)} y1={top} y2={h - pad} stroke="var(--color-line)" strokeOpacity={0.5} />)}
        <line x1={pad} x2={w - right} y1={y(0)} y2={y(0)} stroke="var(--color-line2)" />
        <line x1={x(0)} x2={x(0)} y1={top} y2={h - pad} stroke="var(--color-line2)" />
        {xt.map((v) => <text key={`tx${v}`} x={x(v)} y={h - pad + 11} textAnchor="middle" className={`fill-dim ${fontClass}`} fontSize={8.5}>{v}</text>)}
        {yt.map((v) => <text key={`ty${v}`} x={pad - 5} y={y(v) + 3} textAnchor="end" className={`fill-dim ${fontClass}`} fontSize={8.5}>{v}</text>)}
        {pts.map((p, i) => <circle key={i} cx={x(p.x)} cy={y(p.y)} r={r} fill={together(p) ? 'var(--color-cat1)' : 'var(--color-cat2)'} fillOpacity={0.85} stroke="var(--color-panel)" strokeWidth={0.75} style={{ transition }}><title>{`Δ CPI ${p.x.toFixed(1)} pts · next-month Δ price ${p.y.toFixed(1)}% · ${together(p) ? 'moved together' : 'moved apart'}`}</title></circle>)}
        <path d={fitLine([-8, 8]) ?? undefined} fill="none" stroke="var(--color-bone)" strokeWidth={1.75 * strokeScale} strokeDasharray="5 3" style={{ transition }} />
        <text x={w - 10} y={h - 4} textAnchor="end" className={`fill-dim ${fontClass}`} fontSize={9.5}>Δ CPI (pts) →</text>
        <text x={pad + 4} y={16} className={`fill-dim ${fontClass}`} fontSize={9.5}>next-month Δ price (%) ↑</text>
      </svg>
      <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-mute">
        <span className="flex items-center gap-1.5"><span className="size-2 rounded-full bg-cat1" />Moved together <span className="num text-dim">{nTogether}</span></span>
        <span className="flex items-center gap-1.5"><span className="size-2 rounded-full bg-cat2" />Moved apart <span className="num text-dim">{pts.length - nTogether}</span></span>
        <span className="flex items-center gap-1.5"><svg width="16" height="4"><line x1="0" x2="16" y1="2" y2="2" stroke="var(--color-bone)" strokeWidth="1.75" strokeDasharray="5 3" /></svg>Estimated fit</span>
        <span className="num ml-auto text-dim">slope {fit.slope.toFixed(2)} · R² {fit.r2.toFixed(2)} · n = {pts.length} region-months</span>
      </div>
    </div>
  )
}
