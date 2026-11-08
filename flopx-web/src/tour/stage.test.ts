// WF5 #4: a beat's first target spec that exists wins, even when it is off screen; the always-visible
// header fallback is reached only when nothing before it resolves. No DOM in vitest, so the page is a
// fake tree with the parts resolveTarget reads.
import { describe, expect, it } from 'vitest'
import { clipBox, insideClip, resolveTarget, scrollParentOf, settleInScroller, type Box, type Scroller } from './Stage'

interface FakeEl {
  id: string
  textContent: string
  rect: { left: number; top: number; width: number; height: number }
  parentElement: FakeEl | null
  getBoundingClientRect: () => DOMRect
  closest: (sel: string) => FakeEl | null
}
const mk = (id: string, textContent: string, rect: FakeEl['rect'], parentElement: FakeEl | null = null, closestHit: FakeEl | null = null): FakeEl => ({
  id, textContent, rect, parentElement,
  getBoundingClientRect: () => ({ ...rect, right: rect.left + rect.width, bottom: rect.top + rect.height, x: rect.left, y: rect.top }) as DOMRect,
  closest: (sel) => (sel === '[data-tour-stage]' ? null : closestHit),
})

/** The X-Ray page as G-R 7 saw it: the What-if table's eyebrow below the fold inside the right panel, the page h2 always in view. */
function xrayPage(opts: { eyebrow?: boolean } = { eyebrow: true }) {
  const panel = mk('panel', '', { left: 1011, top: 319, width: 429, height: 316 })
  const table = mk('table', '', { left: 1041, top: 910, width: 379, height: 196 }, panel)
  const eyebrow = mk('eyebrow', 'Estimated quantity', { left: 1041, top: 910, width: 379, height: 14 }, table)
  const h2wrap3 = mk('h2wrap3', '', { left: 206, top: 65, width: 1234, height: 120 })
  const h2wrap2 = mk('h2wrap2', '', { left: 216, top: 75, width: 1214, height: 100 }, h2wrap3)
  const h2wrap1 = mk('h2wrap1', '', { left: 216, top: 75, width: 1214, height: 60 }, h2wrap2)
  const h2 = mk('h2', 'Compute X-Ray', { left: 216, top: 75, width: 400, height: 28 }, h2wrap1)
  const bySel: Record<string, FakeEl[]> = { 'main .eyebrow': opts.eyebrow ? [eyebrow, mk('eyebrow2', 'Assumptions', { left: 1041, top: 1200, width: 379, height: 14 })] : [], 'main h2': [h2] }
  const root = { querySelectorAll: (sel: string) => bySel[sel] ?? [] } as unknown as ParentNode
  return { root, table, h2wrap3 }
}

describe('resolveTarget (WF5 #4)', () => {
  it('lights the beat\'s first target even when it is below the fold, not the in-view header', () => {
    const p = xrayPage()
    const el = resolveTarget([{ sel: 'main .eyebrow', text: 'Estimated quantity', up: 1 }, { sel: 'main h2', nth: 0, up: 3 }], p.root)
    expect((el as unknown as FakeEl).id).toBe('table')
  })
  it('falls back to the header only when nothing before it resolves', () => {
    const p = xrayPage({ eyebrow: false })
    const el = resolveTarget([{ sel: 'main .eyebrow', text: 'Estimated quantity', up: 1 }, { sel: 'main h2', nth: 0, up: 3 }], p.root)
    expect((el as unknown as FakeEl).id).toBe('h2wrap3')
  })
  it('returns null with no specs or when nothing matches', () => {
    const p = xrayPage()
    expect(resolveTarget(null, p.root)).toBeNull()
    expect(resolveTarget([{ sel: 'main .nothing' }], p.root)).toBeNull()
  })
  it('skips an element that matches but has no size', () => {
    const flat = mk('flat', 'Estimated quantity', { left: 0, top: 0, width: 0, height: 0 })
    const root = { querySelectorAll: (sel: string) => (sel === 'main .eyebrow' ? [flat] : []) } as unknown as ParentNode
    expect(resolveTarget([{ sel: 'main .eyebrow', text: 'Estimated quantity' }], root)).toBeNull()
  })
})

