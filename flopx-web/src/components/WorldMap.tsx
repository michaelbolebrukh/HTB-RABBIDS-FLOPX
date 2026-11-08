import { useEffect, useLayoutEffect, useMemo, useRef, useState, type PointerEvent as RPointerEvent } from 'react'
import { geoDistance, geoGraticule10, geoNaturalEarth1, geoOrthographic, geoPath } from 'd3-geo'
import { feature, mesh } from 'topojson-client'
import type { FeatureCollection, MultiLineString } from 'geojson'
import world from 'world-atlas/countries-110m.json'
import { NOW_IDX, REGIONS, type Region } from '@/data/core'
import { PROJECTS, type Project } from '@/data/infra'
import { pressureAt, pressureColor, pressureWord, scenarioDelta, uncAt } from '@/lib/model'
import { continentLeaders, type Continent } from '@/lib/continents'
import type { Scenario, XLayer } from '@/lib/store'
import { monoWidth, place, type Box } from '@/lib/labels'

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const topo = world as any
const LAND = feature(topo, topo.objects.land) as unknown as FeatureCollection
const BORDERS = mesh(topo, topo.objects.countries, (a: unknown, b: unknown) => a !== b) as unknown as MultiLineString
const SPHERE = { type: 'Sphere' } as const

export type MapProjection = 'flat' | 'globe'
const PROJ_KEY = 'flopx.mapProjection'
function readProjection(): MapProjection {
  try { return localStorage.getItem(PROJ_KEY) === 'globe' ? 'globe' : 'flat' } catch { return 'flat' }
}
/** Globe rotation [λ, φ] that puts lon/lat at the centre (tilt clamped so the poles stay readable). */
const centreOn = (lon: number, lat: number): [number, number] => [-lon, Math.max(-60, Math.min(60, -lat))]
/** Shortest-way interpolation of a rotation, so recentring never spins the long way round. */
export function lerpRotation(a: [number, number], b: [number, number], k: number): [number, number] {
  const d = ((((b[0] - a[0]) % 360) + 540) % 360) - 180
  return [a[0] + d * k, a[1] + (b[1] - a[1]) * k]
}

export const ZOOM_MIN = 1
export const ZOOM_MAX = 8
export interface MapView { z: number; pan: [number, number] }
/** Keep the zoomed flat map covering the viewport: at zoom z the content overhangs each edge by (size/2)(z-1). */
export function clampPan(pan: [number, number], z: number, w: number, h: number): [number, number] {
  const mx = (w / 2) * (z - 1), my = (h / 2) * (z - 1)
  return [Math.max(-mx, Math.min(mx, pan[0])), Math.max(-my, Math.min(my, pan[1]))]
}
/** Zoom to z2 keeping the screen point `at` fixed (pass the viewport centre for button zoom). */
export function zoomAt(v: MapView, z2: number, at: [number, number], w: number, h: number): MapView {
  const z = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, z2))
  const cx = w / 2, cy = h / 2, k = z / v.z
  const pan: [number, number] = [at[0] - cx - (at[0] - cx - v.pan[0]) * k, at[1] - cy - (at[1] - cy - v.pan[1]) * k]
  return { z, pan: clampPan(pan, z, w, h) }
}

export interface MapProps {
  layers: XLayer[]
  reveal?: number
  t?: number
  selected?: string | null
  selectedProject?: string | null
  compare?: string[]
  scenario?: Scenario | null
  onRegion?: (id: string) => void
  onProject?: (id: string) => void
  compact?: boolean
  /** Continent callouts on the biggest pressure ring per continent. Defaults to on, except in compact maps. */
  callouts?: boolean
  minHeight?: number
}

