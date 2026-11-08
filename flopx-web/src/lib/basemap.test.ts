import { describe, expect, it } from 'vitest'
import { validateStyleMin } from '@maplibre/maplibre-gl-style-spec'
import { basemapStyle, globeFitZoom, luminance, mixHex, nearestCopy, type BasemapColors } from './basemap'

const DARK: BasemapColors = { land: '#0f1214', ocean: '#0d1012', dot: '#2d3338', line: '#1c2125', line2: '#2a3035', dim: '#5a636b', mute: '#8a939b', halo: '#0c0e10' }

describe('basemapStyle', () => {
  it('paints borders and country names from map-scheme overrides when given', () => {
    const paint = (s: ReturnType<typeof basemapStyle>, id: string) => s.layers.find((l) => l.id === id)!.paint as Record<string, unknown>
    expect(paint(basemapStyle(DARK, 'flat'), 'borders')['line-color']).toBe(DARK.line2)
    const s = basemapStyle({ ...DARK, border: '#34404b', text: '#7d8a96' }, 'flat')
    expect(paint(s, 'borders')['line-color']).toBe('#34404b')
    expect(paint(s, 'country-names')['text-color']).toBe('#7d8a96')
  })
  it('produces a valid MapLibre style for both projections', () => {
    for (const p of ['flat', 'globe'] as const) expect(validateStyleMin(basemapStyle(DARK, p))).toEqual([])
  })
  it('sets the projection type', () => {
    expect(basemapStyle(DARK, 'globe').projection).toEqual({ type: 'globe' })
    expect(basemapStyle(DARK, 'flat').projection).toEqual({ type: 'mercator' })
  })
  it('paints water with the ocean token and keeps land distinguishable from it', () => {
    const s = basemapStyle(DARK, 'flat')
    const water = s.layers.find((l) => l.id === 'water')!
    const land = s.layers.find((l) => l.id === 'land')!
    expect(water.paint).toMatchObject({ 'fill-color': DARK.ocean })
    expect((land.paint as Record<string, string>)['background-color']).not.toBe(DARK.ocean)
  })
  it('keeps a light theme land token as is', () => {
    const light = { ...DARK, land: '#fdfbf7', ocean: '#efe8dc', dot: '#e4dbcc' }
    const land = basemapStyle(light, 'flat').layers.find((l) => l.id === 'land')!
    expect(land.paint).toMatchObject({ 'background-color': '#fdfbf7' })
  })
  it('credits the tile providers', () => {
    const src = basemapStyle(DARK, 'flat').sources.omt as { attribution?: string }
    expect(src.attribution).toMatch(/OpenStreetMap/)
  })
})

describe('luminance', () => {
  it('is 0 for black, 1 for white, and weights green highest', () => {
    expect(luminance('#000')).toBe(0)
    expect(luminance('#ffffff')).toBeCloseTo(1, 9)
    expect(luminance('#00ff00')).toBeGreaterThan(luminance('#ff0000'))
  })
})

describe('mixHex', () => {
  it('returns the endpoints at k = 0 and 1', () => {
    expect(mixHex('#000000', '#ffffff', 0)).toBe('#000000')
    expect(mixHex('#000000', '#ffffff', 1)).toBe('#ffffff')
  })
  it('mixes channels linearly and accepts short hex', () => {
    expect(mixHex('#000', '#fff', 0.5)).toBe('#808080')
    expect(mixHex('#ff0000', '#0000ff', 0.25)).toBe('#bf0040')
  })
  it('clamps k and rejects non-hex input', () => {
    expect(mixHex('#000000', '#ffffff', 2)).toBe('#ffffff')
    expect(mixHex('#000000', '#ffffff', -1)).toBe('#000000')
    expect(() => mixHex('red', '#fff', 0.5)).toThrow()
  })
})

describe('nearestCopy', () => {
  it('leaves a longitude alone when it is already the nearest copy', () => {
    expect(nearestCopy(10, 0)).toBe(10)
    expect(nearestCopy(-170, -120)).toBe(-170)
  })
  it('wraps across the antimeridian toward the view centre', () => {
    expect(nearestCopy(-170, 170)).toBe(190)
    expect(nearestCopy(170, -170)).toBe(-190)
    expect(nearestCopy(10, 360)).toBe(370)
  })
})

describe('globeFitZoom', () => {
  it('gives a globe whose radius is fill × half the shorter side', () => {
    const z = globeFitZoom(900, 460, 0.9)
    const radius = (512 * 2 ** z) / (2 * Math.PI)
    expect(radius).toBeCloseTo(460 * 0.9 / 2, 6)
  })
  it('grows by one zoom level when the box doubles', () => {
    expect(globeFitZoom(800, 800) - globeFitZoom(400, 400)).toBeCloseTo(1, 9)
  })
})
