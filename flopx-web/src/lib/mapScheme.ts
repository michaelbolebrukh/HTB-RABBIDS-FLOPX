import { useEffect, useSyncExternalStore } from 'react'

/**
 * Alternative map colourings for side-by-side testing. A scheme only overrides map tokens
 * (land, water, borders, map labels, pressure glows); the default scheme overrides nothing,
 * so the theme's own tokens in index.css apply. Each scheme has a dark and a light variant.
 */
export type Theme = 'dark' | 'light'
type Vars = Record<string, string>
interface Scheme { id: string; label: string; dark: Vars; light: Vars }

const base = (land: string, ocean: string, dot: string, border: string, text: string): Vars => ({
  '--color-land': land, '--color-ocean': ocean, '--color-map-dot': dot, '--color-map-border': border, '--color-map-text': text,
})
const ramp = (g: [string, string, string, string, string]): Vars => Object.fromEntries(g.map((c, i) => [`--color-g${i}`, c]))

export const BASES: Scheme[] = [
  { id: 'default', label: 'Default', dark: {}, light: {} },
  { id: 'slate', label: 'Slate', dark: base('#151a1f', '#0a0e12', '#2a333c', '#34404b', '#7d8a96'), light: base('#f8f9fa', '#dde5ec', '#d5dde4', '#b9c4ce', '#5d6b78') },
  { id: 'contrast', label: 'Contrast', dark: base('#1e2328', '#07090a', '#3a4249', '#4a545c', '#9aa3ab'), light: base('#ffffff', '#d6d0c4', '#cfc6b6', '#a89f90', '#4b463e') },
  { id: 'mono', label: 'Mono', dark: base('#151515', '#0b0b0b', '#2e2e2e', '#3a3a3a', '#8a8a8a'), light: base('#fafafa', '#e9e9e9', '#dcdcdc', '#c2c2c2', '#6a6a6a') },
  { id: 'navy', label: 'Navy', dark: base('#13171b', '#07111b', '#24303b', '#2f3d4a', '#7f8fa0'), light: base('#fbfaf6', '#d4e2ee', '#d8dfe4', '#b3c2cf', '#55687a') },
  { id: 'sand', label: 'Sand', dark: base('#1a1714', '#0d0c0a', '#3a332b', '#463d33', '#988b7b'), light: base('#fbf6ec', '#e6dac4', '#e2d5bd', '#c4b393', '#6e6250') },
]

/** Pressure glow ramps, low (g0) to high (g4). Glows encode data, so these are for testing legibility only. */
export const RAMPS: Scheme[] = [
  { id: 'heat', label: 'Heat', dark: {}, light: {} },
  { id: 'magma', label: 'Magma', dark: ramp(['#3b2f4a', '#7b3f8c', '#c2457a', '#f2734d', '#fcc25b']), light: ramp(['#e3d9ea', '#c58bd0', '#d55a8a', '#e8703f', '#e8a81f']) },
  { id: 'amber', label: 'Amber', dark: ramp(['#3a3426', '#6b5524', '#a8741c', '#dd941e', '#ffb42a']), light: ramp(['#eadfc8', '#e3c27e', '#dca44a', '#c97f1c', '#a55e05']) },
  { id: 'coolwarm', label: 'Cool–warm', dark: ramp(['#3b6fb6', '#7fa7d9', '#c9c2b8', '#f08a5b', '#e0443a']), light: ramp(['#4d7cc0', '#93b4dd', '#d6cfc4', '#ec8558', '#d23c33']) },
  { id: 'viridis', label: 'Viridis', dark: ramp(['#46327e', '#365c8d', '#277f8e', '#4ac16d', '#fde725']), light: ramp(['#5b3f8f', '#3f6a9a', '#2a8c8f', '#3fae64', '#d9c516']) },
]

export interface MapScheme { base: string; ramp: string }
export const DEFAULT_MAP_SCHEME: MapScheme = { base: 'default', ramp: 'heat' }

/** Every custom property any scheme can set, so switching schemes clears the previous one's values. */
export const ALL_VARS = [...new Set([...BASES, ...RAMPS].flatMap((s) => [...Object.keys(s.dark), ...Object.keys(s.light)]))]

/** The inline CSS variables for a scheme in a theme. Unknown ids fall back to the defaults (no overrides). */
export function schemeVars(s: MapScheme, theme: Theme): Vars {
  const b = BASES.find((x) => x.id === s.base) ?? BASES[0]
  const r = RAMPS.find((x) => x.id === s.ramp) ?? RAMPS[0]
  return { ...b[theme], ...r[theme] }
}

/** Write a scheme onto an element's inline style, clearing any scheme variable it doesn't set. */
export function applyMapScheme(el: HTMLElement, s: MapScheme, theme: Theme) {
  const vars = schemeVars(s, theme)
  for (const k of ALL_VARS) if (!(k in vars)) el.style.removeProperty(k)
  for (const [k, v] of Object.entries(vars)) el.style.setProperty(k, v)
}

const KEY = 'flopx.mapScheme'
function load(): MapScheme {
  try {
    const v = JSON.parse(localStorage.getItem(KEY) ?? 'null') as Partial<MapScheme> | null
    return { base: typeof v?.base === 'string' ? v.base : 'default', ramp: typeof v?.ramp === 'string' ? v.ramp : 'heat' }
  } catch { return DEFAULT_MAP_SCHEME }
}
let state: MapScheme = typeof window === 'undefined' ? DEFAULT_MAP_SCHEME : load()
const subs = new Set<() => void>()

export function setMapScheme(next: Partial<MapScheme>) {
  state = { ...state, ...next }
  try { localStorage.setItem(KEY, JSON.stringify(state)) } catch { /* storage blocked: keep in memory */ }
  subs.forEach((f) => f())
}

export function useMapScheme(): MapScheme {
  return useSyncExternalStore((cb) => { subs.add(cb); return () => { subs.delete(cb) } }, () => state, () => state)
}

const themeOf = (el: HTMLElement): Theme => (el.dataset.blend === 'light' ? 'light' : 'dark')

/** Keeps the chosen scheme applied to <html>, re-applying when the theme switches. Mount once. */
export function useApplyMapScheme() {
  const s = useMapScheme()
  useEffect(() => {
    const el = document.documentElement
    const apply = () => applyMapScheme(el, s, themeOf(el))
    apply()
    const mo = new MutationObserver(apply)
    mo.observe(el, { attributes: true, attributeFilter: ['data-blend'] })
    return () => mo.disconnect()
  }, [s])
}