export function WorldMap({ layers, reveal = 100, t = NOW_IDX, selected, selectedProject, compare = [], scenario = null, onRegion, onProject, compact, callouts = !compact, minHeight = 260 }: MapProps) {
  const ref = useRef<HTMLDivElement>(null)
  const [size, setSize] = useState({ w: 900, h: 460 })
  const [hover, setHover] = useState<string | null>(null)
  const [mode, setMode] = useState<MapProjection>(readProjection)
  const globe = mode === 'globe'
  const [rot, setRot] = useState<[number, number]>(() => centreOn(-35, 30))
  const rotRef = useRef(rot)
  rotRef.current = rot
  const [view, setView] = useState<MapView>({ z: 1, pan: [0, 0] })
  const drag = useRef<{ x: number; y: number; r: [number, number]; pan: [number, number]; moved: boolean } | null>(null)
  const dragged = useRef(false)
  const toggle = () => {
    const next: MapProjection = globe ? 'flat' : 'globe'
    setMode(next)
    setView({ z: 1, pan: [0, 0] })
    try { localStorage.setItem(PROJ_KEY, next) } catch { /* private mode: not remembered */ }
  }
  // Recentre the globe on the selected region with a short eased animation.
  const sel = REGIONS.find((r) => r.id === selected)
  useEffect(() => {
    if (!globe || !sel) return
    const from = rotRef.current
    const to = centreOn(sel.lon, sel.lat)
    const t0 = performance.now()
    let raf = 0
    const step = (now: number) => {
      const k = Math.min(1, (now - t0) / 650)
      setRot(lerpRotation(from, to, 1 - (1 - k) ** 3))
      if (k < 1) raf = requestAnimationFrame(step)
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }, [globe, sel])
  useLayoutEffect(() => {
    if (!ref.current) return
    const ro = new ResizeObserver(([e]) => setSize({ w: Math.max(300, e.contentRect.width), h: Math.max(minHeight, e.contentRect.height) }))
    ro.observe(ref.current)
    return () => ro.disconnect()
  }, [minHeight])
  const sizeRef = useRef(size)
  sizeRef.current = size
  const zoomBy = (f: number, at?: [number, number]) => setView((v) => {
    const { w, h } = sizeRef.current
    return zoomAt(v, v.z * f, at ?? [w / 2, h / 2], w, h)
  })
  // Pinch or ⌘/Ctrl + scroll zooms around the cursor; a plain scroll still scrolls the page.
  // Native listener because React's wheel handler is passive and cannot preventDefault.
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey && !e.metaKey) return
      e.preventDefault()
      const b = el.getBoundingClientRect()
      const at: [number, number] = [e.clientX - b.left, e.clientY - b.top]
      setView((v) => zoomAt(v, v.z * Math.exp(-e.deltaY * 0.01), at, sizeRef.current.w, sizeRef.current.h))
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [])

  const { proj, land, borders, grat, sphere } = useMemo(() => {
    const proj = globe
      ? geoOrthographic().rotate(rot).fitExtent([[10, 10], [size.w - 10, size.h - 10]], SPHERE)
      : geoNaturalEarth1().fitExtent([[8, 8], [size.w - 8, size.h - 8]], { type: 'MultiPoint', coordinates: [[-165, 72], [175, 72], [-165, -46], [175, -46], [0, 74], [0, -48]] })
    // Zoom about the viewport centre, then pan (flat only; the globe moves by rotation).
    const [tx, ty] = proj.translate(), cx = size.w / 2, cy = size.h / 2
    proj.scale(proj.scale() * view.z).translate([cx + (tx - cx) * view.z + (globe ? 0 : view.pan[0]), cy + (ty - cy) * view.z + (globe ? 0 : view.pan[1])])
    const path = geoPath(proj)
    return { proj, land: path(LAND) ?? '', borders: path(BORDERS) ?? '', grat: path(geoGraticule10()) ?? '', sphere: globe ? path(SPHERE) ?? '' : '' }
  }, [size.w, size.h, globe, rot, view])

  // Drag rotates the globe (or pans a zoomed flat map); a drag never counts as a click on a region or project.
  const canDrag = globe || view.z > 1
  const onDown = (e: RPointerEvent) => {
    if (!canDrag) return
    drag.current = { x: e.clientX, y: e.clientY, r: rot, pan: view.pan, moved: false }
    dragged.current = false
  }
  const onMove = (e: RPointerEvent) => {
    const d = drag.current
    if (!d) return
    const dx = e.clientX - d.x, dy = e.clientY - d.y
    if (!d.moved && Math.hypot(dx, dy) < 4) return
    if (!d.moved) { d.moved = true; (e.currentTarget as Element).setPointerCapture(e.pointerId); setHover(null) }
    if (!globe) { setView((v) => ({ ...v, pan: clampPan([d.pan[0] + dx, d.pan[1] + dy], v.z, size.w, size.h) })); return }
    const k = 90 / proj.scale()
    setRot([d.r[0] + dx * k * 1.4, Math.max(-60, Math.min(60, d.r[1] - dy * k * 1.4))])
  }
  const onUp = () => {
    dragged.current = !!drag.current?.moved
    drag.current = null
  }
  const guard = <T,>(fn?: (id: T) => void) => (id: T) => { if (dragged.current) { dragged.current = false; return } fn?.(id) }
  const clickRegion = guard(onRegion)
  const clickProject = guard(onProject)
  const centre: [number, number] = [-rot[0], -rot[1]]
  /** On the globe only the near hemisphere is drawn (small margin so markers don't sit on the limb). */
  const vis = (lon: number, lat: number) => !globe || geoDistance([lon, lat], centre) < Math.PI / 2 - 0.05

  const has = (l: XLayer) => layers.includes(l)
  const inferredOpacity = reveal / 100
  const rawOpacity = 0.25 + 0.75 * (1 - reveal / 100)
  const future = t > NOW_IDX
  // Markers grow a little with zoom, much less than the map, so zooming in separates crowded regions.
  const scale = (globe ? Math.min(1.25, Math.max(0.55, proj.scale() / view.z / 330)) : Math.min(1.25, size.w / 1100)) * view.z ** 0.3
  const pt = (lon: number, lat: number) => proj([lon, lat]) ?? [0, 0]
  const rOf = (r: Region) => (14 + Math.sqrt(r.opMW + r.conMW * 0.5) * 0.42) * scale * (compact ? 0.8 : 1)

  const regions = REGIONS.filter((r) => vis(r.lon, r.lat))
  /** Pressure ring radius: grows with capacity and inferred pressure. */
  const ringOf = (r: Region, p: number) => rOf(r) * (0.5 + (p / 100) * 0.75)
  const labels = layoutLabels({ regions, pt, rOf, has, selected: selected ?? null, compare, compact: !!compact, t, scenario, w: size.w, h: size.h })
  const hoverRegion = hover ? regions.find((r) => r.id === hover) : undefined
  return (
    <div ref={ref} className="relative h-full w-full overflow-hidden" style={{ minHeight }}>
      <svg width={size.w} height={size.h} className={`block ${canDrag ? 'touch-none' : ''} ${drag.current?.moved ? 'cursor-grabbing' : canDrag ? 'cursor-grab' : ''}`}
        onPointerDown={onDown} onPointerMove={onMove} onPointerUp={onUp} onPointerCancel={onUp}>
        <defs>
          <pattern id="dots" width="4" height="4" patternUnits="userSpaceOnUse">
            <circle cx="1" cy="1" r="0.7" fill="var(--color-map-dot)" />
          </pattern>
          <pattern id="nodata" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <line x1="0" y1="0" x2="0" y2="5" stroke="var(--color-dim)" strokeWidth="1" />
          </pattern>
          <pattern id="effhatch" width="4" height="4" patternUnits="userSpaceOnUse" patternTransform="rotate(-45)">
            <line x1="0" y1="0" x2="0" y2="4" stroke="var(--color-inf)" strokeWidth="1.2" strokeOpacity="0.7" />
          </pattern>
          <radialGradient id="globe-shade" cx="42%" cy="38%" r="62%">
            <stop offset="0%" stopColor="var(--color-ocean)" stopOpacity={0} />
            <stop offset="80%" stopColor="var(--color-ocean-edge)" stopOpacity={0.35} />
            <stop offset="100%" stopColor="var(--color-ocean-edge)" stopOpacity={0.85} />
          </radialGradient>
          {regions.map((r) => {
            const p = pressureAt(r, t, scenario)
            return (
              <radialGradient key={r.id} id={`pg-${r.id}`}>
                <stop offset="0%" stopColor={pressureColor(p, 'g')} style={{ stopOpacity: 'var(--glow-core)' }} />
                <stop offset="45%" stopColor={pressureColor(p, 'g')} style={{ stopOpacity: 'var(--glow-mid)' }} />
                <stop offset="100%" stopColor={pressureColor(p, 'g')} stopOpacity={0} />
              </radialGradient>
            )
          })}
        </defs>
        {globe && <path d={sphere} fill="var(--color-ocean)" stroke="var(--color-line2)" strokeWidth={1} />}
        <path d={grat} fill="none" stroke="var(--color-line)" strokeOpacity={0.5} strokeWidth={0.6} />
        <path d={land} fill="var(--color-land)" />
        <path d={land} fill="url(#dots)" />
        <path d={borders} fill="none" stroke="var(--color-line)" strokeWidth={0.5} />
        <path d={land} fill="none" stroke="var(--color-line2)" strokeWidth={0.6} />
        {globe && <path d={sphere} fill="url(#globe-shade)" pointerEvents="none" />}

        {/* Inferred pressure fields */}
        {has('pressure') && regions.map((r) => {
          if (r.quality === 'none') return null
          const [x, y] = pt(r.lon, r.lat)
          const p = pressureAt(r, t, scenario)!
          const rad = ringOf(r, p)
          const u = uncAt(r, t, scenario)
          return (
            <g key={`pf-${r.id}`} opacity={inferredOpacity} pointerEvents="none" style={{ transition: 'opacity .4s' }}>
              <circle cx={x} cy={y} r={rad * 1.35} fill={`url(#pg-${r.id})`} style={{ transition: 'r .4s' }} />
              <circle cx={x} cy={y} r={rad * (1 + u / 60)} fill="none" stroke="var(--color-inf)" strokeOpacity={0.45} strokeDasharray={future ? '2 3' : '1 3'} strokeWidth={1} />
              {(r.quality === 'sparse' || r.quality === 'experimental') && <circle cx={x} cy={y} r={rad * (1 + u / 34)} fill="none" stroke="var(--color-inf)" strokeOpacity={0.3} strokeDasharray="1 4" />}
              {scenarioDelta(r, scenario, t) > 0 && <circle cx={x} cy={y} r={rad * 1.15} fill="none" stroke="var(--color-dec)" strokeWidth={1.2} strokeDasharray="5 3" />}
            </g>
          )
        })}

        {/* Effective vs announced capacity */}
        {has('effective') && regions.map((r) => {
          if (r.quality === 'none') return null
          const [x, y] = pt(r.lon, r.lat)
          const announced = Math.sqrt(r.announced27) * 0.42 * scale
          const eff = Math.sqrt((r.eff27[0] + r.eff27[1]) / 2) * 0.42 * scale
          return (
            <g key={`ef-${r.id}`} pointerEvents="none">
              <circle cx={x} cy={y} r={announced} fill="none" stroke="var(--color-bone)" strokeOpacity={0.55} strokeDasharray="2 2" />
              <circle cx={x} cy={y} r={eff} fill="url(#effhatch)" stroke="var(--color-inf)" strokeOpacity={0.8} />
            </g>
          )
        })}

        {/* Pipeline projects */}
        {has('pipeline') && PROJECTS.filter((p) => vis(p.lon, p.lat)).map((p) => <ProjectGlyph key={p.id} p={p} xy={pt(p.lon, p.lat)} scale={scale} selected={selectedProject === p.id} opacity={rawOpacity} onClick={clickProject} t={t} />)}

        {/* Region anchors + raw observations */}
        {regions.map((r) => {
          const [x, y] = pt(r.lon, r.lat)
          const isSel = selected === r.id
          const cmp = compare.indexOf(r.id)
          const none = r.quality === 'none'
          const hovered = hover === r.id
          return (
            <g key={r.id} className="cursor-pointer" onClick={() => clickRegion(r.id)} onMouseEnter={() => setHover(r.id)} onMouseLeave={() => setHover(null)}>
              <circle cx={x} cy={y} r={Math.max(14, rOf(r) * 0.8)} fill="transparent" />
              {none ? (
                <g>
                  <circle cx={x} cy={y} r={10 * scale} fill="url(#nodata)" stroke="var(--color-dim)" strokeDasharray="2 2" />
                  {!compact && <text x={x + 14} y={y + 3} className="fill-dim font-mono" fontSize={9}>NO DATA</text>}
                </g>
              ) : (
                <g>
                  <circle cx={x} cy={y} r={2.6} fill="var(--color-bone)" />
                  {(isSel || hovered) && <circle cx={x} cy={y} r={rOf(r) * 0.9} fill="none" stroke="var(--color-bone)" strokeWidth={isSel ? 1.5 : 1} strokeOpacity={isSel ? 1 : 0.5} />}
                </g>
              )}
              {cmp >= 0 && (
                <g>
                  <circle cx={x} cy={y} r={rOf(r) * 1.02} fill="none" stroke="var(--color-fct)" strokeWidth={1.5} />
                  <rect x={x - 7} y={y - rOf(r) * 1.02 - 15} width={14} height={13} rx={2} fill="var(--color-fct)" />
                  <text x={x} y={y - rOf(r) * 1.02 - 5.5} textAnchor="middle" className="fill-ink font-mono" fontSize={9} fontWeight={700}>{cmp === 0 ? 'A' : 'B'}</text>
                </g>
              )}
              {has('power') && r.power > 0 && (
                <rect x={x - 14} y={y + 6 - Math.min(28, r.power / 5)} width={4} height={Math.min(28, r.power / 5)} opacity={rawOpacity} fill={r.power > 90 ? 'var(--color-down)' : 'var(--color-warn)'} fillOpacity={0.85} />
              )}
            </g>
          )
        })}

        {/* Labels: one top layer, placed without overlaps (see layoutLabels) */}
        {regions.map((r) => {
          const L = labels.get(r.id)
          if (!L) return null
          const isSel = selected === r.id
          const p = pressureAt(r, t, scenario)
          return (
            <g key={`lb-${r.id}`} className="cursor-pointer" onClick={() => clickRegion(r.id)} onMouseEnter={() => setHover(r.id)} onMouseLeave={() => setHover(null)}>
              {L.power && <text x={L.power.x + L.power.w} y={L.power.y + 8} textAnchor="end" className="fill-mute font-mono" fontSize={8} {...HALO}>${Math.round(r.power)}</text>}
              {L.rental && (
                <g opacity={isSel ? 1 : Math.max(0.6, rawOpacity)} transform={`translate(${L.rental.x},${L.rental.y})`}>
                  <rect width={50} height={15} rx={2} fill="var(--color-panel)" stroke={r.quality === 'stale' ? 'var(--color-warn)' : 'var(--color-line2)'} strokeDasharray={r.quality === 'stale' ? '2 2' : undefined} />
                  <text x={4} y={10.5} className="fill-bone font-mono" fontSize={9.5}>${r.h100.toFixed(2)}</text>
                  <text x={46} y={10.5} textAnchor="end" className="font-mono" fontSize={8} fill={r.premium > 8 ? 'var(--color-down)' : r.premium < -3 ? 'var(--color-up)' : 'var(--color-mute)'}>{r.premium > 0 ? '+' : ''}{r.premium}</text>
                </g>
              )}
              {L.ties && (
                <g transform={`translate(${L.ties.box.x},${L.ties.box.y})`}>
                  {L.ties.shown.map((tk, k) => (
                    <g key={tk} transform={`translate(${k * (TIE_W + 2)},0)`}>
                      <rect width={TIE_W} height={13} rx={2} fill="color-mix(in oklab, var(--color-dec) 14%, var(--color-panel))" stroke="var(--color-dec)" strokeOpacity={0.6} />
                      <text x={TIE_W / 2} y={9.5} textAnchor="middle" className="font-mono" fontSize={8.5} fill="var(--color-dec)">{tk}</text>
                    </g>
                  ))}
                  {L.ties.more > 0 && <text x={L.ties.shown.length * (TIE_W + 2) + 1} y={9.5} className="font-mono" fontSize={8.5} fill="var(--color-dec)" {...HALO}>+{L.ties.more}</text>}
                </g>
              )}
              {L.name && <rect x={L.name.x - 3} y={L.name.y - 1} width={L.name.w + 6} height={L.name.h + 2} rx={3} pointerEvents="none" style={{ fill: 'var(--color-label-bg)', fillOpacity: 'var(--label-bg-opacity)' }} />}
              {L.name && (
                <text x={L.name.x} y={L.name.y + 9.5} className="font-mono" fontSize={9.5} fill={isSel ? 'var(--color-bone)' : 'var(--color-mute)'} letterSpacing="0.06em" fontWeight={isSel ? 600 : 400} {...HALO}>
                  {r.short}
                  {has('pressure') && p != null && <tspan fill={pressureColor(p)} dx={4}>{p.toFixed(0)}</tspan>}
                </text>
              )}
            </g>
          )
        })}
        {/* One callout per continent, on its biggest pressure ring, placed clear of region labels and each other. */}
        {has('pressure') && callouts && (() => {
          const taken: Box[] = [...labels.values()].flatMap((L) => [L.name, L.rental, L.power, L.ties?.box].filter((b): b is Box => !!b))
          regions.forEach((r) => { const [x, y] = pt(r.lon, r.lat); taken.push({ x: x - 4, y: y - 4, w: 8, h: 8 }) })
          taken.push({ x: 0, y: 0, w: 150, h: 64 }, { x: size.w - 100, y: 0, w: 100, h: 32 }, { x: size.w - 230, y: size.h - 40, w: 230, h: 40 })
          const leaders = continentLeaders(regions.filter((r) => r.quality !== 'none'), (r) => { const p = pressureAt(r, t, scenario); return p == null ? null : ringOf(r, p) })
          return leaders.map(({ continent, region: r }) => {
            const [x, y] = pt(r.lon, r.lat)
            const p = pressureAt(r, t, scenario)!
            const rad = ringOf(r, p) * 1.05
            const note = (r.live && !r.live.pressure?.coverage) ? `${pressureWord(p)} · no H100 asks` : `${pressureWord(p)} · H100 ${r.premium > 0 ? '+' : ''}${r.premium}%`
            const tw = monoWidth(Math.max(continent.length, r.short.length + 4 + note.length), 9, 0.04)
            const [pdx, pdy, lead] = CALLOUT_DIR[continent]
            // Preferred direction first, then the other diagonals and verticals; each at growing leader lengths.
            const dirs: [number, number][] = [[pdx, pdy], [-pdx || 1, pdy], [pdx || 1, -pdy], [-pdx || -1, -pdy], [0, -1], [0, 1]]
            const options = [0, 18, 36].flatMap((extra) => dirs.map(([dx, dy]) => calloutGeom(x, y, rad, dx, dy, lead + extra, tw, size.w)))
            const g = options.find((o) => place(taken, [o.box], size.w, size.h, 3)) ?? options[0]
            return (
              <g key={`co-${continent}`} pointerEvents="none" opacity={Math.max(0.35, inferredOpacity)} style={{ transition: 'opacity .4s' }}>
                <polyline points={g.path} fill="none" stroke="var(--color-mute)" strokeOpacity={0.7} strokeWidth={0.8} />
                <circle cx={g.ex} cy={g.ey} r={1.6} fill="var(--color-mute)" />
                <text x={g.tx} y={g.y1} textAnchor={g.anchor} className="fill-dim font-mono" fontSize={8} letterSpacing="0.1em" {...HALO}>{continent.toUpperCase()}</text>
                <text x={g.tx} y={g.y2} textAnchor={g.anchor} className="fill-bone" fontSize={9.5} {...HALO}>
                  <tspan fontWeight={600}>{r.short} {p.toFixed(0)}</tspan><tspan className="fill-mute"> · {note}</tspan>
                </text>
              </g>
            )
          })
        })()}

        {/* A hovered region whose name lost the layout still gets it, drawn on top. */}
        {hoverRegion && !compact && !labels.get(hoverRegion.id)?.name && (() => {
          const [x, y] = pt(hoverRegion.lon, hoverRegion.lat)
          return <text x={x} y={y + 16} textAnchor="middle" pointerEvents="none" className="fill-bone font-mono" fontSize={9.5} letterSpacing="0.06em" {...HALO}>{hoverRegion.short}</text>
        })()}
      </svg>
      <button type="button" onClick={toggle} aria-pressed={globe} title={globe ? 'Switch to flat map' : 'Switch to globe (drag to rotate)'}
        className="absolute right-2 top-2 z-10 flex overflow-hidden rounded-[3px] border border-line2 bg-panel/90 text-[11px] backdrop-blur font-medium">
        <span className={`px-2 py-1 ${!globe ? 'bg-raise text-bone' : 'text-dim hover:text-mute'}`}>Flat</span>
        <span className={`px-2 py-1 ${globe ? 'bg-raise text-bone' : 'text-dim hover:text-mute'}`}>Globe</span>
      </button>
      <div className="absolute bottom-2 right-2 z-10 flex flex-col overflow-hidden rounded-[3px] border border-line2 bg-panel/90 font-mono text-[12px] leading-none backdrop-blur" title="Zoom (or pinch / ⌘ + scroll)">
        <button type="button" aria-label="Zoom in" onClick={() => zoomBy(1.5)} disabled={view.z >= ZOOM_MAX}
          className="grid h-6 w-6 place-items-center text-mute hover:bg-raise hover:text-bone disabled:text-dim disabled:hover:bg-transparent">+</button>
        <button type="button" aria-label="Zoom out" onClick={() => zoomBy(1 / 1.5)} disabled={view.z <= ZOOM_MIN}
          className="grid h-6 w-6 place-items-center border-t border-line2 text-mute hover:bg-raise hover:text-bone disabled:text-dim disabled:hover:bg-transparent">−</button>
        {view.z > ZOOM_MIN && (
          <button type="button" aria-label="Reset zoom" onClick={() => setView({ z: 1, pan: [0, 0] })}
            className="grid h-6 w-6 place-items-center border-t border-line2 text-[10px] text-mute hover:bg-raise hover:text-bone">⟲</button>
        )}
      </div>
      {hover && vis(REGIONS.find((x) => x.id === hover)!.lon, REGIONS.find((x) => x.id === hover)!.lat) && <HoverCard r={REGIONS.find((x) => x.id === hover)!} t={t} scenario={scenario} xy={pt(REGIONS.find((x) => x.id === hover)!.lon, REGIONS.find((x) => x.id === hover)!.lat)} w={size.w} />}
    </div>
  )
}

