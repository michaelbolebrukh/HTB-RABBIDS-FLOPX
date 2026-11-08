// The presenter stage: dims the page around a spotlight cut out over the real element a beat talks
// about, anchors the beat's card beside it with an animated connector, and carries the progress
// rail with keyboard, autoplay and a Present mode (larger type for a projector). The page under it
// stays fully interactive: the dim never takes pointer events, only the card and the rail do.
// Below 768 px the card becomes a bottom sheet and the rail collapses to "beat n of N" (the design
// has no mobile layout, so the tour says so rather than pretending).
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { ChevronLeft, ChevronRight, Maximize2, Minimize2, Pause, Play, X } from 'lucide-react'
import { useStore, type Store } from '@/lib/store'
import { cx, Kbd } from '@/components/ui'
import { CardBody } from './cards'
import { placeCard, placeSheet, railReserve, type Placement, type Rect } from './geometry'
import { goTo, keyAction, next, phaseOf, phasesOf, prev, togglePlay, togglePresent, type TargetSpec } from './model'
import { prefersReducedMotion, useMounted } from './motion'
import { loadSources } from './sources'
import { TOURS, type Beat } from './tours'

export const RAIL_H = 64
/** The collapsed rail below 768 px. */
export const RAIL_H_MOBILE = 44
export const MOBILE_MAX_W = 768
/** Default card width at 1440; dense beats widen it (tours.ts). Present widens by 22% on top. */
const CARD_W = 560
/** Present mode scales the card type (N-TOUR2 #4: 1.25, overriding the stylesheet's default). */
export const PRESENT_SCALE = 1.25
const FIND_GRACE_MS = 1400
/** Below this width a spotlighted element is not worth lighting on a phone; the page section stands in. */
const MIN_SPOT_W = 24

const isMobile = (w: number) => w < MOBILE_MAX_W

/**
 * Resolve a beat's target against the live DOM. Specs are tried in order and the first that exists
 * wins; the stage then scrolls it into view (a panel that is its own scroll container scrolls
 * inside). The always-visible fallbacks at the end of a spec list (the page header) are reached only
 * when nothing before them resolves, so an off-screen primary target is lit, not skipped (WF5 #4).
 */
export function resolveTarget(specs: TargetSpec[] | null, root: ParentNode = document): Element | null {
  if (!specs) return null
  for (const spec of specs) {
    const el = resolveOne(spec, root)
    if (el) return el
  }
  return null
}

/** One spec -> its element, or null when it matches nothing rendered with a size. */
function resolveOne(spec: TargetSpec, root: ParentNode): Element | null {
  let els: Element[]
  try { els = Array.from(root.querySelectorAll(spec.sel)) } catch { return null }
  els = els.filter((e) => !e.closest('[data-tour-stage]'))
  if (spec.text) { const t = spec.text.toLowerCase(); els = els.filter((e) => (e.textContent ?? '').toLowerCase().includes(t)) }
  let el: Element | null = els[spec.nth ?? 0] ?? null
  if (!el) return null
  if (spec.closest) el = el.closest(spec.closest) ?? el
  for (let i = 0; el && i < (spec.up ?? 0); i++) el = el.parentElement
  if (!el) return null
  const r = el.getBoundingClientRect()
  return r.width > 0 && r.height > 0 ? el : null
}

const toRect = (r: DOMRect): Rect => ({ x: r.left, y: r.top, w: r.width, h: r.height })
const same = (a: Rect | null, b: Rect | null) => (!a && !b) || (!!a && !!b && Math.abs(a.x - b.x) < 0.5 && Math.abs(a.y - b.y) < 0.5 && Math.abs(a.w - b.w) < 0.5 && Math.abs(a.h - b.h) < 0.5)

/** Is the element's box inside the viewport above the reserved bottom band? A tall element counts when its top is near the top. */
const inView = (r: DOMRect, reserved = RAIL_H) => {
  const limit = window.innerHeight - reserved
  if (r.left < 0 || r.right > window.innerWidth || r.top < 0) return false
  if (r.bottom <= limit) return true
  return r.height > limit * 0.5 && r.top < window.innerHeight * 0.3
}

/** The edges of a box, as `getBoundingClientRect` reports them. */
export interface Box { left: number; top: number; right: number; bottom: number; width: number; height: number }

