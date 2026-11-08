import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import 'maplibre-gl/dist/maplibre-gl.css'
import { Map as GLMap, setWorkerUrl } from 'maplibre-gl'
// MapLibre finds its worker relative to its own module, which breaks once Vite bundles it; point it at a Vite-built worker.
import mapWorkerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url'
import { geoDistance } from 'd3-geo'
import { NOW_IDX, REGIONS, type Region } from '@/data/core'
import { PROJECTS, type Project, type Stage } from '@/data/infra'
import { capPips, POWER_HOT, powerTone, stagesByRegion } from '@/lib/powerMap'
import { pressureAt, pressureColor, pressureWord, scenarioDelta, uncAt } from '@/lib/model'
import { continentLeaders, type Continent } from '@/lib/continents'
import type { Scenario, XLayer } from '@/lib/store'
import { monoWidth, place, type Box } from '@/lib/labels'
import { basemapStyle, globeFitZoom, nearestCopy, readBasemapColors, type MapProjection } from '@/lib/basemap'

setWorkerUrl(mapWorkerUrl)

export type { MapProjection }
const PROJ_KEY = 'flopx.mapProjection'
function readProjection(): MapProjection {
  try { return localStorage.getItem(PROJ_KEY) === 'globe' ? 'globe' : 'flat' } catch { return 'flat' }
}
/** The flat map's home view: every FLOPX region, without the empty polar caps. */
const FLAT_BOUNDS: [[number, number], [number, number]] = [[-165, -46], [175, 72]]
const GLOBE_HOME: [number, number] = [-35, 25]
const ZOOM_MAX = 10
interface Camera { lng: number; lat: number; zoom: number; home: number }

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
  /** 'markers' draws each facility at its site (clickable); 'summary' folds a region's facilities into its
   *  label as stage pips so nothing piles up. With the power layer, the label carries a small price meter. */
  facilities?: 'markers' | 'summary'
}

/**
 * FLOPX regions over a MapLibre GL vector basemap (OpenFreeMap tiles). MapLibre owns the camera:
 * drag, pinch and ⌘/Ctrl + scroll, and the flat (Mercator) or globe projection. The FLOPX marks are
 * an SVG layer re-projected from the camera on every move, so the label layout below is unchanged.
 */