/**
 * Geometry for a ring callout: a leader from the ring edge in direction (dx, dy) — diagonal with an elbow,
 * or vertical when dx is 0 — and a two-line text block whose bounding box is returned for collision checks.
 */
function calloutGeom(x: number, y: number, rad: number, dx: number, dy: number, lead: number, tw: number, w: number) {
  const ex = dx ? x + dx * rad * 0.71 : x
  const ey = dx ? y + dy * rad * 0.71 : y + dy * rad
  const kx = ex + dx * lead * 0.7
  const ky = ey + dy * (dx ? lead * 0.7 : lead)
  const tx = dx ? kx + dx * 10 : Math.min(w - 8 - tw / 2, Math.max(8 + tw / 2, kx))
  const anchor: 'start' | 'middle' | 'end' = dx < 0 ? 'end' : dx > 0 ? 'start' : 'middle'
  const [y1, y2] = dx ? [ky - 2, ky + 8] : dy < 0 ? [ky - 13, ky - 3] : [ky + 9, ky + 19]
  const bx = anchor === 'start' ? tx : anchor === 'end' ? tx - tw : tx - tw / 2
  const path = dx ? `${ex},${ey} ${kx},${ky} ${tx - dx * 2},${ky}` : `${ex},${ey} ${kx},${ky}`
  return { ex, ey, tx, y1, y2, anchor, path, box: { x: bx, y: y1 - 9, w: tw, h: y2 - y1 + 12 } }
}