/** The parts of a scroll container the stage reads; a DOM element satisfies it, and so does a test fake. */
export interface Scroller {
  getBoundingClientRect: () => Box
  scrollTop: number
  scrollLeft: number
  scrollHeight: number
  clientHeight: number
  parentElement: Scroller | null
}

/**
 * The nearest ancestor that scrolls vertically (overflow auto or scroll, with more content than room),
 * or null when only the page scrolls. The X-Ray right panel is one: its tab body is its own scroll
 * container, so a target can sit inside the viewport and still be clipped by the panel (WF6 #1).
 */
export function scrollParentOf(el: { parentElement: Scroller | null } | null, overflowYOf: (e: Scroller) => string = (e) => getComputedStyle(e as unknown as Element).overflowY): Scroller | null {
  const stop = typeof document === 'undefined' ? null : (document.body as unknown as Scroller)
  let p = el?.parentElement ?? null
  while (p && p !== stop) {
    const o = overflowYOf(p)
    if ((o === 'auto' || o === 'scroll' || o === 'overlay') && p.scrollHeight > p.clientHeight + 1) return p
    p = p.parentElement
  }
  return null
}

/**
 * Is `r` inside `clip`, the visible box of its scroll container? The same shape as the viewport test:
 * an element taller than half the clip counts when its top sits in the clip's upper part, since
 * nothing can show all of it at once. `slack` forgives sub-pixel rounding.
 */
export function insideClip(r: Box, clip: Box, slack = 1): boolean {
  if (r.left < clip.left - slack || r.right > clip.right + slack || r.top < clip.top - slack) return false
  if (r.bottom <= clip.bottom + slack) return true
  return r.height > clip.height * 0.5 && r.top < clip.top + clip.height * 0.3
}

/** The part of `r` that `clip` shows (the cut-out lights what a viewer can see, not what the panel hides); `r` itself when they do not overlap. */
export function clipBox(r: Box, clip: Box): Box {
  const left = Math.max(r.left, clip.left), top = Math.max(r.top, clip.top)
  const right = Math.min(r.right, clip.right), bottom = Math.min(r.bottom, clip.bottom)
  if (right - left <= 0 || bottom - top <= 0) return r
  return { left, top, right, bottom, width: right - left, height: bottom - top }
}

export interface SettleOpts {
  /** The target's scroll container, when it has one other than the page. */
  scroller: Scroller | null
  /** The first time this beat's target resolves inside this container (not the beat's first resolve: a fallback can win a tick while a tab switches). */
  entering: boolean
  /** Was the previous beat's target this element inside this container? A new target in a container resets its scroll first. */
  sameAsLast: boolean
  /** The last time the stage scrolled for this beat was long enough ago to scroll again. */
  mayScroll: boolean
  /** `el.scrollIntoView({ block: 'nearest' })`, injected so the procedure runs without a DOM. */
  scrollNearest: () => void
  /** Re-read the target's box after a scroll. */
  measure: () => Box
}

/**
 * Bring a target into its scroll container's view and re-measure it. On entering a beat whose
 * target is new to its container (a different tab's content, or a different panel from the last
 * beat's), the container's stale scroll is reset first, then the target is scrolled to the nearest
 * edge so it is fully visible; the box is measured again afterwards, since scrolling moved it. Returns
 * the fresh box, the container's visible box, and whether a scroll happened.
 */
export function settleInScroller(r: Box, o: SettleOpts): { r: Box; clip: Box | null; scrolled: boolean } {
  const sc = o.scroller
  if (!sc) return { r, clip: null, scrolled: false }
  let clip = sc.getBoundingClientRect()
  let scrolled = false
  if (o.entering && !o.sameAsLast && (sc.scrollTop !== 0 || sc.scrollLeft !== 0)) {
    sc.scrollTop = 0
    sc.scrollLeft = 0
    r = o.measure()
    clip = sc.getBoundingClientRect()
    scrolled = true
  }
  if (!insideClip(r, clip) && (o.mayScroll || scrolled || o.entering)) {
    try { o.scrollNearest() } catch { /* ignore */ }
    r = o.measure()
    clip = sc.getBoundingClientRect()
    scrolled = true
  }
  return { r, clip, scrolled }
}
/** A target this tall or wide is scrolled to the top of the page so the card gets the room below it. */
const wantsTop = (r: DOMRect, reserved: number) => r.height > (window.innerHeight - reserved) * 0.45 || r.width > window.innerWidth * 0.55
/** Scroll again at most this often while the target stays out of view (layout keeps moving as live data lands). */
const RESCROLL_MS = 1800

