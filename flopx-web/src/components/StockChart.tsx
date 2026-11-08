import { useId, useMemo, useState, type ReactNode } from 'react'
import { area as d3area, curveLinear, curveStepAfter, line as d3line } from 'd3-shape'
import { scaleLinear } from 'd3-scale'
import { useWidth } from './charts'
import { CHART_TYPES, LINE_TYPES, OHLC_TYPES, type ChartType } from '@/lib/chartTypes'
import { useChartStyle } from '@/lib/chartStyle'
import { indicatorById, tail, type IndicatorId, type IndicatorResult } from '@/lib/indicators'
import { computeIndicator } from '@/lib/indicatorMath'

import type { Candle } from '@/data/fund'
export type { Candle }
export interface Line { id: string; label: string; values: number[]; color: string; dash?: string; width?: number; opacity?: number }
export interface Marker { i: number; id: string; label: string; hollow?: boolean; active?: boolean }

const AXIS = 64 // right-hand price axis, trading-platform style
const GAP = 10 // gap between panes, holds the divider
const X_AXIS = 20
const PANE_PAD = 16 // room for the pane title inside each pane
const IND_H = 76 // height of each indicator pane

const fmtInd = (v: number, fmt: IndicatorResult['fmt'], fp: (v: number) => string = money, fv: (v: number) => string = money) =>
  fmt === 'percent' ? `${v.toFixed(1)}%` : fmt === 'price' ? fp(v) : fmt === 'volume' ? fv(v)
    : Math.abs(v) >= 1e6 ? `${(v / 1e6).toFixed(2)}M` : Math.abs(v) >= 1e4 ? `${(v / 1e3).toFixed(1)}k` : v.toFixed(1)

const money = (v: number) => (Math.abs(v) >= 1e6 ? `$${(v / 1e6).toFixed(2)}M` : `$${(v / 1e3).toFixed(0)}k`)