/** Text halo in the page ground colour so labels stay legible over pressure glows and other marks. */
const HALO = { style: { stroke: 'var(--color-halo)', strokeWidth: 3, strokeOpacity: 'var(--halo-opacity)', paintOrder: 'stroke', strokeLinejoin: 'round' } } as const
const TIE_W = 34
/** Default callout direction per continent ([dx, dy] unit signs, leader length px), chosen to land in empty space rather than on neighbouring labels. */
const CALLOUT_DIR: Record<Continent, [number, number, number]> = {
  'North America': [0, -1, 30],
  'South America': [1, 1, 26],
  Europe: [1, -1, 54],
  'Middle East': [-1, 1, 26],
  Africa: [1, 1, 26],
  Asia: [-1, 1, 26],
  Oceania: [-1, 1, 26],
}

interface RegionLabels {
  name?: Box
  rental?: Box
  ties?: { box: Box; shown: string[]; more: number }
  power?: Box
}

/**
 * Collision-free label layout. Labels are placed greedily in priority order
 * (selected region first, then larger regions; names before prices before
 * company ties before power figures), each trying a few slots around its dot.
 * A label with no free slot is dropped (ties first collapse to "TICK +N");
 * everything dropped is still in the hover card. The selected region's labels
 * always show, at their preferred slot if nothing fits.
 */
