import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { scaleLinear } from 'd3-scale'
import { area as d3area, line as d3line } from 'd3-shape'
import { cx } from './ui'
import { useChartStyle } from '../lib/chartStyle'
import { useIsPhone } from '../lib/usePhone'

export function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null)
  const [w, setW] = useState(600)
  useLayoutEffect(() => {
    if (!ref.current) return
    const ro = new ResizeObserver(([e]) => setW(Math.max(120, e.contentRect.width)))
    ro.observe(ref.current)
    return () => ro.disconnect()
  }, [])
  return [ref, w] as const
}

export interface Series {
  id: string
  label?: string
  values: (number | null)[]
  color: string
  style?: 'solid' | 'dashed' | 'dotted'
  width?: number
  band?: { lo: (number | null)[]; hi: (number | null)[]; hatched?: boolean }
  area?: boolean
  fmt?: (v: number) => string
  /** Stroke opacity, to push context series (benchmarks) into the background. */
  opacity?: number
  /** Always draw a mark at every point, whatever the chart style: for a series of isolated readings (the FLOPX index's own days beside a longer history). */
  points?: boolean
}

export interface Annotation {
  i: number
  id: string
  label: string
  color?: string
  active?: boolean
  hollow?: boolean
}

const clean = (v: number | null | undefined) => (v == null || Number.isNaN(v) ? null : v)

/**
 * The one visible marker a month-axis chart carries when its illustrative months were scaled to
 * join the real present (decision G14, src/live/README.md). Pages pass it as the `label` of the
 * existing illustrative badge in the eyebrow; it adds no layout.
 */
export const SCALED_LABEL = "History and forecast scaled to today's reading, illustrative"
/** The same marker for a past-only chart (no forecast slots on its axis). */
export const SCALED_HISTORY_LABEL = "History scaled to today's reading, illustrative"
/** The same marker where the eyebrow slot also holds a control and the long form would wrap the title. */
export const SCALED_SHORT_LABEL = "Scaled to today's reading, illustrative"
/**
 * The badge a GPU index chart or spark carries when its past months are GetDeploying's archived
 * weekly medians (CC BY 4.0) rather than the design's curve, and the label of the FLOPX index's own
 * marks beside it. Defined with the data they describe (src/live/gpu.ts), re-exported here beside
 * the other chart labels.
 */
export { FCPI_MARKS_LABEL, GDH_BADGE_LABEL } from '@/live/gpu'

/** Beeswarm layout: y offsets (px, around 0) so dots at x positions never overlap.
 *  Dots are placed in x order, each at the free offset closest to the axis; past `maxOff` they may overlap. */
export function beeswarm(xs: number[], r: number, maxOff = Infinity): number[] {
  const d = 2 * r + 1
  const order = xs.map((_, i) => i).sort((a, b) => xs[a] - xs[b])
  const placed: { x: number; y: number }[] = []
  const out = new Array<number>(xs.length).fill(0)
  for (const i of order) {
    const near = placed.filter((p) => Math.abs(p.x - xs[i]) < d)
    let best = 0
    for (let k = 0; ; k++) {
      const off = k === 0 ? 0 : (k % 2 ? 1 : -1) * Math.ceil(k / 2) * 2
      if (Math.abs(off) > maxOff) { best = 0; break }
      if (near.every((p) => Math.hypot(p.x - xs[i], p.y - off) >= d)) { best = off; break }
    }
    out[i] = best
    placed.push({ x: xs[i], y: best })
  }
  return out
}

/** Round-number axis ticks covering [lo, hi] in about `count` steps. */
export function niceTicks(lo: number, hi: number, count: number): number[] {
  if (!(hi > lo)) return [lo]
  const raw = (hi - lo) / Math.max(1, count)
  const mag = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((x) => x >= raw - 1e-12) ?? 10 * mag
  const a = Math.floor(lo / step + 1e-9) * step
  const b = Math.ceil(hi / step - 1e-9) * step
  return Array.from({ length: Math.round((b - a) / step) + 1 }, (_, k) => a + k * step)
}

