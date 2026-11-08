import { afterEach, describe, expect, it } from 'vitest'
import type { Store } from '@/lib/store'
import { leadsWith, phaseOf, phasesOf, SPINE } from './model'
import { REVEAL_HOLD_MS, TOUR_IDS, TOUR_LENGTHS, TOURS, toXrayReveal } from './tours'

/** A store that records the X-Ray patches the beat applies. */
function fakeStore() {
  const patches: Record<string, unknown>[] = []
  const s = { setInspector: () => {}, setXray: (p: unknown) => { patches.push(p as Record<string, unknown>) }, navigate: () => {} } as unknown as Store
  return { s, patches }
}
const g = globalThis as unknown as { window?: unknown }
const savedWindow = g.window
afterEach(() => { if (savedWindow === undefined) delete g.window; else g.window = savedWindow })

describe('the reveal beat under reduced motion (WF5 #9)', () => {
  it('shows the end state at once: reveal 100 in the first patch and no timer', async () => {
    g.window = { matchMedia: () => ({ matches: true }) }
    const { s, patches } = fakeStore()
    const cleanup = toXrayReveal(s)
    expect(patches[0]?.reveal).toBe(100)
    await new Promise((r) => setTimeout(r, REVEAL_HOLD_MS + 100))
    expect(patches.length).toBe(1)
    if (typeof cleanup === 'function') cleanup()
  })
  it('without the preference starts at 0 and only moves after the hold', async () => {
    g.window = { matchMedia: () => ({ matches: false }) }
    const { s, patches } = fakeStore()
    const cleanup = toXrayReveal(s)
    expect(patches[0]?.reveal).toBe(0)
    await new Promise((r) => setTimeout(r, 200))
    expect(patches.length).toBe(1)
    if (typeof cleanup === 'function') cleanup()
  })
})

describe('the three tours', () => {
  it('are G-Research, HRT and Metaview, each with unique beat ids and a card for every beat', async () => {
    expect(TOUR_IDS.map((id) => TOURS[id].name)).toEqual(['G-Research', 'HRT', 'Metaview'])
    const ids = TOUR_IDS.flatMap((id) => TOURS[id].beats.map((b) => b.id))
    expect(new Set(ids).size).toBe(ids.length)
    // Every beat id is dispatched by cards.tsx (the switch lists it).
    const src = await import('node:fs').then((fs) => fs.readFileSync(new URL('./cards.tsx', import.meta.url), 'utf8'))
    for (const id of ids) expect(src, `card for ${id}`).toContain(`'${id}'`)
  })
  it('share the six-beat spine: every phase is on it and phases never run backwards', () => {
    for (const id of TOUR_IDS) {
      const ns = TOURS[id].beats.map((b) => phaseOf(b.phase).n)
      for (let i = 1; i < ns.length; i++) expect(ns[i], `${id} beat ${i}`).toBeGreaterThanOrEqual(ns[i - 1])
      expect(ns.every((n) => SPINE.some((s) => s.n === n))).toBe(true)
    }
  })
  it('G-Research leads with 1, 3, 4, 5; HRT with 2, 5, 6; Metaview runs all six and ends on the review', () => {
    expect(leadsWith(TOURS.gr.beats, 4)).toEqual([1, 3, 4, 5])
    expect(leadsWith(TOURS.hrt.beats, 3)).toEqual([2, 5, 6])
    expect(phasesOf(TOURS.mv.beats)).toEqual(['invisible', 'retrieval', 'analysis', 'modelling', 'estimation', 'decision', 'review'])
    expect(TOURS.mv.beats[TOURS.mv.beats.length - 1].target).toBeNull()
  })
  it('Metaview is the longest, 8 to 10 beats; lengths are published for the store', () => {
    const n = TOURS.mv.beats.length
    expect(n).toBeGreaterThanOrEqual(8)
    expect(n).toBeLessThanOrEqual(10)
    expect(n).toBeGreaterThanOrEqual(TOURS.gr.beats.length)
    expect(n).toBeGreaterThanOrEqual(TOURS.hrt.beats.length)
    expect(TOUR_LENGTHS).toEqual({ gr: TOURS.gr.beats.length, hrt: TOURS.hrt.beats.length, mv: n })
  })
  it('every beat dwells long enough to read and every spotlight spec is a valid selector', () => {
    for (const id of TOUR_IDS) for (const b of TOURS[id].beats) {
      expect(b.duration).toBeGreaterThanOrEqual(6000)
      for (const t of b.target ?? []) expect(t.sel.trim().length, `${b.id} selector`).toBeGreaterThan(0)
    }
  })
})