function layoutLabels({ regions, pt, rOf, has, selected, compare, compact, t, scenario, w, h }: {
  regions: Region[]; pt: (lon: number, lat: number) => [number, number]; rOf: (r: Region) => number; has: (l: XLayer) => boolean
  selected: string | null; compare: string[]; compact: boolean; t: number; scenario: Scenario | null; w: number; h: number
}): Map<string, RegionLabels> {
  const out = new Map<string, RegionLabels>(regions.map((r) => [r.id, {}]))
  if (compact) return out
  // Reserve the dots, compare badges and the map's own controls (Flat/Globe top-right, zoom bottom-right).
  const placed: Box[] = [{ x: w - 100, y: 0, w: 100, h: 32 }, { x: w - 34, y: h - 90, w: 34, h: 90 }]
  const xy = new Map(regions.map((r) => [r.id, pt(r.lon, r.lat)]))
  for (const r of regions) {
    const [x, y] = xy.get(r.id)!
    placed.push({ x: x - 4, y: y - 4, w: 8, h: 8 })
    if (compare.includes(r.id)) placed.push({ x: x - 8, y: y - rOf(r) * 1.02 - 16, w: 16, h: 15 })
  }
  const order = [...regions].sort((a, b) => Number(b.id === selected) - Number(a.id === selected) || b.opMW - a.opMW)
  const at = (r: Region) => xy.get(r.id)!
  const put = (r: Region, cands: Box[]) => {
    const got = place(placed, cands, w, h)
    if (got || r.id !== selected) return got
    placed.push(cands[0])
    return cands[0]
  }

  // 1. Region names (shown with the pressure or power layer, or for the selected region).
  for (const r of order) {
    if (!(has('pressure') || has('power') || r.id === selected)) continue
    const p = pressureAt(r, t, scenario)
    const tw = monoWidth(r.short.length, 9.5, 0.06) + (has('pressure') && p != null ? 4 + monoWidth(String(Math.round(p)).length, 9.5, 0.06) : 0)
    const [x, y] = at(r)
    const lh = 12
    out.get(r.id)!.name = put(r, [
      { x: x - tw / 2, y: y + 6, w: tw, h: lh },
      { x: x + 7, y: y - lh / 2, w: tw, h: lh },
      { x: x - tw / 2, y: y - 6 - lh, w: tw, h: lh },
      { x: x - 7 - tw, y: y - lh / 2, w: tw, h: lh },
      { x: x - tw / 2, y: y + rOf(r) * 0.6, w: tw, h: lh },
    ]) ?? undefined
  }
  // 2. Rental quotes.
  if (has('rental')) for (const r of order) {
    if (r.quality === 'none' || !(r.h100 > 0)) continue
    const [x, y] = at(r)
    const W = 50, H = 15
    out.get(r.id)!.rental = put(r, [
      { x: x + 8, y: y - 22, w: W, h: H }, { x: x - 8 - W, y: y - 22, w: W, h: H },
      { x: x + 10, y: y - H / 2, w: W, h: H }, { x: x - 10 - W, y: y - H / 2, w: W, h: H },
      { x: x + 8, y: y + 8, w: W, h: H }, { x: x - 8 - W, y: y + 8, w: W, h: H },
      { x: x - W / 2, y: y - 26 - H, w: W, h: H },
    ]) ?? undefined
  }
  // 3. Documented company ties: full row, else first ticker + count.
  if (has('exposure')) for (const r of order) {
    const tk = r.documented.map((d) => d.t)
    if (r.quality === 'none' || tk.length === 0) continue
    const [x, y] = at(r)
    const H = 13
    const slots = (W: number): Box[] => [
      { x: x + 10, y: y + 10, w: W, h: H }, { x: x - 10 - W, y: y + 10, w: W, h: H },
      { x: x + 10, y: y - 10 - H, w: W, h: H }, { x: x - 10 - W, y: y - 10 - H, w: W, h: H },
      { x: x - W / 2, y: y + 22, w: W, h: H },
    ]
    const full = tk.slice(0, 3)
    const fullW = full.length * (TIE_W + 2) - 2 + (tk.length > 3 ? 4 + monoWidth(String(tk.length - 3).length + 1, 8.5) : 0)
    const fb = place(placed, slots(fullW), w, h)
    if (fb) { out.get(r.id)!.ties = { box: fb, shown: full, more: tk.length - full.length }; continue }
    if (tk.length > 1) {
      const cb = put(r, slots(TIE_W + 4 + monoWidth(String(tk.length - 1).length + 1, 8.5)))
      if (cb) out.get(r.id)!.ties = { box: cb, shown: tk.slice(0, 1), more: tk.length - 1 }
    } else {
      const cb = put(r, slots(TIE_W))
      if (cb) out.get(r.id)!.ties = { box: cb, shown: tk, more: 0 }
    }
  }
  // 4. Power price text beside its bar (the bar itself is always drawn).
  if (has('power')) for (const r of order) {
    if (!(r.power > 0)) continue
    const [x, y] = at(r)
    const W = monoWidth(String(Math.round(r.power)).length + 1, 8), H = 10
    out.get(r.id)!.power = place(placed, [{ x: x - 17 - W, y: y + 1, w: W, h: H }, { x: x - 12 - W / 2, y: y + 8, w: W, h: H }], w, h) ?? undefined
  }
  return out
}