export function WorldMap({ layers, reveal = 100, t = NOW_IDX, selected, selectedProject, compare = [], scenario = null, onRegion, onProject, compact, callouts = !compact, minHeight = 260, facilities = 'markers' }: MapProps) {
  const ref = useRef<HTMLDivElement>(null)
  const mapEl = useRef<HTMLDivElement>(null)
  const mapRef = useRef<GLMap | null>(null)
  const [size, setSize] = useState({ w: 900, h: 460 })
  const sizeRef = useRef(size)
  sizeRef.current = size
  const [hover, setHover] = useState<string | null>(null)
  const [mode, setMode] = useState<MapProjection>(readProjection)
  const modeRef = useRef(mode)
  modeRef.current = mode
  const globe = mode === 'globe'
  const [cam, setCam] = useState<Camera | null>(null)

  /** Home camera for a projection: the whole globe, or the flat map fitted to FLAT_BOUNDS. */
  const home = (map: GLMap, m: MapProjection) => {
    const { w, h } = sizeRef.current
    if (m === 'globe') return { center: GLOBE_HOME, zoom: globeFitZoom(w, h) }
    const c = map.cameraForBounds(FLAT_BOUNDS, { padding: 8 })
    return { center: (c?.center ?? [5, 13]) as [number, number], zoom: c?.zoom ?? 1 }
  }
  const goHome = (map: GLMap, m: MapProjection, animate: boolean) => {
    map.setMinZoom(0)
    const v = home(map, m)
    // Allow a little zoom-out past home, never down to a postage-stamp world.
    map.setMinZoom(Math.max(0, v.zoom - 0.5))
    if (animate) { map.easeTo({ ...v, duration: 500 }); return }
    map.jumpTo(v)
    // Mercator may clamp the zoom up so the world fills a tall box; home is where the camera landed.
    const z = map.getZoom()
    setCam((c) => (c ? { ...c, home: z } : c))
  }

  useLayoutEffect(() => {
    if (!ref.current) return
    const ro = new ResizeObserver(([e]) => setSize({ w: Math.max(300, e.contentRect.width), h: Math.max(minHeight, e.contentRect.height) }))
    ro.observe(ref.current)
    return () => ro.disconnect()
  }, [minHeight])

  useEffect(() => {
    if (!mapEl.current) return
    const map = new GLMap({
      container: mapEl.current,
      style: basemapStyle(readBasemapColors(), modeRef.current),
      center: GLOBE_HOME, zoom: 1, maxZoom: ZOOM_MAX,
      dragRotate: false, pitchWithRotate: false, touchPitch: false,
      // A plain scroll keeps scrolling the page; ⌘/Ctrl + scroll or two fingers move the map.
      cooperativeGestures: true,
      attributionControl: false,
    })
    map.touchZoomRotate.disableRotation()
    map.keyboard.disableRotation()
    mapRef.current = map
    const sync = () => {
      const c = map.getCenter()
      setCam((p) => ({ lng: c.lng, lat: c.lat, zoom: map.getZoom(), home: p?.home ?? map.getZoom() }))
    }
    map.on('move', sync)
    map.once('load', () => { goHome(map, modeRef.current, false); sync() })
    // Repaint the basemap from the CSS tokens when the theme changes.
    const mo = new MutationObserver(() => map.setStyle(basemapStyle(readBasemapColors(), modeRef.current)))
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-blend', 'class', 'style'] })
    return () => { mo.disconnect(); map.remove(); mapRef.current = null }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const toggle = () => {
    const next: MapProjection = globe ? 'flat' : 'globe'
    setMode(next)
    try { localStorage.setItem(PROJ_KEY, next) } catch { /* private mode: not remembered */ }
    const map = mapRef.current
    if (!map) return
    map.setProjection({ type: next === 'globe' ? 'globe' : 'mercator' })
    goHome(map, next, false)
  }
  // Turn the globe to the selected region.
  const sel = REGIONS.find((r) => r.id === selected)
  useEffect(() => {
    const map = mapRef.current
    if (!globe || !sel || !map) return
    map.easeTo({ center: [sel.lon, Math.max(-60, Math.min(60, sel.lat))], duration: 650 })
  }, [globe, sel])

  const map = mapRef.current
  const zoom = cam?.zoom ?? 1
  const homeZoom = cam?.home ?? zoom
  const centre: [number, number] = [cam?.lng ?? GLOBE_HOME[0], cam?.lat ?? GLOBE_HOME[1]]
  /** Screen position of a lon/lat; on the flat map, on the world copy nearest the view centre. */
  const pt = (lon: number, lat: number): [number, number] => {
    if (!map || !cam) return [-9999, -9999]
    const p = map.project([globe ? lon : nearestCopy(lon, centre[0]), lat])
    return [p.x, p.y]
  }
  /** On the globe only the near hemisphere is drawn (a margin so markers don't sit on the limb). */
  const vis = (lon: number, lat: number) => !!cam && (!globe || geoDistance([lon, lat], centre) < Math.PI / 2 - 0.12)

  const has = (l: XLayer) => layers.includes(l)
  const inferredOpacity = reveal / 100
  const rawOpacity = 0.25 + 0.75 * (1 - reveal / 100)
  const future = t > NOW_IDX
  // Markers grow a little with zoom, much less than the map, so zooming in separates crowded regions.
  const scale = Math.min(1.25, size.w / 1100) * Math.min(2, 2 ** (Math.max(0, zoom - homeZoom) * 0.3))
  const rOf = (r: Region) => (14 + Math.sqrt(r.opMW + r.conMW * 0.5) * 0.42) * scale * (compact ? 0.8 : 1)
  const clickRegion = (id: string) => onRegion?.(id)
  const clickProject = (id: string) => onProject?.(id)

  const regions = REGIONS.filter((r) => vis(r.lon, r.lat))
  /** Pressure ring radius: grows with capacity and inferred pressure. */
  const ringOf = (r: Region, p: number) => rOf(r) * (0.5 + (p / 100) * 0.75)
  const summary = facilities === 'summary'
  const labels = layoutLabels({ regions, pt, rOf, has, selected: selected ?? null, compare, compact: !!compact, t, scenario, w: size.w, h: size.h, summary })
  const hoverRegion = hover ? regions.find((r) => r.id === hover) : undefined
  return (
    <div ref={ref} className="relative h-full w-full overflow-hidden" style={{ minHeight }}>
      {/* MapLibre's stylesheet makes its container position: relative, so the sizing lives on a wrapper. */}
      <div className="absolute inset-0"><div ref={mapEl} className="flopx-map h-full w-full" /></div>
      {/* OpenStreetMap's licence requires visible credit; a quiet line instead of MapLibre's control. */}
      <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer" className="absolute bottom-1 left-1.5 z-10 text-[9.5px] text-dim/70 hover:text-mute">© OpenStreetMap</a>
      <svg width={size.w} height={size.h} className="pointer-events-none absolute inset-0 block">
        <defs>
          <pattern id="nodata" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <line x1="0" y1="0" x2="0" y2="5" stroke="var(--color-dim)" strokeWidth="1" />
          </pattern>
          <pattern id="effhatch" width="4" height="4" patternUnits="userSpaceOnUse" patternTransform="rotate(-45)">
            <line x1="0" y1="0" x2="0" y2="4" stroke="var(--color-inf)" strokeWidth="1.2" strokeOpacity="0.7" />
          </pattern>
          {regions.map((r) => {
            const p = pressureAt(r, t, scenario)
            return (
              <radialGradient key={r.id} id={`pg-${r.id}`}>
                <stop offset="0%" stopColor={pressureColor(p, 'g')} style={{ stopOpacity: 'var(--glow-core)' }} />
                <stop offset="40%" stopColor={pressureColor(p, 'g')} style={{ stopOpacity: 'var(--glow-mid)' }} />
                <stop offset="72%" stopColor={pressureColor(p, 'g')} style={{ stopOpacity: 'calc(var(--glow-mid) * 0.4)' }} />
                <stop offset="100%" stopColor={pressureColor(p, 'g')} stopOpacity={0} />
              </radialGradient>
            )
          })}
        </defs>


        {/* Inferred pressure fields */}
        {has('pressure') && regions.map((r) => {
          if (r.quality === 'none') return null
          const [x, y] = pt(r.lon, r.lat)
          const p = pressureAt(r, t, scenario)!
          const rad = ringOf(r, p)
          const u = uncAt(r, t, scenario)
          return (
            <g key={`pf-${r.id}`} opacity={inferredOpacity} pointerEvents="none" style={{ transition: 'opacity .4s' }}>
              {/* Heat field: spreads well past the uncertainty ring, further for hotter regions, so neighbours merge. */}
              <circle cx={x} cy={y} r={rad * (1.8 + p / 80)} fill={`url(#pg-${r.id})`} style={{ transition: 'r .4s' }} />
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
        {has('pipeline') && !summary && (() => {
          const shown = PROJECTS.filter((p) => vis(p.lon, p.lat))
          const detail = zoom - homeZoom >= 1
          const labelY = stackProjectLabels(shown.filter((p) => detail || selectedProject === p.id).map((p) => ({ p, xy: pt(p.lon, p.lat), r: projectRadius(p, scale) })))
          return shown.map((p) => <ProjectGlyph key={p.id} p={p} xy={pt(p.lon, p.lat)} scale={scale} selected={selectedProject === p.id} opacity={rawOpacity} onClick={clickProject} t={t} labelY={labelY.get(p.id)} />)
        })()}

        {/* Region anchors + raw observations */}
        {regions.map((r) => {
          const [x, y] = pt(r.lon, r.lat)
          const isSel = selected === r.id
          const cmp = compare.indexOf(r.id)
          const none = r.quality === 'none'
          const hovered = hover === r.id
          return (
            <g key={r.id} className="pointer-events-auto cursor-pointer" onClick={() => clickRegion(r.id)} onMouseEnter={() => setHover(r.id)} onMouseLeave={() => setHover(null)}>
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
            <g key={`lb-${r.id}`} className="pointer-events-auto cursor-pointer" onClick={() => clickRegion(r.id)} onMouseEnter={() => setHover(r.id)} onMouseLeave={() => setHover(null)}>
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
                <text x={L.name.x} y={L.name.y + 10.5} className="font-mono" fontSize={10.5} fill={isSel ? 'var(--color-bone)' : 'var(--color-mute)'} letterSpacing="0.06em" fontWeight={isSel ? 600 : 400} {...HALO}>
                  {r.short}
                  {has('pressure') && p != null && <tspan fill={pressureColor(p)} dx={4}>{p.toFixed(0)}</tspan>}
                </text>
              )}
              {L.name && L.price != null && (
                <g transform={`translate(${L.name.x + L.price},${L.name.y + (L.name.h - METER_H) / 2})`}>
                  <title>{`${r.short}: day-ahead power $${Math.round(r.power)}/MWh${r.power > POWER_HOT ? ` (above $${POWER_HOT})` : ''}`}</title>
                  <PowerMeter usd={r.power} />
                </g>
              )}
              {L.name && L.pips && (
                <g transform={`translate(${L.name.x + L.pips.at},${L.name.y + 3})`} pointerEvents="none">
                  {L.pips.shown.map((st, k) => <StagePip key={k} stage={st} x={k * (PIP + 2)} />)}
                  {L.pips.more > 0 && <text x={L.pips.shown.length * (PIP + 2) + 1} y={7} className="fill-mute font-mono" fontSize={8.5}>+{L.pips.more}</text>}
                </g>
              )}
            </g>
          )
        })}
        {/* One callout per continent, on its biggest pressure ring, placed clear of region labels and each other. */}
        {has('pressure') && callouts && (() => {
          const taken: Box[] = [...labels.values()].flatMap((L) => [L.name, L.rental, L.ties?.box].filter((b): b is Box => !!b))
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
                <rect x={g.box.x - 4} y={g.box.y} width={g.box.w + 8} height={g.box.h} rx={2} style={{ fill: 'var(--color-label-bg)', fillOpacity: 'var(--label-bg-opacity)' }} />
                <text x={g.tx} y={g.y1} textAnchor={g.anchor} className="fill-dim" fontSize={9} {...HALO}>{continent}</text>
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
        <button type="button" aria-label="Zoom in" onClick={() => map?.zoomIn()} disabled={zoom >= ZOOM_MAX - 0.01}
          className="grid h-6 w-6 place-items-center text-mute hover:bg-raise hover:text-bone disabled:text-dim disabled:hover:bg-transparent">+</button>
        <button type="button" aria-label="Zoom out" onClick={() => map?.zoomOut()} disabled={zoom <= homeZoom - 0.49}
          className="grid h-6 w-6 place-items-center border-t border-line2 text-mute hover:bg-raise hover:text-bone disabled:text-dim disabled:hover:bg-transparent">−</button>
        {zoom > homeZoom + 0.05 && (
          <button type="button" aria-label="Reset view" onClick={() => map && goHome(map, mode, true)}
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
  /** Power meter starts this many px into the name label; in summary mode, facility pips start `at` px into it. */
  price?: number
  pips?: { at: number; shown: Stage[]; more: number }
  rental?: Box
  ties?: { box: Box; shown: string[]; more: number }
}

/**
 * Collision-free label layout. Labels are placed greedily in priority order
 * (selected region first, then larger regions; names (with their power meter) before prices before
 * company ties), each trying a few slots around its dot.
 * A label with no free slot is dropped (ties first collapse to "TICK +N");
 * everything dropped is still in the hover card. The selected region's labels
 * always show, at their preferred slot if nothing fits.
 */
function layoutLabels({ regions, pt, rOf, has, selected, compare, compact, t, scenario, w, h, summary = false }: {
  regions: Region[]; pt: (lon: number, lat: number) => [number, number]; rOf: (r: Region) => number; has: (l: XLayer) => boolean
  selected: string | null; compare: string[]; compact: boolean; t: number; scenario: Scenario | null; w: number; h: number; summary?: boolean
}): Map<string, RegionLabels> {
  const out = new Map<string, RegionLabels>(regions.map((r) => [r.id, {}]))
  if (compact) return out
  // Reserve the dots, compare badges and the map's own controls (Flat/Globe top-right, zoom bottom-right, map credit bottom-left).
  const placed: Box[] = [{ x: w - 100, y: 0, w: 100, h: 32 }, { x: w - 34, y: h - 90, w: 34, h: 90 }, { x: 0, y: h - 16, w: 96, h: 16 }]
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
    const stages = summary && has('pipeline') ? STAGES.get(r.id) ?? [] : []
    const showPrice = has('power') && r.power > 0
    if (!(has('pressure') || has('power') || stages.length > 0 || r.id === selected)) continue
    const p = pressureAt(r, t, scenario)
    let tw = monoWidth(r.short.length, 10.5, 0.06) + (has('pressure') && p != null ? 4 + monoWidth(String(Math.round(p)).length, 10.5, 0.06) : 0)
    const price = showPrice ? tw + 6 : undefined
    if (price != null) tw = price + METER_W
    const pips = stages.length ? { at: tw + 6, ...capPips(stages) } : undefined
    if (pips) tw = pips.at + pips.shown.length * (PIP + 2) - 2 + (pips.more ? 3 + monoWidth(String(pips.more).length + 1, 8.5) : 0)
    const [x, y] = at(r)
    const lh = 13
    out.get(r.id)!.name = put(r, [
      { x: x - tw / 2, y: y + 6, w: tw, h: lh },
      { x: x + 7, y: y - lh / 2, w: tw, h: lh },
      { x: x - tw / 2, y: y - 6 - lh, w: tw, h: lh },
      { x: x - 7 - tw, y: y - lh / 2, w: tw, h: lh },
      { x: x - tw / 2, y: y + rOf(r) * 0.6, w: tw, h: lh },
    ]) ?? undefined
    if (out.get(r.id)!.name) Object.assign(out.get(r.id)!, { price, pips })
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
  return out
}

const STAGES = stagesByRegion(PROJECTS)
const PIP = 7

/** A facility's stage as a small square: filled operating, half-filled building, outline proposed, dashed announced. */
/** Power meter: day-ahead $/MWh as the filled share of a short track (full = $150), red above POWER_HOT. */
export const METER_W = 22
export const METER_H = 5
export function PowerMeter({ usd }: { usd: number }) {
  const c = `var(--color-${powerTone(usd)})`
  return (
    <>
      <rect width={METER_W} height={METER_H} rx={1} fill="var(--color-line2)" />
      <rect width={METER_W * Math.min(1, usd / 150)} height={METER_H} rx={1} fill={c} />
    </>
  )
}

export function StagePip({ stage, x = 0, size = PIP, color = 'var(--color-bone)' }: { stage: Stage; x?: number; size?: number; color?: string }) {
  const s = size - 1
  return (
    <g transform={`translate(${x + 0.5},0.5)`}>
      {stage === 'operational' && <rect width={s} height={s} fill={color} fillOpacity={0.9} />}
      {stage === 'construction' && <><rect width={s} height={s} fill="none" stroke={color} /><rect y={s / 2} width={s} height={s / 2} fill={color} fillOpacity={0.7} /></>}
      {stage === 'proposed' && <rect width={s} height={s} fill="none" stroke={color} />}
      {stage === 'announced' && <rect width={s} height={s} fill="none" stroke={color} strokeDasharray="1.5 1.5" />}
    </g>
  )
}

/**
 * A facility, drawn like the build-status chart: one hue (cat3), square sized by reported MW, stage by line style
 * (solid operational, outline construction, dashed proposed, dotted announced). Under construction, the fill rises
 * with the energized share. A red diamond marks a slipped milestone. Once the map is zoomed in
 * (or the site is selected) it carries a name · MW label; at world zoom labels would collide with region labels.
 */
const projectRadius = (p: Project, scale: number) => Math.max(3.5, Math.sqrt(p.reportedMW) * 0.26 * scale)
const projectMW = (p: Project) => (p.reportedMW >= 1000 ? `${(p.reportedMW / 1000).toFixed(1)} GW` : `${p.reportedMW} MW`)
const projectShort = (p: Project) => p.name.replace(/ (Campus|Compute|Digital)\b.*$/, '')

/** Baseline y for each facility label: above its square, nudged up a line at a time until it clears labels already placed. */
function stackProjectLabels(items: { p: Project; xy: [number, number]; r: number }[]) {
  const placed: { x0: number; x1: number; y0: number; y1: number }[] = []
  const out = new Map<string, number>()
  for (const { p, xy: [x, y], r } of [...items].sort((a, b) => a.xy[1] - b.xy[1])) {
    const x0 = x + r + 4, x1 = x0 + (projectShort(p).length + projectMW(p).length + 3) * 5.3
    let by = y - r - 3
    while (placed.some((b) => x0 < b.x1 && x1 > b.x0 && by - 10 < b.y1 && by > b.y0)) by -= 12
    placed.push({ x0, x1, y0: by - 10, y1: by })
    out.set(p.id, by)
  }
  return out
}

function ProjectGlyph({ p, xy, scale, selected, opacity, onClick, t, labelY }: { p: Project; xy: [number, number]; scale: number; selected: boolean; opacity: number; onClick?: (id: string) => void; t: number; labelY?: number }) {
  const [x, y] = xy
  const r = projectRadius(p, scale)
  const c = 'var(--color-cat3)'
  // In future months, a construction project may be shown as energised only within its inferred range.
  const futureOnline = t > NOW_IDX && p.stage === 'construction' && p.onTimeProb > 0.5
  const share = futureOnline ? 1 : p.reportedMW ? p.energizedMW / p.reportedMW : 0
  const slipped = p.milestones.some((m) => m.status === 'slipped')
  const side = 2 * r
  const stroke = { operational: undefined, construction: undefined, proposed: '3 2', announced: '1 2' }[p.stage]
  return (
    <g className="pointer-events-auto cursor-pointer" opacity={Math.max(0.6, opacity)} onClick={(e) => { e.stopPropagation(); onClick?.(p.id) }}>
      <title>{`${p.name} · ${p.stage} · ${p.energizedMW} of ${p.reportedMW} MW energized · online ${p.expectedOnline}${slipped ? ' · milestone slipped' : ''}`}</title>
      <rect x={x - r - 4} y={y - r - 4} width={side + 8} height={side + 8} fill="transparent" />
      {p.stage === 'operational'
        ? <rect x={x - r} y={y - r} width={side} height={side} rx={1.5} fill={c} fillOpacity={0.9} stroke="var(--color-halo)" strokeOpacity={0.6} />
        : (
          <g>
            <rect x={x - r} y={y - r} width={side} height={side} rx={1.5} fill={c} fillOpacity={p.stage === 'construction' ? 0.14 : 0.06}
              stroke={p.stage === 'announced' ? 'var(--color-mute)' : c} strokeOpacity={p.stage === 'proposed' ? 0.8 : 1} strokeWidth={1.2} strokeDasharray={stroke} />
            {p.stage === 'construction' && share > 0 && <rect x={x - r} y={y + r - side * share} width={side} height={side * share} fill={c} fillOpacity={0.75} />}
          </g>
        )}
      {slipped && <rect x={x + r - 2.5} y={y - r - 2.5} width={5} height={5} transform={`rotate(45 ${x + r} ${y - r})`} fill="var(--color-down)" />}
      {selected && <rect x={x - r - 4} y={y - r - 4} width={side + 8} height={side + 8} rx={2} fill="none" stroke="var(--color-fct)" strokeWidth={1.5} />}
      {labelY != null && (
        <text x={x + r + 4} y={labelY} fontSize={9.5} className="pointer-events-none font-sans" fill="var(--color-mute)" {...HALO}>
          {projectShort(p)}<tspan fill="var(--color-dim)"> · {projectMW(p)}</tspan>
        </text>
      )}
    </g>
  )
}

function HoverCard({ r, t, scenario, xy, w }: { r: Region; t: number; scenario: Scenario | null; xy: [number, number]; w: number }) {
  const p = pressureAt(r, t, scenario)
  const left = xy[0] > w - 240 ? xy[0] - 230 : xy[0] + 18
  return (
    <div className="pointer-events-none absolute z-10 w-[210px] rounded-[4px] border border-line2 bg-ink/95 p-2.5 shadow-2xl backdrop-blur fade-in" style={{ left, top: Math.max(8, xy[1] - 40) }}>
      <div className="text-[12px] font-semibold text-bone">{r.name}</div>
      <div className="mb-1.5 text-[11px] text-dim">{r.marketplace} · grid {r.grid.toUpperCase()}</div>
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