/** Price (candles or lines), volume and drawdown as three stacked panes on one time axis with a shared crosshair. */
export function StockChart({
  labels, candles, volume, drawdown, type, lines = [], splitIdx, splitLabel, markers = [], onMarker,
  priceH = 230, volH = 64, ddH = 64, indicators = [],
  history, fmtPrice: fp = money, fmtVolume: fv = money, priceLabel = 'NAV', volumeLabel = 'Volume · traded notional', note,
}: {
  labels: string[]
  candles: Candle[]
  volume: number[]
  drawdown: number[]
  type: ChartType
  /** Drawn for the line chart types instead of the candles. */
  lines?: Line[]
  splitIdx?: number
  splitLabel?: string
  markers?: Marker[]
  onMarker?: (id: string) => void
  priceH?: number
  volH?: number
  ddH?: number
  /** Technical indicators: overlays go on the price pane, the rest get their own pane. */
  indicators?: IndicatorId[]
  /** Longer series whose tail is the visible one: indicators are computed on it so they are warmed up from the first visible bar. */
  history?: { candles: Candle[]; volume: number[] }
  /** Axis/readout formatters; default to the fund's $k/$M money format. */
  fmtPrice?: (v: number) => string
  fmtVolume?: (v: number) => string
  priceLabel?: string
  volumeLabel?: string
  /** A note for the readout line beside the week it shows (e.g. the source of an archived close), or null for none. */
  note?: (i: number) => ReactNode
}) {
  const [ref, w] = useWidth<HTMLDivElement>()
  const uid = useId().replace(/:/g, '')
  const { strokeScale, fontClass } = useChartStyle()
  const [hover, setHover] = useState<number | null>(null)
  const n = candles.length
  const results = useMemo(
    () => indicators.map((id) => ({ def: indicatorById(id), res: tail(computeIndicator(id, history?.candles ?? candles, history?.volume ?? volume), n) })),
    [indicators, candles, volume, history, n],
  )
  const overlays = results.filter((r) => r.def.placement === 'overlay')
  const paneInds = results.filter((r) => r.def.placement === 'pane')
  const indExt = paneInds.length * (IND_H + GAP)
  const plotW = Math.max(60, w - AXIS)
  const step = plotW / n
  const sx = (i: number) => (i + 0.5) * step

  // pane geometry: [top, height] of the drawable area (below each pane's title strip)
  const pTop = PANE_PAD
  const indTop = (k: number) => priceH + GAP + k * (IND_H + GAP) + PANE_PAD
  const vTop = priceH + GAP + indExt + PANE_PAD
  const dTop = priceH + GAP + indExt + volH + GAP + PANE_PAD
  const H = priceH + GAP + indExt + volH + GAP + ddH + X_AXIS
  const iH = IND_H - PANE_PAD - 2
  const pH = priceH - PANE_PAD - 4
  const vH = volH - PANE_PAD - 2
  const dH = ddH - PANE_PAD - 2

  const isLine = LINE_TYPES.has(type)
  // Heikin-Ashi series derived from the candles
  const ha: Candle[] = []
  if (type === 'heikin') {
    candles.forEach((k, i) => {
      const c = (k.o + k.h + k.l + k.c) / 4
      const o = i ? (ha[i - 1].o + ha[i - 1].c) / 2 : (k.o + k.c) / 2
      ha.push({ ...k, o, c, h: Math.max(k.h, o, c), l: Math.min(k.l, o, c) })
    })
  }
  const cs = type === 'heikin' ? ha : candles
  const base = candles[0].o // baseline reference
  const closeOnly = type === 'area' || type === 'baseline' || type === 'columns'
  const priceVals = isLine ? lines.flatMap((l) => l.values) : closeOnly ? [...cs.map((k) => k.c), ...(type === 'baseline' ? [base] : [])] : cs.flatMap((k) => [k.h, k.l])
  // overlay values widen the price domain only when they stay within ±25% of the candle range
  const pLo = Math.min(...priceVals)
  const pHi = Math.max(...priceVals)
  const pSpan = (pHi - pLo) || Math.abs(pHi) * 0.02 || 1
  const overlayVals = overlays.flatMap(({ res }) => [...res.lines.flatMap((l) => l.values), ...(res.band ? [...res.band.upper, ...res.band.lower] : [])])
    .filter((v): v is number => v != null && Number.isFinite(v) && v >= pLo - 0.25 * pSpan && v <= pHi + 0.25 * pSpan)
  const yScale = scaleLinear().domain([Math.min(pLo, ...overlayVals), Math.max(pHi, ...overlayVals)]).nice().range([pTop + pH, pTop])
  const pTicks = yScale.ticks(4)
  const py = (v: number) => yScale(v)
  const vMax = Math.max(...volume)
  const vy = (v: number) => vTop + vH - (vMax ? v / vMax : 0) * vH // all-zero volume (an index) draws no bars
  const ddMin = Math.min(-1, Math.floor(Math.min(...drawdown) / 5) * 5)
  const dy = (v: number) => dTop + (v / ddMin) * dH

  const bodyW = Math.max(1, Math.min(9, step * 0.62))
  const up = (c: Candle) => c.c >= c.o
  const upPrev = (i: number) => (i ? cs[i].c >= cs[i - 1].c : up(cs[0]))
  const colOf = (u: boolean) => (u ? 'var(--color-up)' : 'var(--color-down)')
  const vMaxAll = Math.max(...volume)
  const past = (i: number) => splitIdx != null && i < splitIdx
  const at = hover ?? n - 1
  const c = cs[at]
  const last = cs[n - 1]
  const curve = type === 'step' ? curveStepAfter : curveLinear
  const lineGen = d3line<number>().x((_, i) => sx(i)).y((v) => py(v)).curve(curve)
  const closeLine = d3line<Candle>().x((_, i) => sx(i)).y((k) => py(k.c))(cs) ?? ''
  const areaPath = (y0: (i: number) => number, y1: (i: number) => number) => d3area<Candle>().x((_, i) => sx(i)).y0((_, i) => y0(i)).y1((_, i) => y1(i))(cs) ?? ''
  const bottom = pTop + pH
  const baseY = py(base)
  const typeLabel = CHART_TYPES.find((t) => t.id === type)?.label ?? type
  const lineOf = (vals: number[], y: (v: number) => number) => vals.map((v, i) => `${i ? 'L' : 'M'}${sx(i).toFixed(1)},${y(v).toFixed(1)}`).join('')
  const xTicks = labels.map((l, i) => ({ l, i })).filter(({ i }) => i % Math.max(1, Math.ceil(n / Math.max(2, Math.floor(plotW / 90)))) === 0)

  const pane = (top: number, h: number, title: string, shade: boolean) => (
    <g>
      <rect x={0} y={top - PANE_PAD} width={plotW} height={h} fill={shade ? 'var(--color-raise)' : 'transparent'} opacity={0.55} />
      <text x={6} y={top - PANE_PAD + 12} className="fill-dim font-sans" fontSize={10.5} fontWeight={500}>{title}</text>
    </g>
  )

  return (
    <div ref={ref} className="select-none">
      {/* readout: values under the crosshair, or the latest week */}
      <div className="mb-2 flex flex-wrap items-baseline gap-x-4 gap-y-1 font-mono text-[11px] tabular-nums">
        <span className="text-mute">{labels[at]}{past(at) ? ' · backtest' : splitIdx != null ? ' · paper' : ''}{note?.(at)}</span>
        {OHLC_TYPES.has(type) ? (
          <span className={up(c) ? 'text-up' : 'text-down'}>
            <span className="text-dim">O</span> {fp(c.o)} <span className="text-dim">H</span> {fp(c.h)} <span className="text-dim">L</span> {fp(c.l)} <span className="text-dim">C</span> {fp(c.c)}
            {' '}({c.c >= c.o ? '+' : '−'}{Math.abs(((c.c - c.o) / c.o) * 100).toFixed(2)}%)
          </span>
        ) : isLine ? (
          lines.map((l) => <span key={l.id} style={{ color: l.color, opacity: l.opacity ?? 1 }}>{l.label} {fp(l.values[at])}</span>)
        ) : (
          <span className={type === 'baseline' ? (c.c >= base ? 'text-up' : 'text-down') : 'text-bone'}>
            <span className="text-dim">Close</span> {fp(c.c)}
            {type === 'baseline' && <> ({c.c >= base ? '+' : '−'}{Math.abs(((c.c - base) / base) * 100).toFixed(2)}% vs base)</>}
          </span>
        )}
        {results.map(({ def, res }) => (
          <span key={def.id} className="text-dim">{def.short}{' '}
            {res.lines.map((l) => <span key={l.id} style={{ color: l.color }}>{l.values[at] != null ? fmtInd(l.values[at] as number, res.fmt, fp, fv) : '–'}{' '}</span>)}
          </span>
        ))}
        <span className="text-bone"><span className="text-dim">Vol</span> {fv(volume[at])}</span>
        <span className="text-down"><span className="text-dim">DD</span> {drawdown[at].toFixed(1)}%</span>
      </div>

      <svg width={w} height={H} className="block overflow-visible"
        onMouseMove={(e) => { const x = e.clientX - e.currentTarget.getBoundingClientRect().left; setHover(x >= 0 && x <= plotW ? Math.min(n - 1, Math.max(0, Math.floor(x / step))) : null) }}
        onMouseLeave={() => setHover(null)}>
        {pane(pTop, priceH, isLine ? `${priceLabel} · vs benchmarks` : `${priceLabel} · ${typeLabel}`, false)}
        {paneInds.map(({ def }, k) => <g key={def.id}>{pane(indTop(k), IND_H, def.short, k % 2 === 0)}</g>)}
        {pane(vTop, volH, volumeLabel, true)}
        {pane(dTop, ddH, 'Drawdown', false)}

        {/* pane dividers */}
        {[priceH + GAP / 2, ...paneInds.map((_, k) => priceH + GAP + k * (IND_H + GAP) + IND_H + GAP / 2), priceH + GAP + indExt + volH + GAP / 2].map((y) => <line key={y} x1={0} x2={w} y1={y} y2={y} stroke="var(--color-line2)" />)}
        <line x1={plotW} x2={plotW} y1={0} y2={H - X_AXIS} stroke="var(--color-line)" />

        {/* price grid + right axis */}
        {pTicks.map((t) => (
          <g key={t}>
            <line x1={0} x2={plotW} y1={py(t)} y2={py(t)} stroke="var(--color-line)" strokeDasharray="2 3" />
            <text x={plotW + 6} y={py(t) + 3} className={`fill-dim ${fontClass}`} fontSize={9.5}>{fp(t)}</text>
          </g>
        ))}
        <text x={plotW + 6} y={vTop + 3} className={`fill-dim ${fontClass}`} fontSize={9.5}>{fv(vMax)}</text>
        <text x={plotW + 6} y={dTop + 3} className={`fill-dim ${fontClass}`} fontSize={9.5}>0%</text>
        <text x={plotW + 6} y={dTop + dH + 3} className={`fill-dim ${fontClass}`} fontSize={9.5}>{ddMin}%</text>

        {/* backtest | paper split */}
        {splitIdx != null && (
          <g>
            <rect x={sx(splitIdx) - step / 2} y={0} width={plotW - (sx(splitIdx) - step / 2)} height={H - X_AXIS} fill="var(--color-dec)" opacity={0.045} />
            <line x1={sx(splitIdx) - step / 2} x2={sx(splitIdx) - step / 2} y1={0} y2={H - X_AXIS} stroke="var(--color-dec)" strokeOpacity={0.5} strokeDasharray="3 3" />
            {splitLabel && <text x={sx(splitIdx) - step / 2 + 5} y={11} className="font-sans" fontSize={10.5} fontWeight={500} fill="var(--color-dec)">{splitLabel}</text>}
          </g>
        )}

        {/* price */}
        <defs>
          <linearGradient id={`sc-area-grad-${uid}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--color-dec)" stopOpacity={0.4} />
            <stop offset="100%" stopColor="var(--color-dec)" stopOpacity={0} />
          </linearGradient>
          <clipPath id={`sc-pclip-${uid}`}><rect x={0} y={pTop} width={plotW} height={pH} /></clipPath>
          <clipPath id={`sc-above-${uid}`}><rect x={0} y={pTop} width={plotW} height={Math.max(0, baseY - pTop)} /></clipPath>
          <clipPath id={`sc-below-${uid}`}><rect x={0} y={baseY} width={plotW} height={Math.max(0, bottom - baseY)} /></clipPath>
        </defs>
        {isLine && lines.map((l) => (
          <g key={l.id}>
            <path d={lineGen(l.values) ?? ''} fill="none" stroke={l.color} strokeWidth={(l.width ?? 1.25) * strokeScale} strokeDasharray={l.dash} opacity={l.opacity ?? 1} />
            {type === 'markers' && l.id === lines[0].id && l.values.map((v, i) => <circle key={i} cx={sx(i)} cy={py(v)} r={2} fill={l.color} />)}
          </g>
        ))}
        {type === 'area' && (
          <g>
            <path d={areaPath(() => bottom, (i) => py(cs[i].c))} fill={`url(#sc-area-grad-${uid})`} />
            <path d={closeLine} fill="none" stroke="var(--color-dec)" strokeWidth={1.5 * strokeScale} />
          </g>
        )}
        {type === 'hlc' && (
          <g>
            <path d={areaPath((i) => py(cs[i].l), (i) => py(cs[i].h))} fill="var(--color-dec)" opacity={0.18} />
            <path d={closeLine} fill="none" stroke="var(--color-dec)" strokeWidth={1.5 * strokeScale} />
          </g>
        )}
        {type === 'baseline' && (
          <g>
            <path d={areaPath(() => baseY, (i) => py(cs[i].c))} fill="var(--color-up)" opacity={0.25} clipPath={`url(#sc-above-${uid})`} />
            <path d={areaPath(() => baseY, (i) => py(cs[i].c))} fill="var(--color-down)" opacity={0.25} clipPath={`url(#sc-below-${uid})`} />
            <path d={closeLine} fill="none" stroke="var(--color-up)" strokeWidth={1.5 * strokeScale} clipPath={`url(#sc-above-${uid})`} />
            <path d={closeLine} fill="none" stroke="var(--color-down)" strokeWidth={1.5 * strokeScale} clipPath={`url(#sc-below-${uid})`} />
            <line x1={0} x2={plotW} y1={baseY} y2={baseY} stroke="var(--color-mute)" strokeDasharray="4 3" />
            <rect x={plotW + 1} y={baseY - 8} width={AXIS - 2} height={16} rx={2} fill="var(--color-raise)" stroke="var(--color-line2)" />
            <text x={plotW + 6} y={baseY + 3.5} className={fontClass} fontSize={9.5} fill="var(--color-bone)">{fp(base)}</text>
          </g>
        )}
        {!isLine && cs.map((k, i) => {
          const x = sx(i)
          const op = past(i) ? 0.5 : 1
          if (type === 'columns') {
            const bw = Math.max(1, step * 0.7)
            return <rect key={i} x={x - bw / 2} y={py(k.c)} width={bw} height={Math.max(0, bottom - py(k.c))} fill={colOf(upPrev(i))} opacity={op * 0.8} />
          }
          if (type === 'highlow') {
            const bw = Math.max(1, Math.min(4, step * 0.35))
            return (
              <g key={i} opacity={op}>
                <rect x={x - bw / 2} y={py(k.h)} width={bw} height={Math.max(1, py(k.l) - py(k.h))} fill={colOf(upPrev(i))} />
                {hover === i && (
                  <g pointerEvents="none">
                    <text x={x + 5} y={py(k.h) + 3} className={`fill-bone ${fontClass}`} fontSize={9}>H {fp(k.h)}</text>
                    <text x={x + 5} y={py(k.l) + 3} className={`fill-bone ${fontClass}`} fontSize={9}>L {fp(k.l)}</text>
                  </g>
                )}
              </g>
            )
          }
          if (type === 'bars') {
            const col = colOf(up(k))
            const t = Math.max(1, Math.min(5, step * 0.35))
            return (
              <g key={i} opacity={op} stroke={col} strokeWidth={1}>
                <line x1={x} x2={x} y1={py(k.h)} y2={py(k.l)} />
                <line x1={x - t} x2={x} y1={py(k.o)} y2={py(k.o)} />
                <line x1={x} x2={x + t} y1={py(k.c)} y2={py(k.c)} />
              </g>
            )
          }
          if (type === 'area' || type === 'hlc' || type === 'baseline') return null
          // candles, hollow, volcandles, heikin
          const col = colOf(type === 'hollow' ? upPrev(i) : up(k))
          const bw = type === 'volcandles' ? Math.max(1, (vMaxAll ? volume[i] / vMaxAll : 1) * step * 0.9) : bodyW
          const top = py(Math.max(k.o, k.c))
          const hollow = type === 'hollow' && up(k)
          return (
            <g key={i} opacity={op}>
              <line x1={x} x2={x} y1={py(k.h)} y2={py(k.l)} stroke={col} strokeWidth={1} />
              <rect x={x - bw / 2} y={top} width={bw} height={Math.max(1, py(Math.min(k.o, k.c)) - top)} fill={hollow ? 'var(--color-panel)' : col} stroke={col} strokeWidth={hollow ? 1 : 0} />
            </g>
          )
        })}

        {/* overlay indicators on the price pane */}
        <g clipPath={`url(#sc-pclip-${uid})`} pointerEvents="none">
          {overlays.map(({ def, res }) => {
            const idx = Array.from({ length: n }, (_, i) => i)
            const band = res.band
            return (
              <g key={def.id}>
                {band && <path d={d3area<number>().defined((i) => band.upper[i] != null && band.lower[i] != null).x((i) => sx(i)).y0((i) => py(band.lower[i] as number)).y1((i) => py(band.upper[i] as number))(idx) ?? ''} fill={band.color} fillOpacity={0.08} />}
                {res.lines.map((l) => l.kind === 'dots'
                  ? <g key={l.id} fill={l.color}>{l.values.map((v, i) => v != null && <circle key={i} cx={sx(i)} cy={py(v)} r={1.6} />)}</g>
                  : <path key={l.id} d={d3line<number | null>().defined((v) => v != null).x((_, i) => sx(i)).y((v) => py(v as number))(l.values) ?? ''} fill="none" stroke={l.color} strokeWidth={1.25 * strokeScale} strokeDasharray={l.kind === 'dashed' ? '4 3' : undefined} />)}
              </g>
            )
          })}
        </g>

        {/* last-price tag on the axis */}
        {!isLine && (
          <g>
            <line x1={0} x2={plotW} y1={py(last.c)} y2={py(last.c)} stroke={colOf(up(last))} strokeOpacity={0.5} strokeDasharray="1 2" />
            <rect x={plotW + 1} y={py(last.c) - 8} width={AXIS - 2} height={16} rx={2} fill={colOf(up(last))} />
            <text x={plotW + 6} y={py(last.c) + 3.5} className={fontClass} fontSize={9.5} fontWeight={600} fill="var(--color-ink)">{fp(last.c)}</text>
          </g>
        )}

        {/* decision markers above the bar */}
        {markers.map((m) => {
          const y = py(isLine && lines.length ? lines[0].values[m.i] : closeOnly ? cs[m.i].c : cs[m.i].h) - 9 // line types: sit on the primary (first) line
          return (
            <g key={m.id} className="cursor-pointer" onClick={() => onMarker?.(m.id)}>
              <title>{m.label}</title>
              <rect x={sx(m.i) - 4} y={y - 4} width={8} height={8} transform={`rotate(45 ${sx(m.i)} ${y})`}
                fill={m.hollow ? 'var(--color-panel)' : 'var(--color-dec)'} stroke="var(--color-dec)" strokeWidth={m.active ? 2 : 1.25} />
            </g>
          )
        })}

        {/* indicator panes */}
        {paneInds.map(({ def, res }, k) => {
          const top = indTop(k)
          const vals = res.lines.flatMap((l) => l.values).filter((v): v is number => v != null && Number.isFinite(v))
          const hist = res.lines.some((l) => l.kind === 'histogram')
          let lo = res.range ? res.range[0] : Math.min(...vals, ...(hist ? [0] : []))
          let hi = res.range ? res.range[1] : Math.max(...vals, ...(hist ? [0] : []))
          if (!Number.isFinite(lo) || !Number.isFinite(hi)) { lo = 0; hi = 1 }
          if (lo === hi) { lo -= 1; hi += 1 }
          const sc = scaleLinear().domain([lo, hi]).range([top + iH, top])
          if (!res.range) sc.nice()
          const [d0, d1] = sc.domain()
          const zero = sc(0)
          return (
            <g key={def.id}>
              {(res.guides ?? []).filter((g) => g >= d0 && g <= d1).map((g) => (
                <g key={g}>
                  <line x1={0} x2={plotW} y1={sc(g)} y2={sc(g)} stroke="var(--color-mute)" strokeOpacity={0.6} strokeDasharray="3 3" />
                  <text x={plotW + 6} y={sc(g) + 3} className={`fill-dim ${fontClass}`} fontSize={9.5}>{fmtInd(g, res.fmt, fp, fv)}</text>
                </g>
              ))}
              <text x={plotW + 6} y={top + 3} className={`fill-dim ${fontClass}`} fontSize={9.5}>{fmtInd(d1, res.fmt, fp, fv)}</text>
              <text x={plotW + 6} y={top + iH + 3} className={`fill-dim ${fontClass}`} fontSize={9.5}>{fmtInd(d0, res.fmt, fp, fv)}</text>
              {res.lines.map((l) => {
                if (l.kind === 'histogram') {
                  return (
                    <g key={l.id}>
                      {l.values.map((v, i) => v != null && (
                        <rect key={i} x={sx(i) - bodyW / 2} y={Math.min(sc(v), zero)} width={bodyW} height={Math.abs(sc(v) - zero)}
                          fill={v >= 0 ? 'var(--color-up)' : 'var(--color-down)'} opacity={0.6} />
                      ))}
                    </g>
                  )
                }
                if (l.kind === 'dots') return <g key={l.id} fill={l.color}>{l.values.map((v, i) => v != null && <circle key={i} cx={sx(i)} cy={sc(v)} r={1.6} />)}</g>
                return <path key={l.id} d={d3line<number | null>().defined((v) => v != null).x((_, i) => sx(i)).y((v) => sc(v as number))(l.values) ?? ''} fill="none" stroke={l.color} strokeWidth={1.25 * strokeScale} strokeDasharray={l.kind === 'dashed' ? '4 3' : undefined} />
              })}
            </g>
          )
        })}

        {/* volume */}
        {volume.map((v, i) => (
          <rect key={i} x={sx(i) - bodyW / 2} y={vy(v)} width={bodyW} height={vTop + vH - vy(v)}
            fill={up(candles[i]) ? 'var(--color-up)' : 'var(--color-down)'} opacity={past(i) ? 0.3 : 0.6} />
        ))}

        {/* drawdown */}
        <path d={`${lineOf(drawdown, dy)}L${sx(n - 1)},${dTop}L${sx(0)},${dTop}Z`} fill="var(--color-down)" opacity={0.18} />
        <path d={lineOf(drawdown, dy)} fill="none" stroke="var(--color-down)" strokeWidth={1} />

        {/* x axis */}
        {xTicks.map(({ l, i }) => <text key={i} x={i ? sx(i) : 0} y={H - 6} textAnchor={i ? 'middle' : 'start'} className={`fill-dim ${fontClass}`} fontSize={9.5}>{l}</text>)}

        {/* crosshair across all panes */}
        {hover != null && (
          <g pointerEvents="none">
            <line x1={sx(hover)} x2={sx(hover)} y1={0} y2={H - X_AXIS} stroke="var(--color-mute)" strokeOpacity={0.6} strokeDasharray="2 2" />
            <rect x={sx(hover) - 34} y={H - X_AXIS + 2} width={68} height={15} rx={2} fill="var(--color-raise)" stroke="var(--color-line2)" />
            <text x={sx(hover)} y={H - X_AXIS + 12.5} textAnchor="middle" className={`fill-bone ${fontClass}`} fontSize={9.5}>{labels[hover]}</text>
          </g>
        )}
      </svg>
    </div>
  )
}