function ProjectGlyph({ p, xy, scale, selected, opacity, onClick, t }: { p: Project; xy: [number, number]; scale: number; selected: boolean; opacity: number; onClick?: (id: string) => void; t: number }) {
  const [x, y] = xy
  const r = Math.max(3, Math.sqrt(p.reportedMW) * 0.26 * scale)
  const c = 'var(--color-bone)'
  // In future months, a construction project may be shown as energised only within its inferred range.
  const futureOnline = t > NOW_IDX && p.stage === 'construction' && p.onTimeProb > 0.5
  return (
    <g className="cursor-pointer" opacity={Math.max(0.55, opacity)} onClick={(e) => { e.stopPropagation(); onClick?.(p.id) }}>
      <title>{`${p.name} · ${p.stage} · ${p.reportedMW} MW reported`}</title>
      <rect x={x - r - 4} y={y - r - 4} width={2 * r + 8} height={2 * r + 8} fill="transparent" />
      {p.stage === 'operational' && <rect x={x - r} y={y - r} width={2 * r} height={2 * r} fill={c} fillOpacity={0.85} />}
      {p.stage === 'construction' && (
        <g>
          <rect x={x - r} y={y - r} width={2 * r} height={2 * r} fill="none" stroke={c} strokeWidth={1.2} />
          <rect x={x - r} y={y} width={2 * r} height={r} fill={c} fillOpacity={futureOnline ? 0.85 : 0.55} />
        </g>
      )}
      {p.stage === 'proposed' && <rect x={x - r} y={y - r} width={2 * r} height={2 * r} fill="none" stroke={c} strokeWidth={1} />}
      {p.stage === 'announced' && <rect x={x - r} y={y - r} width={2 * r} height={2 * r} fill="none" stroke={c} strokeWidth={1} strokeDasharray="1.5 2" />}
      {selected && <rect x={x - r - 4} y={y - r - 4} width={2 * r + 8} height={2 * r + 8} fill="none" stroke="var(--color-fct)" strokeWidth={1.5} />}
    </g>
  )
}