export function TourStage() {
  const store = useStore()
  const { tour, setTour } = store
  const storeRef = useRef<Store>(store)
  storeRef.current = store
  const t = tour ? TOURS[tour.id] : null
  const beat: Beat | null = t ? t.beats[tour!.beat] ?? null : null
  const n = t?.beats.length ?? 0
  const beatKey = tour ? `${tour.id}:${tour.beat}` : ''

  const [vp, setVp] = useState({ w: typeof window === 'undefined' ? 1440 : window.innerWidth, h: typeof window === 'undefined' ? 900 : window.innerHeight })
  useEffect(() => {
    const fn = () => setVp({ w: window.innerWidth, h: window.innerHeight })
    window.addEventListener('resize', fn)
    return () => window.removeEventListener('resize', fn)
  }, [])
  const mobile = isMobile(vp.w)
  const nominalRailH = mobile ? RAIL_H_MOBILE : RAIL_H
  // The band the cut-out and the card keep clear is the rail's measured top, not its nominal height:
  // the rail's bottom padding puts its top 8 px above `vp.h - RAIL_H` at 1440 (WF5 #3).
  const railRef = useRef<HTMLDivElement>(null)
  const [railTop, setRailTop] = useState<number | null>(null)
  const measureRail = useCallback(() => {
    const r = railRef.current?.getBoundingClientRect()
    const top = r && r.height > 0 ? r.top : null
    setRailTop((cur) => (cur === top || (cur != null && top != null && Math.abs(cur - top) < 0.5) ? cur : top))
  }, [])
  useLayoutEffect(() => {
    const el = railRef.current
    if (!el) return
    const ro = new ResizeObserver(measureRail)
    ro.observe(el)
    measureRail()
    return () => ro.disconnect()
  }, [!!tour, mobile, measureRail])
  const railH = railReserve(vp.h, railTop, nominalRailH)

  // While a tour runs the page gets room to scroll past the rail (index.css, html[data-tour]).
  useEffect(() => {
    if (!tour) return
    document.documentElement.setAttribute('data-tour', '')
    return () => document.documentElement.removeAttribute('data-tour')
  }, [!!tour])

  // Put the app on the beat's screen when the beat changes; the beat may hand back a cleanup.
  useEffect(() => {
    if (!tour || !beat) return
    void loadSources()
    const cleanup = beat.run?.(storeRef.current)
    return () => { if (typeof cleanup === 'function') cleanup() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [beatKey])

  // Card size, measured at its natural height (header + body content + footer) so a constrained card
  // does not feed its own smaller size back into placement.
  const cardRef = useRef<HTMLDivElement>(null)
  const headRef = useRef<HTMLDivElement>(null)
  const bodyRef = useRef<HTMLDivElement>(null)
  const footRef = useRef<HTMLDivElement>(null)
  const [cardH, setCardH] = useState(240)
  const measureCard = useCallback(() => {
    const head = headRef.current, body = bodyRef.current, foot = footRef.current
    if (!head || !body || !foot) return
    const h = Math.ceil(head.getBoundingClientRect().height + body.scrollHeight + foot.getBoundingClientRect().height + 2)
    setCardH((cur) => (Math.abs(cur - h) < 1 ? cur : h))
  }, [])
  useLayoutEffect(() => {
    const el = cardRef.current
    if (!el) return
    const ro = new ResizeObserver(measureCard)
    ro.observe(el)
    if (bodyRef.current?.firstElementChild) ro.observe(bodyRef.current.firstElementChild)
    measureCard()
    return () => ro.disconnect()
  }, [beatKey, tour?.present, measureCard])

  const present = tour?.present ?? false
  // The sheet takes at most 45% of a phone's height; the body scrolls inside it.
  const sheetH = Math.min(cardH, Math.floor(vp.h * 0.45))

  // Find and follow the target: poll (pages render after navigation, live data lands later), plus resize and scroll.
  const [rect, setRect] = useState<Rect | null>(null)
  const [fallback, setFallback] = useState(false)
  const [lost, setLost] = useState(false)
  const lastScroll = useRef(0)
  const beatStart = useRef(0)
  const reservedRef = useRef(railH)
  reservedRef.current = mobile ? railH + sheetH + 8 : railH
  // The previous beat's target and its scroll container: a beat whose target is new to a container
  // (another tab's content in the same panel, or another panel) starts from that container's top.
  const lastTarget = useRef<{ el: Element | null; sc: Scroller | null }>({ el: null, sc: null })
  useEffect(() => {
    if (!beat) { setRect(null); return }
    beatStart.current = performance.now()
    lastScroll.current = 0
    setLost(false)
    setFallback(false)
    let raf = 0
    let first = true
    // Inner scroll containers this beat has already settled a target in (reset once per beat, per container).
    const entered = new Set<Scroller>()
    const measure = () => {
      measureCard()
      measureRail()
      const reserved = reservedRef.current
      const el = resolveTarget(beat.target, document)
      if (el) {
        let r: DOMRect = el.getBoundingClientRect()
        let sinceScroll = performance.now() - lastScroll.current
        // 1. The target's own scroll container inside the page (the X-Ray right panel's tab body): reset a
        //    stale scroll on entry, scroll the target to the nearest edge when the container clips it, and
        //    measure again, because a rect inside the viewport can still be hidden by its panel (WF6 #1).
        //    The page column itself (#main-scroll) is left to step 2: it spans the viewport, `navigate`
        //    resets it per page, and the smooth centred scroll there should not be preceded by a jump.
        const main = document.getElementById('main-scroll')
        const sc = scrollParentOf(el as unknown as { parentElement: Scroller | null })
        const inner = sc && sc !== (main as unknown as Scroller | null) ? sc : null
        const entering = !!inner && !entered.has(inner)
        if (inner) entered.add(inner)
        const settled = settleInScroller(r, {
          scroller: inner,
          entering,
          sameAsLast: lastTarget.current.el === el && lastTarget.current.sc === inner,
          mayScroll: sinceScroll > RESCROLL_MS,
          scrollNearest: () => el.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'auto' }),
          measure: () => el.getBoundingClientRect(),
        })
        r = settled.r as DOMRect
        if (settled.scrolled) { lastScroll.current = performance.now(); sinceScroll = 0 }
        lastTarget.current = { el, sc: inner }
        // What a viewer can see of the target: the part its container shows. The cut-out lights that part,
        // and the viewport test judges it, so a table taller than its panel is not re-centred (which would
        // scroll the panel too and hide the table's top rows again) once its top is at the panel's top.
        const clip = settled.clip ?? (sc && main && sc === (main as unknown as Scroller) ? main.getBoundingClientRect() : null)
        const visible = (clip ? clipBox(r, clip) : r) as DOMRect
        // 2. The viewport: bring the target into the area above the rail (a tall one to the top of the page).
        const tall = wantsTop(visible, reserved)
        const mainTop = main?.getBoundingClientRect().top ?? 0
        if ((!inView(visible, reserved) && sinceScroll > RESCROLL_MS) || (first && tall && visible.top > mainTop + 40 && !isMobile(window.innerWidth))) {
          lastScroll.current = performance.now()
          try { el.scrollIntoView({ block: tall ? 'start' : 'center', inline: 'nearest', behavior: prefersReducedMotion() ? 'auto' : 'smooth' }) } catch { /* ignore */ }
        }
        first = false
        let nextRect = toRect(visible)
        let fb = false
        if (isMobile(window.innerWidth) && (r.width < MIN_SPOT_W || !inView(r, reserved))) {
          // The design's 174 px content column cannot show most targets on a phone: light the page section instead.
          const main = document.getElementById('main-scroll')
          if (main) { nextRect = toRect(main.getBoundingClientRect()); fb = true }
        }
        setFallback(fb)
        setRect((cur) => (same(cur, nextRect) ? cur : nextRect))
        setLost(false)
      } else {
        setRect(null)
        if (performance.now() - beatStart.current > FIND_GRACE_MS) setLost(true)
      }
    }
    measure()
    const id = setInterval(measure, 240)
    const onMove = () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(measure) }
    window.addEventListener('resize', onMove)
    document.addEventListener('scroll', onMove, true)
    return () => { clearInterval(id); cancelAnimationFrame(raf); window.removeEventListener('resize', onMove); document.removeEventListener('scroll', onMove, true) }
  }, [beat, beatKey, measureCard, measureRail])

  const cardW = Math.min(vp.w - 32, Math.round((beat?.width ?? CARD_W) * (present ? 1.22 : 1)))
  const placement: Placement = useMemo(() => {
    const target = beat?.target && !lost ? rect : null
    if (mobile) return placeSheet(target, { viewport: vp, card: { w: vp.w, h: cardH }, railH, sheetH, pad: 6 })
    return placeCard(target, { viewport: vp, card: { w: cardW, h: cardH }, railH, prefer: beat?.prefer, pad: 10, gap: 20 })
  }, [rect, lost, vp, cardW, cardH, beat, mobile, railH, sheetH])
  const anchored = placement.side !== 'centre'
  // Until the target is found (or given up on), keep the card where it was rather than jumping to the centre.
  const lastRef = useRef<Placement>(placement)
  const shown = beat?.target && !rect && !lost ? lastRef.current : placement
  if (anchored || lost || !beat?.target) lastRef.current = placement

  // Keyboard.
  useEffect(() => {
    if (!tour) return
    const fn = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null
      const inField = !!el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable)
      const a = keyAction(e.key, inField)
      if (!a) return
      if (a === 'exit' && (storeRef.current.palette || storeRef.current.ticket || storeRef.current.inspector)) return
      e.preventDefault()
      setTour((s) => {
        if (!s) return s
        const len = TOURS[s.id].beats.length
        switch (a) {
          case 'next': return next({ ...s, playing: false }, len)
          case 'prev': return prev({ ...s, playing: false })
          case 'play': return togglePlay(s)
          case 'first': return goTo(s, 0, len)
          case 'last': return goTo(s, len - 1, len)
          case 'present': return togglePresent(s)
          case 'exit': return null
        }
      })
    }
    window.addEventListener('keydown', fn)
    return () => window.removeEventListener('keydown', fn)
  }, [!!tour, setTour])

  // Autoplay: dwell per beat, stop at the end. A beat that waits for a live answer (`await`) arms its
  // dwell only once the answer has landed, or once the wait cap has passed (the card then says so).
  const playing = tour?.playing ?? false
  const [armed, setArmed] = useState(false)
  useEffect(() => {
    setArmed(false)
    if (!playing || !beat) return
    let timer: ReturnType<typeof setTimeout> | undefined
    let poll: ReturnType<typeof setInterval> | undefined
    const start = performance.now()
    const arm = () => { setArmed(true); timer = setTimeout(() => setTour((s) => (s ? next(s, TOURS[s.id].beats.length) : s)), beat.duration) }
    const w = beat.await
    if (!w) arm()
    else {
      const check = () => { if (w.ready() || performance.now() - start > w.capMs) { if (poll) clearInterval(poll); arm() } }
      poll = setInterval(check, 300)
      check()
    }
    return () => { if (timer) clearTimeout(timer); if (poll) clearInterval(poll) }
  }, [playing, beatKey, beat, setTour])
  const dwellOn = useMounted(`${beatKey}:${playing}:${armed}`)

  const step = useCallback((d: 1 | -1) => setTour((s) => (s ? (d > 0 ? next({ ...s, playing: false }, TOURS[s.id].beats.length) : prev({ ...s, playing: false })) : s)), [setTour])

  if (!tour || !t || !beat) return null
  const phases = phasesOf(t.beats)
  const ph = phaseOf(beat.phase)
  const toneVar = t.tone === 'inf' ? 'var(--color-inf)' : t.tone === 'dec' ? 'var(--color-dec)' : 'var(--color-fct)'
  const toneText = t.tone === 'inf' ? 'text-inf' : t.tone === 'dec' ? 'text-dec' : 'text-fct'
  const s = shown.spotlight
  const c = shown.card
  const connector = shown.connector
  const sheet = shown.side === 'sheet' || (mobile && !anchored)
  const cardStyle: React.CSSProperties = sheet
    ? { top: vp.h - railH - sheetH, left: 0, width: vp.w, height: sheetH }
    : { top: c.y, left: c.x, width: c.w, maxHeight: Math.min(c.h, vp.h - railH - 32) }

  return (
    <div className="tour-stage pointer-events-none fixed inset-0 z-[75]" data-tour-stage data-present={present} data-mobile={mobile} data-side={shown.side} data-shrunk={shown.shrunk} data-clipped={shown.clipped} data-fallback={fallback} style={{ '--tour-ring': toneVar, '--tour-scale': present ? PRESENT_SCALE : 1 } as React.CSSProperties} aria-live="polite">
      {/* Dim with the spotlight cut out (a zero-size box when there is no target: the shadow then covers the page). */}
      <div className="tour-dim" data-none={!anchored} style={anchored ? { top: s.y, left: s.x, width: s.w, height: s.h } : { top: -20, left: -20, width: 0, height: 0 }} />
      {anchored && connector && (
        <svg key={`${beatKey}:c`} className="tour-connector pointer-events-none fixed inset-0 h-full w-full" aria-hidden>
          <path d={`M${connector.from.x} ${connector.from.y} C ${(connector.from.x + connector.to.x) / 2} ${connector.from.y}, ${(connector.from.x + connector.to.x) / 2} ${connector.to.y}, ${connector.to.x} ${connector.to.y}`} fill="none" stroke={toneVar} strokeWidth="1.25" />
          <circle cx={connector.to.x} cy={connector.to.y} r="3.5" fill={toneVar} />
        </svg>
      )}

      {/* The card (a bottom sheet below 768 px) */}
      <div ref={cardRef} className={cx('tour-card pointer-events-auto flex flex-col', sheet && 'tour-sheet')} style={cardStyle} role="dialog" aria-label={`${t.name} walkthrough, beat ${tour.beat + 1} of ${n}: ${beat.title}`}>
        <div key={beatKey} className={cx('tour-card-in flex min-h-0 flex-1 flex-col overflow-hidden border border-line2 bg-panel/95 shadow-2xl backdrop-blur', sheet ? 'rounded-t-lg border-b-0' : 'rounded-lg')} style={{ borderTopColor: toneVar }}>
          <div ref={headRef} className={cx('flex shrink-0 items-start justify-between gap-3 border-b border-line px-4', sheet ? 'py-2' : 'py-3')}>
            <div className="min-w-0">
              <div className={cx('mb-0.5 flex items-center gap-2 font-mono text-[0.78em] tracking-wider uppercase', toneText)}><span className="num">{String(ph.n).padStart(2, '0')}</span><span>{ph.label}</span><span className="text-dim">· {t.name}</span></div>
              <h2 className={cx('font-serif leading-tight tracking-tight text-bone', sheet ? 'text-[1.3em]' : 'text-[1.55em]')}>{beat.title}</h2>
              {sheet && <div className="mt-0.5 text-[0.8em] text-dim">Best viewed on a desktop: the design has no phone layout, so {fallback ? 'the page section stands in for the spotlight' : 'the card sits below the spotlight'}.</div>}
            </div>
            <div className="flex shrink-0 items-center gap-1.5">
              <span className="num text-[0.85em] text-dim">{tour.beat + 1}/{n}</span>
              <button onClick={() => setTour(null)} className="cursor-pointer rounded-[3px] p-1 text-dim hover:bg-hover hover:text-bone" aria-label="Exit walkthrough" title="Exit (Esc)"><X size={14} /></button>
            </div>
          </div>
          <div ref={bodyRef} className="tour-card-body min-h-0 flex-1 overflow-y-auto px-4 py-3">
            <div><CardBody beatId={beat.id} store={store} /></div>
          </div>
          <div ref={footRef} className="flex shrink-0 items-center justify-between gap-2 border-t border-line px-4 py-2">
            <span className="hidden items-center gap-1.5 font-mono text-[0.72em] text-dim md:flex"><Kbd>←</Kbd><Kbd>→</Kbd> beats <Kbd>space</Kbd> play <Kbd>P</Kbd> present <Kbd>esc</Kbd> exit</span>
            <div className="flex items-center gap-1.5">
              <button onClick={() => step(-1)} disabled={tour.beat === 0} className="flex h-7 cursor-pointer items-center rounded-[4px] border border-line2 px-2 text-[0.85em] text-bone hover:bg-hover disabled:cursor-not-allowed disabled:opacity-40" aria-label="Previous beat"><ChevronLeft size={13} /></button>
              {tour.beat < n - 1
                ? <button onClick={() => step(1)} className="flex h-7 cursor-pointer items-center gap-1 rounded-[4px] bg-bone px-2.5 text-[0.85em] font-medium text-ink hover:bg-white" aria-label="Next beat">Next <ChevronRight size={13} /></button>
                : <button onClick={() => setTour(null)} className="flex h-7 cursor-pointer items-center gap-1 rounded-[4px] bg-bone px-2.5 text-[0.85em] font-medium text-ink hover:bg-white">Finish</button>}
            </div>
          </div>
        </div>
      </div>

      {/* Progress rail (collapsed to "beat n of N" and the phase below 768 px) */}
      <div className={cx('pointer-events-auto fixed inset-x-0 bottom-0 z-[76] flex justify-center', mobile ? 'px-0 pb-0' : 'px-4 pb-3')}>
        <div ref={railRef} className={cx('flex w-full flex-col gap-1.5 border border-line2 bg-ink/92 shadow-2xl backdrop-blur', mobile ? 'rounded-none border-x-0 border-b-0 px-3 py-1.5' : 'max-w-[1100px] rounded-lg px-4 py-2')} role="toolbar" aria-label="Walkthrough progress">
          <div className="flex items-center gap-3">
            <span className={cx('flex shrink-0 items-center gap-2 font-mono text-[0.8em] tracking-wider', toneText)}><span aria-hidden className="size-[7px] rounded-[1.5px]" style={{ background: toneVar }} />{mobile ? t.name : t.name.toUpperCase()}</span>
            {mobile ? (
              <span className="min-w-0 flex-1 truncate text-[0.8em] text-mute"><span className="num text-bone">beat {tour.beat + 1} of {n}</span> · {ph.label}</span>
            ) : (
              <div className="flex min-w-0 flex-1 items-center gap-1 overflow-hidden">
                {phases.map((p) => {
                  const m = phaseOf(p)
                  const active = p === beat.phase
                  const done = phases.indexOf(p) < phases.indexOf(beat.phase)
                  const first = t.beats.findIndex((b) => b.phase === p)
                  return (
                    <button key={p} onClick={() => setTour((st) => (st ? goTo({ ...st, playing: false }, first, n) : st))} className={cx('flex min-w-0 cursor-pointer items-center gap-1.5 rounded-[3px] px-2 py-1 text-left text-[0.8em] transition-colors', active ? 'bg-raise text-bone' : done ? 'text-mute hover:text-bone' : 'text-dim hover:text-mute')} title={m.label}>
                      <span className="num">{m.n}</span><span className="truncate">{m.short}</span>
                    </button>
                  )
                })}
              </div>
            )}
            <div className="flex items-center gap-1">
              <button onClick={() => setTour((st) => (st ? togglePlay(st) : st))} className="flex h-7 cursor-pointer items-center gap-1 rounded-[4px] border border-line2 px-2 text-[0.8em] text-bone hover:bg-hover" aria-pressed={playing} title="Autoplay (space)">{playing ? <Pause size={12} /> : <Play size={12} />}{!mobile && (playing ? 'Pause' : 'Play')}</button>
              {!mobile && <button onClick={() => setTour((st) => (st ? togglePresent(st) : st))} className={cx('flex h-7 cursor-pointer items-center gap-1 rounded-[4px] border px-2 text-[0.8em] hover:bg-hover', present ? 'border-bone text-bone' : 'border-line2 text-mute')} aria-pressed={present} title="Present: larger type for a projector (P)">{present ? <Minimize2 size={12} /> : <Maximize2 size={12} />}Present</button>}
              <button onClick={() => setTour(null)} className="flex h-7 cursor-pointer items-center rounded-[4px] px-1.5 text-dim hover:text-bone" aria-label="Exit walkthrough"><X size={13} /></button>
            </div>
          </div>
          {!mobile && (
            <div className="relative flex items-center gap-1">
              {t.beats.map((b, i) => (
                <button key={b.id} onClick={() => setTour((st) => (st ? goTo({ ...st, playing: false }, i, n) : st))} className="group relative h-2 flex-1 cursor-pointer" aria-label={`Beat ${i + 1}: ${b.title}`} aria-current={i === tour.beat ? 'step' : undefined} title={b.title}>
                  <span className={cx('absolute inset-x-0 top-[3px] h-[2px] rounded-full', i < tour.beat ? 'bg-bone' : i === tour.beat ? 'bg-line2' : 'bg-line')} />
                  {i === tour.beat && <span className="tour-rail-fill absolute left-0 top-[3px] h-[2px] rounded-full" style={{ background: toneVar, width: playing ? (armed && dwellOn ? '100%' : '0%') : '100%', transition: playing && armed && dwellOn ? `width ${beat.duration}ms linear` : 'none' }} />}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