// WF6 #1 / N-FIX4: a target inside the viewport can still be hidden by its own scroll container (the X-Ray
// right panel's tab body kept beat 7's scrollTop into beat 8, so the Documented ties block sat 260 px above
// the panel's visible box). The stage tests the container as well as the viewport, resets a stale scroll on
// entry, scrolls the target to the nearest edge and measures again. The geometry below is the measured one.
const box = (left: number, top: number, width: number, height: number): Box => ({ left, top, width, height, right: left + width, bottom: top + height })
/** The X-Ray right panel's tab body as G-R 8 found it: visible box 1021,637,419x167, content 415 px, scrollTop 276. */
function fakePanel(scrollTop = 276, contentH = 415, clip = box(1021, 637, 419, 167)) {
  const sc: Scroller & { nearestCalls: number } = { scrollTop, scrollLeft: 0, scrollHeight: contentH, clientHeight: clip.height, parentElement: null, getBoundingClientRect: () => clip, nearestCalls: 0 }
  /** A target `offset` px from the content top, `h` tall: its box follows the container's scroll. */
  const target = (offset: number, h: number) => ({
    measure: () => box(clip.left + 20, clip.top + offset - sc.scrollTop, clip.width - 40, h),
    /** scrollIntoView({ block: 'nearest' }) as CSSOM View defines it, confined to this container. */
    scrollNearest: () => {
      sc.nearestCalls++
      const top = offset - sc.scrollTop, bottom = top + h
      if (top < 0 || (bottom > clip.height && h > clip.height)) sc.scrollTop = offset
      else if (bottom > clip.height) sc.scrollTop = offset + h - clip.height
    },
  })
  return { sc, target }
}

describe('scrollParentOf', () => {
  const style = new Map<Scroller, string>()
  const mk = (overflowY: string, scrollHeight: number, clientHeight: number, parentElement: Scroller | null): Scroller => {
    const s: Scroller = { scrollTop: 0, scrollLeft: 0, scrollHeight, clientHeight, parentElement, getBoundingClientRect: () => box(0, 0, 0, 0) }
    style.set(s, overflowY)
    return s
  }
  const of = (s: Scroller) => style.get(s) ?? 'visible'
  it('finds the nearest ancestor that scrolls vertically and has more content than room', () => {
    const main = mk('auto', 2000, 824, null)
    const panel = mk('auto', 415, 167, main)
    const wrap = mk('visible', 415, 415, panel)
    expect(scrollParentOf({ parentElement: wrap }, of)).toBe(panel)
  })
  it('skips an overflow:auto ancestor whose content fits, so the page column is found behind it', () => {
    const main = mk('auto', 2000, 824, null)
    const fits = mk('auto', 300, 300, main)
    expect(scrollParentOf({ parentElement: fits }, of)).toBe(main)
  })
  it('returns null when nothing above the element scrolls', () => {
    const wrap = mk('visible', 100, 100, mk('hidden', 500, 100, null))
    expect(scrollParentOf({ parentElement: wrap }, of)).toBeNull()
    expect(scrollParentOf(null, of)).toBeNull()
  })
})

describe('insideClip', () => {
  const clip = box(1021, 637, 419, 167)
  it('is false for the G-R 8 block 260 px above its panel\'s visible box, although the rect is inside the viewport', () => {
    expect(insideClip(box(1041, 377, 379, 43), clip)).toBe(false)
  })
  it('is true for a block inside the box, with a pixel of slack at each edge', () => {
    expect(insideClip(box(1041, 653, 379, 43), clip)).toBe(true)
    expect(insideClip(box(1041, 636, 379, 168), clip)).toBe(true)
    expect(insideClip(box(1041, 635, 379, 43), clip)).toBe(false)
  })
  it('is false for a short block that runs past the bottom, or past the right edge', () => {
    expect(insideClip(box(1041, 780, 379, 43), clip)).toBe(false)
    expect(insideClip(box(1041, 653, 420, 43), clip)).toBe(false)
  })
  it('counts the G-R 7 table, taller than its 143 px box, once its top sits at the box\'s top', () => {
    const c7 = box(1021, 661, 419, 143)
    expect(insideClip(box(1041, 661, 379, 196), c7)).toBe(true)
    expect(insideClip(box(1041, 634, 379, 196), c7)).toBe(false)
    expect(insideClip(box(1041, 720, 379, 196), c7)).toBe(false)
  })
})