function HoverCard({ r, t, scenario, xy, w }: { r: Region; t: number; scenario: Scenario | null; xy: [number, number]; w: number }) {
  const p = pressureAt(r, t, scenario)
  const left = xy[0] > w - 240 ? xy[0] - 230 : xy[0] + 18
  return (
    <div className="pointer-events-none absolute z-10 w-[210px] rounded-[4px] border border-line2 bg-ink/95 p-2.5 shadow-2xl backdrop-blur fade-in" style={{ left, top: Math.max(8, xy[1] - 40) }}>
      <div className="text-[12px] font-semibold text-bone">{r.name}</div>
      <div className="mb-1.5 font-mono text-[9.5px] text-dim">{r.marketplace} · grid {r.grid.toUpperCase()}</div>
      {p == null ? (
        <div className="text-[11px] text-mute">No usable observations — FLOPX does not estimate pressure here.</div>
      ) : (
        <div className="space-y-0.5 font-mono text-[10.5px]">
          <div className="flex justify-between"><span className="text-inf">◈ Pressure {t > NOW_IDX ? '(fcst)' : '(inf.)'}</span><span className="text-bone">{p.toFixed(0)} ± {uncAt(r, t, scenario).toFixed(0)}</span></div>
          {r.h100 > 0 && <div className="flex justify-between"><span className="text-mute">■ H100 median</span><span className="text-bone">${r.h100.toFixed(2)}</span></div>}
          {r.power > 0 && <div className="flex justify-between"><span className="text-mute">■ Power DA</span><span className="text-bone">${r.power.toFixed(0)}/MWh</span></div>}
          <div className="flex justify-between"><span className="text-mute">■ Op / constr.</span><span className="text-bone">{(r.opMW / 1000).toFixed(1)} / {(r.conMW / 1000).toFixed(1)} GW</span></div>
        </div>
      )}
      {r.documented.length > 0 && <div className="mt-1.5 font-mono text-[10px] text-dec">◆ Ties {r.documented.map((d) => d.t).join(' · ')}</div>}
      {r.quality !== 'good' && <div className="mt-1.5 text-[11px] text-warn font-medium capitalize">{r.quality} coverage</div>}
    </div>
  )
}
