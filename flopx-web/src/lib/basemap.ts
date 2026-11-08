import type { StyleSpecification } from 'maplibre-gl'

/** OpenFreeMap: free OpenMapTiles-schema vector tiles, no API key. Attribution is required. */
export const TILES_URL = 'https://tiles.openfreemap.org/planet'
export const GLYPHS_URL = 'https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf'
export const ATTRIBUTION = '<a href="https://openfreemap.org" target="_blank" rel="noreferrer">OpenFreeMap</a> © <a href="https://www.openmaptiles.org/" target="_blank" rel="noreferrer">OpenMapTiles</a> © <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap</a>'
const FONT = ['Noto Sans Regular']

export type MapProjection = 'flat' | 'globe'

/** Theme colours the basemap is painted with (hex), read from the app's CSS tokens. */
export interface BasemapColors {
  land: string
  ocean: string
  dot: string
  line: string
  line2: string
  dim: string
  mute: string
  halo: string
  /** Optional map-only overrides (set by a map colour scheme); default to line2 and dim. */
  border?: string
  text?: string
}

const TOKENS: Record<Exclude<keyof BasemapColors, 'border' | 'text'>, string> = {
  land: '--color-land', ocean: '--color-ocean', dot: '--color-map-dot', line: '--color-line',
  line2: '--color-line2', dim: '--color-dim', mute: '--color-mute', halo: '--color-halo',
}

export function readBasemapColors(el: Element = document.documentElement): BasemapColors {
  const cs = getComputedStyle(el)
  const c = Object.fromEntries(Object.entries(TOKENS).map(([k, v]) => [k, cs.getPropertyValue(v).trim() || '#808080'])) as unknown as BasemapColors
  const border = cs.getPropertyValue('--color-map-border').trim()
  const text = cs.getPropertyValue('--color-map-text').trim()
  return { ...c, ...(border && { border }), ...(text && { text }) }
}

/** Linear mix of two #rgb/#rrggbb colours; k = 0 gives a, k = 1 gives b. */
export function mixHex(a: string, b: string, k: number): string {
  const parse = (h: string) => {
    const s = h.replace('#', '')
    const f = s.length === 3 ? s.split('').map((c) => c + c).join('') : s
    if (!/^[0-9a-f]{6}$/i.test(f)) throw new Error(`Not a hex colour: ${h}`)
    return [0, 2, 4].map((i) => parseInt(f.slice(i, i + 2), 16))
  }
  const t = Math.max(0, Math.min(1, k))
  const pa = parse(a), pb = parse(b)
  return '#' + pa.map((v, i) => Math.round(v + (pb[i] - v) * t).toString(16).padStart(2, '0')).join('')
}

/** Relative luminance (0–1, sRGB weights, no gamma) of a hex colour. */
export function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(mixHex(hex, hex, 0).slice(i, i + 2), 16) / 255)
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

/**
 * The app's basemap: land, water, country borders, and roads and place names that only
 * appear once zoomed in, so the world view stays quiet under FLOPX's own region labels.
 */
export function basemapStyle(c: BasemapColors, projection: MapProjection): StyleSpecification {
  // Dark themes' land and ocean tokens are nearly equal, so dark land is lifted toward the map-dot colour.
  const land = luminance(c.land) > 0.5 ? c.land : mixHex(c.land, c.dot, 0.35)
  const border = c.border ?? c.line2
  const label = c.text ?? c.dim
  const text = (minzoom: number, filter: unknown[], size: [number, number], color: string) => ({
    minzoom, filter,
    layout: { 'text-field': ['coalesce', ['get', 'name:en'], ['get', 'name']], 'text-font': FONT, 'text-size': ['interpolate', ['linear'], ['zoom'], minzoom, size[0], minzoom + 4, size[1]], 'text-max-width': 8 },
    paint: { 'text-color': color, 'text-halo-color': c.halo, 'text-halo-width': 1.2 },
  })
  return {
    version: 8,
    projection: { type: projection === 'globe' ? 'globe' : 'mercator' },
    sky: { 'atmosphere-blend': 0 },
    glyphs: GLYPHS_URL,
    sources: { omt: { type: 'vector', url: TILES_URL, attribution: ATTRIBUTION } },
    layers: [
      { id: 'land', type: 'background', paint: { 'background-color': land } },
      { id: 'landcover-ice', type: 'fill', source: 'omt', 'source-layer': 'landcover', filter: ['==', ['get', 'class'], 'ice'], paint: { 'fill-color': mixHex(land, c.halo, 0.25), 'fill-opacity': 0.5 } },
      { id: 'landuse-urban', type: 'fill', source: 'omt', 'source-layer': 'landuse', minzoom: 6, filter: ['in', ['get', 'class'], ['literal', ['residential', 'commercial', 'industrial']]], paint: { 'fill-color': c.dot, 'fill-opacity': 0.35 } },
      { id: 'water', type: 'fill', source: 'omt', 'source-layer': 'water', paint: { 'fill-color': c.ocean } },
      { id: 'roads', type: 'line', source: 'omt', 'source-layer': 'transportation', minzoom: 5, filter: ['in', ['get', 'class'], ['literal', ['motorway', 'trunk', 'primary']]], paint: { 'line-color': c.line2, 'line-opacity': 0.6, 'line-width': ['interpolate', ['linear'], ['zoom'], 5, 0.4, 10, 1.4] } },
      { id: 'borders', type: 'line', source: 'omt', 'source-layer': 'boundary', filter: ['all', ['==', ['get', 'admin_level'], 2], ['!=', ['get', 'maritime'], 1]], paint: { 'line-color': border, 'line-width': ['interpolate', ['linear'], ['zoom'], 0, 0.5, 6, 1.1] } },
      { id: 'borders-state', type: 'line', source: 'omt', 'source-layer': 'boundary', minzoom: 3.5, filter: ['all', ['==', ['get', 'admin_level'], 4], ['!=', ['get', 'maritime'], 1]], paint: { 'line-color': c.line, 'line-dasharray': [2, 2], 'line-width': 0.6 } },
      { id: 'country-names', type: 'symbol', source: 'omt', 'source-layer': 'place', ...text(3, ['==', ['get', 'class'], 'country'], [10, 13], label) },
      { id: 'city-names', type: 'symbol', source: 'omt', 'source-layer': 'place', ...text(5, ['in', ['get', 'class'], ['literal', ['city', 'town']]], [9.5, 12], c.mute) },
    ] as StyleSpecification['layers'],
  }
}

/** Shift a longitude by whole turns to the world copy nearest the view centre, so markers follow a wrapped flat map. */
export function nearestCopy(lng: number, centerLng: number): number {
  return lng + 360 * Math.round((centerLng - lng) / 360)
}

/** Zoom at which the whole globe (radius = worldSize / 2π, worldSize = 512·2^z) fills `fill` of the shorter side. */
export function globeFitZoom(w: number, h: number, fill = 0.9): number {
  return Math.log2((Math.min(w, h) * fill / 2) * 2 * Math.PI / 512)
}