describe('clipBox', () => {
  const clip = box(1021, 661, 419, 143)
  it('keeps the part of the target the container shows', () => {
    expect(clipBox(box(1041, 661, 379, 196), clip)).toEqual(box(1041, 661, 379, 143))
    expect(clipBox(box(1041, 634, 379, 196), clip)).toEqual(box(1041, 661, 379, 143))
    expect(clipBox(box(1041, 720, 379, 120), clip)).toEqual(box(1041, 720, 379, 804 - 720))
  })
  it('leaves a target that is inside, or wholly outside, as it is', () => {
    expect(clipBox(box(1041, 700, 379, 40), clip)).toEqual(box(1041, 700, 379, 40))
    expect(clipBox(box(1041, 377, 379, 43), clip)).toEqual(box(1041, 377, 379, 43))
  })
})

describe('settleInScroller', () => {
  const base = { entering: true, sameAsLast: false, mayScroll: true }
  it('passes a target through when it has no scroll container of its own', () => {
    const r = box(580, 75, 836, 201)
    expect(settleInScroller(r, { ...base, scroller: null, scrollNearest: () => { throw new Error('no') }, measure: () => r })).toEqual({ r, clip: null, scrolled: false })
  })
  it('G-R 8 on entry: resets the panel\'s stale scroll, so the block lands at its natural place, and returns the re-measured box', () => {
    const p = fakePanel()
    const t = p.target(16, 43)
    expect(t.measure()).toEqual(box(1041, 377, 379, 43))
    const out = settleInScroller(t.measure(), { ...base, scroller: p.sc, ...t })
    expect(p.sc.scrollTop).toBe(0)
    expect(out.scrolled).toBe(true)
    expect(out.r).toEqual(box(1041, 653, 379, 43))
    expect(insideClip(out.r, out.clip!)).toBe(true)
    expect(p.sc.nearestCalls).toBe(0)
  })
  it('a target far down a freshly reset container is then scrolled to the nearest edge, and the box is read after the scroll', () => {
    const p = fakePanel()
    const t = p.target(300, 43)
    const out = settleInScroller(t.measure(), { ...base, scroller: p.sc, ...t })
    expect(p.sc.nearestCalls).toBe(1)
    expect(p.sc.scrollTop).toBe(300 + 43 - 167)
    expect(out.r).toEqual(box(1041, 637 + 167 - 43, 379, 43))
    expect(insideClip(out.r, out.clip!)).toBe(true)
  })
  it('G-R 7: a table taller than its box is aligned to the top and counted as settled', () => {
    const p = fakePanel(0, 684, box(1021, 661, 419, 143))
    const t = p.target(249, 196)
    const out = settleInScroller(t.measure(), { ...base, scroller: p.sc, ...t })
    expect(p.sc.scrollTop).toBe(249)
    expect(out.r).toEqual(box(1041, 661, 379, 196))
    expect(insideClip(out.r, out.clip!)).toBe(true)
    expect(clipBox(out.r, out.clip!).height).toBe(143)
  })
  it('does not touch a container whose target is the same one the last beat lit and is already visible', () => {
    const p = fakePanel(100, 415)
    const t = p.target(110, 43)
    const out = settleInScroller(t.measure(), { ...base, sameAsLast: true, scroller: p.sc, ...t })
    expect(p.sc.scrollTop).toBe(100)
    expect(out.scrolled).toBe(false)
    expect(p.sc.nearestCalls).toBe(0)
  })
  it('after entry, re-scrolls a clipped target only when the cadence allows, and never resets the container again', () => {
    const p = fakePanel(276)
    const t = p.target(16, 43)
    const held = settleInScroller(t.measure(), { ...base, entering: false, mayScroll: false, scroller: p.sc, ...t })
    expect(held.scrolled).toBe(false)
    expect(p.sc.scrollTop).toBe(276)
    const out = settleInScroller(t.measure(), { ...base, entering: false, mayScroll: true, scroller: p.sc, ...t })
    expect(p.sc.nearestCalls).toBe(1)
    expect(p.sc.scrollTop).toBe(16)
    expect(out.r).toEqual(box(1041, 637, 379, 43))
  })
})