/** Spread label y positions at least `gap` apart, keeping them inside [top, bottom]. Input order is kept. */
export function spreadLabels(ys: number[], gap: number, top: number, bottom: number): number[] {
  const order = ys.map((y, i) => ({ y, i })).sort((p, q) => p.y - q.y)
  const out = order.map((o) => o.y)
  for (let k = 0; k < out.length; k++) out[k] = Math.max(out[k], k ? out[k - 1] + gap : top)
  for (let k = out.length - 1; k >= 0; k--) out[k] = Math.min(out[k], k < out.length - 1 ? out[k + 1] - gap : bottom)
  const res = new Array<number>(ys.length)
  order.forEach((o, k) => { res[o.i] = out[k] })
  return res
}

export function TimeChart({
  labels, series, height = 220, nowIdx, nowLabel = 'Now', yFmt = (v) => v.toFixed(0), yDomain, annotations, onAnnotation, shade, cursor, onCursor, zero, xTickEvery, className, forecastLabel = 'Forecast', hideForecastZone, legend,
  yTicks = 4, niceY, xAxis = true, directLabels, annotationStyle = 'pin',
}: {
  labels: string[]
  series: Series[]
  height?: number
  nowIdx?: number
  nowLabel?: string
  yFmt?: (v: number) => string
  yDomain?: [number, number]
  annotations?: Annotation[]
  onAnnotation?: (id: string) => void
  shade?: [number, number, string][]
  cursor?: number
  onCursor?: (i: number) => void
  zero?: boolean
  xTickEvery?: number
  className?: string
  forecastLabel?: string
  hideForecastZone?: boolean
  legend?: boolean
  /** Approximate number of y-axis intervals. */
  yTicks?: number
  /** Snap the y-domain and ticks to round numbers. */
  niceY?: boolean
  /** Draw the date labels under the plot (off when a chart below shares the axis). */
  xAxis?: boolean
  /** Name each labelled series at its right end; only series that run to the last point are labelled. */
  directLabels?: boolean
  /** 'pin': marker at the top with a leader line. 'point': marker sits on the line itself. */
  annotationStyle?: 'pin' | 'point'
}) {
  const [ref, w] = useWidth<HTMLDivElement>()
  const cs = useChartStyle()
  const [hover, setHover] = useState<number | null>(null)
  // Phone: a full-size chart keeps a readable plot height once the width shrinks; deliberate mini charts (< 100px) keep theirs.
  const phone = useIsPhone()
  if (phone && height >= 100 && height < 140) height = 140
  // Phone: 12px axis text needs a little more room under the plot so the lowest y label clears the x labels.
  const pad = { l: 46, r: directLabels ? 96 : 14, t: 14, b: xAxis ? (phone ? 28 : 22) : 6 }
  const n = labels.length
  const [lo0, hi0] = useMemo(() => {
    if (yDomain) return yDomain
    const all: number[] = []
    for (const s of series) {
      s.values.forEach((v) => { const c = clean(v); if (c != null) all.push(c) })
      s.band?.lo.forEach((v) => { const c = clean(v); if (c != null) all.push(c) })
      s.band?.hi.forEach((v) => { const c = clean(v); if (c != null) all.push(c) })
    }
    if (zero) all.push(0)
    const mn = Math.min(...all)
    const mx = Math.max(...all)
    const p = (mx - mn) * 0.08 || 1
    return [mn - p, mx + p]
  }, [series, yDomain, zero])
  const yCount = Math.max(2, Math.round(yTicks * cs.tickScale))
  const nice = !!niceY && !yDomain
  const yScale = scaleLinear().domain([lo0, hi0]).range([height - pad.b, pad.t])
  if (nice) yScale.nice(yCount)
  const [lo, hi] = yScale.domain() as [number, number]
  const yt = nice ? yScale.ticks(yCount) : Array.from({ length: yCount + 1 }, (_, k) => lo0 + ((hi0 - lo0) * k) / yCount)
  const xScale = scaleLinear().domain([0, Math.max(1, n - 1)]).range([pad.l, w - pad.l - pad.r + pad.l])
  const sx = (i: number) => xScale(i)
  const sy = (v: number) => yScale(v)
  const every = xTickEvery ?? Math.max(1, Math.ceil(n / Math.max(3, Math.round(Math.floor(w / (phone ? 110 : 90)) * cs.tickScale))))
  const isVal = (v: number | null | undefined) => clean(v) != null
  const lineGen = d3line<number | null>().defined(isVal).x((_, i) => sx(i)).y((v) => sy(v as number)).curve(cs.curveFactory)
  const areaGen = d3area<number | null>().defined(isVal).x((_, i) => sx(i)).y0(sy(lo)).y1((v) => sy(v as number)).curve(cs.curveFactory)
  const showPoints = cs.points && n <= 80
  const gradIds = series.filter((s) => s.area || cs.area)
  const active = hover ?? cursor ?? null

  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const r = (e.currentTarget as SVGSVGElement).getBoundingClientRect()
    const x = e.clientX - r.left
    const i = Math.round(((x - pad.l) / (w - pad.l - pad.r)) * (n - 1))
    setHover(Math.max(0, Math.min(n - 1, i)))
  }
  // Touch: tap a point (or slide along the chart) to read it; the readout stays until the next tap.
  const onTouch = (e: React.TouchEvent<SVGSVGElement>) => {
    const t = e.touches[0]
    if (!t) return
    const r = e.currentTarget.getBoundingClientRect()
    const i = Math.round(((t.clientX - r.left - pad.l) / (w - pad.l - pad.r)) * (n - 1))
    setHover(Math.max(0, Math.min(n - 1, i)))
  }
  const pid = useMemo(() => `h${Math.random().toString(36).slice(2, 8)}`, [])

  return (
    <div ref={ref} className={cx('relative w-full select-none', className)}>
      {legend && (
        <div className="mb-2 flex flex-wrap gap-x-4 gap-y-1">
          {series.filter((s) => s.label).map((s) => (
            <span key={s.id} className="flex items-center gap-1.5 text-[11px] text-mute">
              <svg width="16" height="6">{s.points ? <circle cx="8" cy="3" r="2.6" fill={s.color} /> : <line x1="0" y1="3" x2="16" y2="3" stroke={s.color} strokeWidth={s.width ?? 1.5} strokeDasharray={s.style === 'dashed' ? '4 3' : s.style === 'dotted' ? '1 2.5' : undefined} />}</svg>
              {s.label}
            </span>
          ))}
        </div>
      )}
      <svg width={w} height={height} onMouseMove={onMove} onMouseLeave={() => setHover(null)} onTouchStart={onTouch} onTouchMove={onTouch} onClick={() => active != null && onCursor?.(active)} className={cx(onCursor ? 'cursor-crosshair' : '', 'max-md:touch-pan-y')}>
        <defs>
          <pattern id={`${pid}-hatch`} width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <line x1="0" y1="0" x2="0" y2="6" stroke="var(--color-fct)" strokeOpacity="0.09" strokeWidth="2" />
          </pattern>
          <pattern id={`${pid}-infhatch`} width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(-45)">
            <line x1="0" y1="0" x2="0" y2="5" stroke="var(--color-inf)" strokeOpacity="0.35" strokeWidth="1.2" />
          </pattern>
          {gradIds.map((s) => (
            <linearGradient key={s.id} id={`${pid}-g-${s.id}`} x1="0" x2="0" y1="0" y2="1">
              <stop offset="0%" stopColor={s.color} stopOpacity="0.22" />
              <stop offset="100%" stopColor={s.color} stopOpacity="0" />
            </linearGradient>
          ))}
        </defs>
        {/* shaded low-information periods */}
        {shade?.map(([a, b, label], k) => (
          <g key={k}>
            <rect x={sx(a) - 2} y={pad.t} width={Math.max(4, sx(b) - sx(a) + 4)} height={height - pad.t - pad.b} fill="var(--color-warn)" fillOpacity={0.06} />
            <text x={sx(a)} y={phone ? pad.t + 13 + k * 14 : pad.t + 9} className={`fill-warn ${cs.fontClass}`} fontSize={phone ? 10 : 8.5} opacity={0.8}>{label}</text>
          </g>
        ))}
        {/* forecast zone */}
        {nowIdx != null && nowIdx < n - 1 && !hideForecastZone && (
          <g>
            <rect x={sx(nowIdx)} y={pad.t} width={sx(n - 1) - sx(nowIdx)} height={height - pad.t - pad.b} fill={`url(#${pid}-hatch)`} />
            <text x={sx(nowIdx) + 6} y={height - pad.b - 6} className="fill-fct font-sans" fontSize={10} fontWeight={500} opacity={0.8}>{forecastLabel} →</text>
            <text x={sx(nowIdx) - 6} y={height - pad.b - 6} textAnchor="end" className="fill-dim font-sans" fontSize={10} fontWeight={500}>← Observed</text>
          </g>
        )}
        {cs.grid === 'xy' && labels.map((_, i) => (i % every === 0 ? <line key={`vx-${i}`} x1={sx(i)} x2={sx(i)} y1={pad.t} y2={height - pad.b} stroke="var(--color-line)" strokeOpacity={0.5} /> : null))}
        {yt.map((v, k) => (
          <g key={k}>
            {(cs.grid !== 'none' || k === 0) && <line x1={pad.l} x2={w - pad.r} y1={sy(v)} y2={sy(v)} stroke="var(--color-line)" strokeDasharray={k === 0 ? undefined : '2 4'} />}
            <text x={pad.l - 8} y={sy(v) + 3} textAnchor="end" className={`fill-dim ${cs.fontClass}`} fontSize={9.5}>{yFmt(v)}</text>
          </g>
        ))}
        {zero && lo < 0 && hi > 0 && <line x1={pad.l} x2={w - pad.r} y1={sy(0)} y2={sy(0)} stroke="var(--color-mute)" strokeOpacity={0.5} />}
        {xAxis && labels.map((l, i) => (i % every === 0 ? <text key={i} x={sx(i)} y={height - 6} textAnchor="middle" className={`fill-dim ${cs.fontClass}`} fontSize={9.5}>{l}</text> : null))}
        {series.map((s) => s.band && (
          <path key={`b-${s.id}`}
            d={d3area<number>().defined((i) => isVal(s.band!.lo[i]) && isVal(s.band!.hi[i])).x((i) => sx(i)).y0((i) => sy(s.band!.lo[i]!)).y1((i) => sy(s.band!.hi[i]!)).curve(cs.curveFactory)(s.band.lo.map((_, i) => i)) ?? ''}
            fill={s.band.hatched ? `url(#${pid}-infhatch)` : s.color} fillOpacity={s.band.hatched ? 1 : 0.13} stroke={s.color} strokeOpacity={0.25} strokeDasharray="2 3" style={{ transition: cs.transition }} />
        ))}
        {gradIds.map((s) => (
          <path key={`a-${s.id}`} d={areaGen(s.values) ?? ''} fill={`url(#${pid}-g-${s.id})`} style={{ transition: cs.transition }} />
        ))}
        {series.map((s) => (
          <path key={s.id} d={lineGen(s.values) ?? ''} fill="none" stroke={s.color} strokeOpacity={s.opacity} strokeWidth={(s.width ?? 1.6) * cs.strokeScale} strokeDasharray={s.style === 'dashed' ? '5 4' : s.style === 'dotted' ? '1.5 3' : undefined} strokeLinejoin="round" strokeLinecap="round" style={{ transition: cs.transition }} />
        ))}
        {series.map((s) => ((showPoints || s.points) && (
          <g key={`pt-${s.id}`} pointerEvents="none">
            {s.values.map((v, i) => (clean(v) == null ? null : <circle key={i} cx={sx(i)} cy={sy(v as number)} r={s.points ? 3.2 : 2} fill={s.color} fillOpacity={s.opacity} stroke={s.points ? 'var(--color-ink)' : undefined} strokeWidth={s.points ? 1 : undefined} style={{ transition: cs.transition }} />))}
          </g>
        )))}
        {nowIdx != null && (
          <g>
            <line x1={sx(nowIdx)} x2={sx(nowIdx)} y1={pad.t - 6} y2={height - pad.b} stroke="var(--color-bone)" strokeOpacity={0.6} />
            <rect x={sx(nowIdx) - 19} y={0} width={38} height={14} rx={2} fill="var(--color-bone)" />
            <text x={sx(nowIdx)} y={9.5} textAnchor="middle" className="fill-ink font-sans" fontSize={10} fontWeight={600}>{nowLabel}</text>
          </g>
        )}
        {directLabels && (() => {
          const ends = series.filter((s) => s.label && clean(s.values[n - 1]) != null)
          const ys = spreadLabels(ends.map((s) => sy(s.values[n - 1] as number)), 11, pad.t + 4, height - pad.b - 2)
          return ends.map((s, k) => (
            <g key={`dl-${s.id}`} pointerEvents="none">
              <line x1={sx(n - 1) + 3} x2={sx(n - 1) + 9} y1={sy(s.values[n - 1] as number)} y2={ys[k]} stroke={s.color} strokeOpacity={0.6} />
              <text x={sx(n - 1) + 12} y={ys[k] + 3} className={`fill-mute ${cs.fontClass}`} fontSize={9.5}>{s.label}</text>
            </g>
          ))
        })()}
        {annotationStyle === 'point' && annotations?.map((a) => {
          const host = series.find((s) => clean(s.values[a.i]) != null)
          const y = host ? sy(host.values[a.i] as number) : pad.t + 4
          const c = a.color ?? 'var(--color-dec)'
          return (
            <g key={a.id} className="cursor-pointer" onClick={(e) => { e.stopPropagation(); onAnnotation?.(a.id) }}>
              <circle cx={sx(a.i)} cy={y} r={11} fill="transparent" />
              {a.active && <circle cx={sx(a.i)} cy={y} r={10} fill="none" stroke={c} strokeOpacity={0.6} />}
              <rect x={sx(a.i) - 4.5} y={y - 4.5} width={9} height={9} transform={`rotate(45 ${sx(a.i)} ${y})`} fill={a.hollow ? 'var(--color-panel)' : c} stroke={c} strokeWidth={1.5} />
              <title>{a.label}</title>
            </g>
          )
        })}
        {annotationStyle === 'pin' && annotations?.map((a) => {
          const base = series[0]?.values[a.i]
          const y = clean(base) != null ? sy(base as number) : pad.t + 10
          const c = a.color ?? 'var(--color-dec)'
          return (
            <g key={a.id} className="cursor-pointer" onClick={(e) => { e.stopPropagation(); onAnnotation?.(a.id) }}>
              <line x1={sx(a.i)} x2={sx(a.i)} y1={y} y2={pad.t + 4} stroke={c} strokeOpacity={0.5} strokeDasharray="2 2" />
              <rect x={sx(a.i) - 5.5} y={pad.t - 1.5} width={11} height={11} transform={`rotate(45 ${sx(a.i)} ${pad.t + 4})`} fill={a.hollow ? 'var(--color-panel)' : c} stroke={c} strokeWidth={1.5} />
              {a.active && <circle cx={sx(a.i)} cy={pad.t + 4} r={11} fill="none" stroke={c} strokeOpacity={0.6} />}
              <circle cx={sx(a.i)} cy={y} r={3} fill="var(--color-ink)" stroke={c} strokeWidth={1.5} />
              <title>{a.label}</title>
            </g>
          )
        })}
        {active != null && (
          <g pointerEvents="none">
            <line x1={sx(active)} x2={sx(active)} y1={pad.t} y2={height - pad.b} stroke="var(--color-mute)" strokeOpacity={0.5} />
            {series.map((s) => { const v = clean(s.values[active]); return v == null ? null : <circle key={s.id} cx={sx(active)} cy={sy(v)} r={3} fill={s.color} stroke="var(--color-ink)" /> })}
          </g>
        )}
      </svg>
      {hover != null && (
        <div className="pointer-events-none absolute top-1 z-10 min-w-[140px] rounded-[4px] border border-line2 bg-ink/95 px-2.5 py-1.5 shadow-xl backdrop-blur" style={{ left: Math.min(w - 170, Math.max(0, sx(hover) + 10)) }}>
          <div className="mb-1 font-mono text-[10px] text-mute">{labels[hover]}{nowIdx != null && hover > nowIdx && <span className="ml-1.5 text-fct">· forecast</span>}</div>
          {series.map((s) => {
            const v = clean(s.values[hover])
            if (v == null) return null
            const blo = clean(s.band?.lo[hover])
            const bhi = clean(s.band?.hi[hover])
            return (
              <div key={s.id} className="flex items-center justify-between gap-3 font-mono text-[10.5px]">
                <span className="flex items-center gap-1.5 text-mute"><span className="inline-block h-0.5 w-2.5" style={{ background: s.color }} />{s.label ?? s.id}</span>
                <span className="text-bone">{(s.fmt ?? yFmt)(v)}{blo != null && bhi != null && <span className="ml-1 text-dim">[{(s.fmt ?? yFmt)(blo)}–{(s.fmt ?? yFmt)(bhi)}]</span>}</span>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

/** Forecast distribution: approximated as skew-normal from p10/mean/p90. */
export function Distribution({ p10, mean, p90, unit = '%', height = 110, color = 'var(--color-fct)', label }: { p10: number; mean: number; p90: number; unit?: string; height?: number; color?: string; label?: string }) {
  const [ref, w] = useWidth<HTMLDivElement>()
  const cs = useChartStyle()
  const sdL = Math.max(0.1, (mean - p10) / 1.2816)
  const sdR = Math.max(0.1, (p90 - mean) / 1.2816)
  const lo = Math.min(p10 - sdL * 1.6, 0)
  const hi = Math.max(p90 + sdR * 1.6, 0)
  const N = 80
  const pts = Array.from({ length: N + 1 }, (_, k) => {
    const x = lo + ((hi - lo) * k) / N
    const sd = x < mean ? sdL : sdR
    return { x, y: Math.exp(-0.5 * ((x - mean) / sd) ** 2) }
  })
  const pad = { l: 14, r: 14, t: 20, b: 22 }
  const sx = (x: number) => pad.l + ((x - lo) / (hi - lo)) * (w - pad.l - pad.r)
  const sy = (y: number) => height - pad.b - y * (height - pad.t - pad.b)
  const lineGen = d3line<{ x: number; y: number }>().x((p) => sx(p.x)).y((p) => sy(p.y)).curve(cs.curveFactory)
  const areaGen = d3area<{ x: number; y: number }>().x((p) => sx(p.x)).y0(sy(0)).y1((p) => sy(p.y)).curve(cs.curveFactory)
  const inner = pts.filter((p) => p.x >= p10 && p.x <= p90)
  const d = lineGen(pts) ?? ''
  const dIn = inner.length ? areaGen(inner) ?? '' : ''
  const pctNeg = (() => {
    // probability mass below zero (approx)
    const below = pts.filter((p) => p.x < 0).reduce((s, p) => s + p.y, 0)
    const tot = pts.reduce((s, p) => s + p.y, 0)
    return Math.round((below / tot) * 100)
  })()
  const f = (v: number) => `${v > 0 ? '+' : ''}${v.toFixed(1)}${unit}`
  const fTick = (v: number) => (v === 0 ? '0' : `${v > 0 ? '+' : ''}${+v.toFixed(1)}${unit}`)
  // Same axis treatment as TimeChart: round-number ticks, dashed grid, solid baseline.
  const xt = niceTicks(lo, hi, Math.max(3, Math.round((w / 90) * cs.tickScale))).filter((v) => v >= lo && v <= hi)
  const gridY = [0.33, 0.66, 1]
  const gid = useMemo(() => `d${Math.random().toString(36).slice(2, 8)}`, [])
  const tagW = f(mean).length * 6.4 + 10
  return (
    <div ref={ref} className="w-full">
      <div className="mb-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-mute">
        <span className="flex items-center gap-1.5"><svg width="16" height="6"><line x1="0" y1="3" x2="16" y2="3" stroke={color} strokeWidth={1.5} /></svg>{label ?? 'Forecast distribution'}</span>
        <span className="flex items-center gap-1.5"><span className="h-2 w-3 rounded-[1px] border border-dashed" style={{ background: `color-mix(in srgb, ${color} 13%, transparent)`, borderColor: `color-mix(in srgb, ${color} 40%, transparent)` }} />p10–p90</span>
      </div>
      <svg width={w} height={height}>
        <defs>
          <linearGradient id={`${gid}-g`} x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity="0.22" />
            <stop offset="100%" stopColor={color} stopOpacity="0" />
          </linearGradient>
        </defs>
        {cs.grid !== 'none' && gridY.map((g) => <line key={g} x1={pad.l} x2={w - pad.r} y1={sy(g)} y2={sy(g)} stroke="var(--color-line)" strokeDasharray="2 4" />)}
        {cs.grid === 'xy' && xt.map((v) => <line key={`vx-${v}`} x1={sx(v)} x2={sx(v)} y1={pad.t} y2={sy(0)} stroke="var(--color-line)" strokeOpacity={0.5} />)}
        <path d={areaGen(pts) ?? ''} fill={`url(#${gid}-g)`} style={{ transition: cs.transition }} />
        <path d={dIn} fill={color} fillOpacity={0.13} stroke={color} strokeOpacity={0.25} strokeDasharray="2 3" style={{ transition: cs.transition }} />
        <line x1={pad.l} x2={w - pad.r} y1={sy(0)} y2={sy(0)} stroke="var(--color-line)" />
        <line x1={sx(0)} x2={sx(0)} y1={pad.t} y2={sy(0)} stroke="var(--color-mute)" strokeOpacity={0.5} />
        <path d={d} fill="none" stroke={color} strokeWidth={1.6 * cs.strokeScale} strokeLinejoin="round" strokeLinecap="round" style={{ transition: cs.transition }} />
        <line x1={sx(mean)} x2={sx(mean)} y1={pad.t - 6} y2={sy(0)} stroke="var(--color-bone)" strokeOpacity={0.6} />
        <rect x={sx(mean) - tagW / 2} y={0} width={tagW} height={14} rx={2} fill="var(--color-bone)" />
        <text x={sx(mean)} y={10} textAnchor="middle" className={`fill-ink ${cs.fontClass}`} fontSize={10} fontWeight={600}>{f(mean)}</text>
        {xt.map((v) => <text key={v} x={sx(v)} y={height - 6} textAnchor="middle" className={`fill-dim ${cs.fontClass}`} fontSize={9.5}>{fTick(v)}</text>)}
      </svg>
      <div className="mt-1.5 flex justify-between text-[11px] text-dim">
        <span className="num">p10 {f(p10)} · p90 {f(p90)}</span>
        <span>P(loss) <span className="num text-mute">≈ {pctNeg}%</span></span>
      </div>
    </div>
  )
}

export function HBars({ rows, max, fmt = (v) => v.toFixed(2), signed }: { rows: { label: ReactNode; value: number; color?: string; hatched?: boolean; right?: ReactNode }[]; max?: number; fmt?: (v: number) => string; signed?: boolean }) {
  const cs = useChartStyle()
  const m = max ?? Math.max(...rows.map((r) => Math.abs(r.value)), 0.0001)
  return (
    <div className="space-y-2">
      {rows.map((r, k) => (
        <div key={k} className="grid grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)_auto] items-center gap-3">
          <div className="truncate text-[12px] text-mute">{r.label}</div>
          <div className={cx('relative h-2 rounded-sm bg-line/60', signed && 'overflow-visible')}>
            {signed && <div className="absolute inset-y-[-3px] left-1/2 w-px bg-line2" />}
            <div
              className={cx('absolute inset-y-0 rounded-sm', r.hatched && 'hatch-inf border')}
              style={{
                left: signed ? (r.value >= 0 ? '50%' : `${50 - (Math.abs(r.value) / m) * 50}%`) : 0,
                width: `${(Math.abs(r.value) / m) * (signed ? 50 : 100)}%`,
                background: r.hatched ? undefined : (r.color ?? (signed ? (r.value >= 0 ? 'var(--color-up)' : 'var(--color-down)') : 'var(--color-bone)')),
                borderColor: r.color,
                transition: cs.motion ? 'width .35s ease, left .35s ease' : undefined,
              }}
            />
          </div>
          <div className="num w-14 text-right text-[11px] text-bone">{r.right ?? fmt(r.value)}</div>
        </div>
      ))}
    </div>
  )
}

export function useInterval(fn: () => void, ms: number | null) {
  const saved = useRef(fn)
  saved.current = fn
  useEffect(() => {
    if (ms == null) return
    const id = setInterval(() => saved.current(), ms)
    return () => clearInterval(id)
  }, [ms])
}
