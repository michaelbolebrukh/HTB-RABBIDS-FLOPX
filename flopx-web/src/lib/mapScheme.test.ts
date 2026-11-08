import { describe, expect, it } from 'vitest'
import { ALL_VARS, applyMapScheme, BASES, DEFAULT_MAP_SCHEME, RAMPS, schemeVars } from './mapScheme'

const HEX = /^#[0-9a-f]{6}$/i

describe('map schemes', () => {
  it('overrides nothing by default, in either theme', () => {
    expect(schemeVars(DEFAULT_MAP_SCHEME, 'dark')).toEqual({})
    expect(schemeVars(DEFAULT_MAP_SCHEME, 'light')).toEqual({})
  })

  it('combines a basemap and a ramp, and picks the theme variant', () => {
    const d = schemeVars({ base: 'slate', ramp: 'viridis' }, 'dark')
    const l = schemeVars({ base: 'slate', ramp: 'viridis' }, 'light')
    expect(d['--color-ocean']).toBe('#0a0e12')
    expect(l['--color-ocean']).toBe('#dde5ec')
    expect(d['--color-g4']).toBe('#fde725')
    expect(Object.keys(d)).toEqual(Object.keys(l))
  })

  it('falls back to the defaults for unknown ids', () => {
    expect(schemeVars({ base: 'nope', ramp: 'nope' }, 'dark')).toEqual({})
  })

  it('defines every non-default scheme fully in both themes with hex colours', () => {
    for (const set of [BASES, RAMPS]) {
      const keys = Object.keys(set[1].dark).sort()
      for (const s of set.slice(1)) {
        for (const t of ['dark', 'light'] as const) {
          expect(Object.keys(s[t]).sort()).toEqual(keys)
          Object.values(s[t]).forEach((v) => expect(v).toMatch(HEX))
        }
      }
    }
  })

  it('keeps land and ocean distinguishable and ordered by theme', () => {
    const lum = (h: string) => [1, 3, 5].reduce((a, i, j) => a + [0.2126, 0.7152, 0.0722][j] * parseInt(h.slice(i, i + 2), 16), 0)
    for (const b of BASES.slice(1)) {
      // Dark: land above ocean. Light: ocean below land. Never equal.
      expect(lum(b.dark['--color-land'])).toBeGreaterThan(lum(b.dark['--color-ocean']))
      expect(lum(b.light['--color-land'])).toBeGreaterThan(lum(b.light['--color-ocean']))
    }
  })

  it('applies inline and clears variables the next scheme does not set', () => {
    const props = new Map<string, string>()
    const el = { style: { setProperty: (k: string, v: string) => props.set(k, v), removeProperty: (k: string) => props.delete(k) } } as unknown as HTMLElement
    applyMapScheme(el, { base: 'navy', ramp: 'magma' }, 'dark')
    expect(props.get('--color-land')).toBe('#13171b')
    expect(props.get('--color-g0')).toBe('#3b2f4a')
    applyMapScheme(el, { base: 'default', ramp: 'magma' }, 'light')
    expect(props.has('--color-land')).toBe(false)
    expect(props.get('--color-g0')).toBe('#e3d9ea')
    applyMapScheme(el, DEFAULT_MAP_SCHEME, 'dark')
    expect([...props.keys()].filter((k) => ALL_VARS.includes(k))).toEqual([])
  })
})
