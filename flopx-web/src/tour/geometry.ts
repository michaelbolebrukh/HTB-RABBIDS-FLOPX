// Spotlight geometry for the presenter stage: pure functions over rectangles, so the placement of
// the cut-out, the card and the connector can be tested without a DOM.
//
// The one rule placement must never break: the card never covers the lit element. It may sit over
// the dimmed page, so the candidates are the four free bands around the spotlight (right, left,
// below, above, in the beat's preferred order), then the largest band the card can shrink into,
// then, for a tall target, the spotlight is clipped from below so a band opens up under it.

export interface Rect {
  x: number
  y: number
  w: number
  h: number
}

/** 'sheet' is the mobile bottom sheet; 'over' is the last resort when no band is usable. */
export type Side = 'right' | 'left' | 'below' | 'above' | 'centre' | 'sheet' | 'over'

export interface Placement {
  /** The cut-out around the target (padded, clamped to the area above the rail). */
  spotlight: Rect
  /** Where the card sits, at the size it may take (shrunk when `shrunk`). */
  card: Rect
  side: Side
  /** Connector from the card's edge to the spotlight's edge; null when the card is centred (no target). */
  connector: { from: { x: number; y: number }; to: { x: number; y: number } } | null
  /** The card had to shrink below its natural size to stay off the spotlight (its body scrolls). */
  shrunk: boolean
  /** The spotlight was clipped from below (a tall target) so the card could sit under the lit part. */
  clipped: boolean
  /** No free band was usable even after shrinking and clipping: the card overlaps the spotlight. */
  overlaps: boolean
}

export interface PlaceOpts {
  viewport: { w: number; h: number }
  card: { w: number; h: number }
  /** Padding around the target inside the cut-out. */
  pad?: number
  /** Gap between the cut-out and the card. */
  gap?: number
  /** Keep the card this far from the viewport edge. */
  margin?: number
  /** Height reserved at the bottom for the progress rail (the cut-out never runs under it). */
  railH?: number
  prefer?: Side[]
  /** Smallest card worth placing in a band before clipping the spotlight instead. */
  minCard?: { w: number; h: number }
}

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v))
const DEFAULT_ORDER: Side[] = ['right', 'left', 'below', 'above']
/** Keep at least this much of a clipped target lit. */
const MIN_LIT_H = 120

/**
 * The band to keep clear at the bottom of the viewport, from the rail's measured top: the rail is
 * taller than its nominal height once its bottom padding is counted (60 px of rail plus 12 px of
 * padding at 1440, so its top is 828 and not 836; WF5 #3). Falls back to `nominal` until it is measured.
 */
export function railReserve(viewportH: number, railTop: number | null | undefined, nominal: number): number {
  if (railTop == null || !Number.isFinite(railTop) || railTop <= 0 || railTop >= viewportH) return nominal
  return Math.max(nominal, Math.ceil(viewportH - railTop))
}

/** Pad a rect and clamp it to the viewport (and above `maxY`, the rail's top); a degenerate rect stays degenerate. */
export function spotlightRect(target: Rect, viewport: { w: number; h: number }, pad = 8, maxY = viewport.h): Rect {
  const x = clamp(target.x - pad, 0, viewport.w)
  const y = clamp(target.y - pad, 0, maxY)
  const x2 = clamp(target.x + target.w + pad, 0, viewport.w)
  const y2 = clamp(target.y + target.h + pad, 0, maxY)
  return { x, y, w: Math.max(0, x2 - x), h: Math.max(0, y2 - y) }
}

/** Overlap area of two rects (0 when they do not touch). */
export function intersection(a: Rect, b: Rect): number {
  const ix = Math.max(0, Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x))
  const iy = Math.max(0, Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y))
  return ix * iy
}

/** Centre the card in the usable area (no target to anchor to). */
export function centredCard(opts: PlaceOpts): Rect {
  const railH = opts.railH ?? 0
  const margin = opts.margin ?? 16
  const w = Math.min(opts.card.w, opts.viewport.w - 2 * margin)
  const h = Math.min(opts.card.h, opts.viewport.h - railH - 2 * margin)
  return { x: (opts.viewport.w - w) / 2, y: (opts.viewport.h - railH - h) / 2, w, h }
}

/** The free band on one side of the spotlight inside the usable area (may have a zero or negative size). */
function bandFor(side: Side, s: Rect, usable: Rect, gap: number): Rect {
  switch (side) {
    case 'right': return { x: s.x + s.w + gap, y: usable.y, w: usable.x + usable.w - (s.x + s.w + gap), h: usable.h }
    case 'left': return { x: usable.x, y: usable.y, w: s.x - gap - usable.x, h: usable.h }
    case 'below': return { x: usable.x, y: s.y + s.h + gap, w: usable.w, h: usable.y + usable.h - (s.y + s.h + gap) }
    default: return { x: usable.x, y: usable.y, w: usable.w, h: s.y - gap - usable.y }
  }
}

/** Put a w x h card in a band, hugging the spotlight and aligned with its centre along the free axis. */
function inBand(side: Side, band: Rect, s: Rect, w: number, h: number): Rect {
  if (side === 'right' || side === 'left') {
    const x = side === 'right' ? band.x : band.x + band.w - w
    return { x, y: clamp(s.y + s.h / 2 - h / 2, band.y, Math.max(band.y, band.y + band.h - h)), w, h }
  }
  const y = side === 'below' ? band.y : band.y + band.h - h
  return { x: clamp(s.x + s.w / 2 - w / 2, band.x, Math.max(band.x, band.x + band.w - w)), y, w, h }
}

/**
 * Place the card beside the spotlight without ever covering it. Bands are tried in preference
 * order at the card's natural size; failing that the card shrinks into the largest band that is
 * at least `minCard`; failing that a tall spotlight is clipped from below so a band opens under
 * the lit part; only when even that is impossible does the card overlap (`overlaps`).
 */
export function placeCard(target: Rect | null, opts: PlaceOpts): Placement {
  const pad = opts.pad ?? 8
  const gap = opts.gap ?? 18
  const margin = opts.margin ?? 16
  const railH = opts.railH ?? 0
  const minCard = opts.minCard ?? { w: 300, h: 180 }
  const usable: Rect = { x: margin, y: margin, w: opts.viewport.w - 2 * margin, h: opts.viewport.h - railH - 2 * margin }
  const none = { shrunk: false, clipped: false, overlaps: false }
  if (!target || target.w <= 0 || target.h <= 0) {
    return { spotlight: { x: 0, y: 0, w: 0, h: 0 }, card: centredCard(opts), side: 'centre', connector: null, ...none }
  }
  let s = spotlightRect(target, opts.viewport, pad, opts.viewport.h - railH)
  const w = Math.min(opts.card.w, usable.w)
  const h = Math.min(opts.card.h, usable.h)
  const order: Side[] = [...(opts.prefer ?? []), ...DEFAULT_ORDER].filter((x, i, a) => DEFAULT_ORDER.includes(x) && a.indexOf(x) === i)
  const bands = () => order.map((side) => ({ side, band: bandFor(side, s, usable, gap) }))

  // 1. A band that takes the card at its natural size.
  for (const { side, band } of bands()) {
    if (band.w >= w && band.h >= h) {
      const card = inBand(side, band, s, w, h)
      return { spotlight: s, card, side, connector: connectorFor(card, s, side), ...none }
    }
  }
  // 2. The largest usable band, with the card shrunk to fit it.
  // A band that takes the card's natural height wins over a larger one that would make its body
  // scroll (N-TOUR2 #1): width shrinks before height does. Among equals, the largest area wins.
  const fit = (list: { side: Side; band: Rect }[]) => {
    let best: { side: Side; band: Rect; area: number; fitsH: boolean } | null = null
    for (const { side, band } of list) {
      if (band.w < minCard.w || band.h < minCard.h) continue
      const area = Math.min(band.w, w) * Math.min(band.h, h)
      const fitsH = band.h >= h
      if (!best || (fitsH && !best.fitsH) || (fitsH === best.fitsH && area > best.area)) best = { side, band, area, fitsH }
    }
    return best
  }
  const b2 = fit(bands())
  const place2 = () => {
    if (!b2) return null
    const cw = Math.min(w, b2.band.w)
    const ch = Math.min(h, b2.band.h)
    const card = inBand(b2.side, b2.band, s, cw, ch)
    return { spotlight: s, card, side: b2.side, connector: connectorFor(card, s, b2.side), shrunk: cw < w || ch < h, clipped: false, overlaps: false } as Placement
  }
  const p2 = place2()
  if (p2 && !p2.shrunk) return p2
  // 3. A tall target: light its top part only, so a band opens below it for the card (up to 60% of the
  //    usable height, so a dense card at 1440 x 900 keeps its natural height rather than scrolling). The
  //    card may then sit over the dimmed lower part of the target, never over the lit part. Taken before a
  //    shrunk band whenever it gives the card its natural height; otherwise the shrunk band stands (N-TOUR2 #1).
  const wantH = Math.min(h, Math.round(usable.h * 0.6))
  const newBottom = usable.y + usable.h - gap - Math.max(minCard.h, wantH)
  if (newBottom - s.y >= MIN_LIT_H && newBottom < s.y + s.h) {
    const s3 = { ...s, h: newBottom - s.y }
    const band = bandFor('below', s3, usable, gap)
    if (band.w >= minCard.w && band.h >= minCard.h) {
      const cw = Math.min(w, band.w)
      const ch = Math.min(h, band.h)
      const card = inBand('below', band, s3, cw, ch)
      const p3: Placement = { spotlight: s3, card, side: 'below', connector: connectorFor(card, s3, 'below'), shrunk: cw < w || ch < h, clipped: true, overlaps: false }
      if (!p2 || (!p3.shrunk && p2.shrunk) || (p3.shrunk === p2.shrunk && p3.card.h > p2.card.h)) { s = s3; return p3 }
    }
  }
  if (p2) return p2
  // 4. Nothing is free: the card sits over the page, as low as the usable area allows.
  const card = { x: clamp(opts.viewport.w / 2 - w / 2, usable.x, Math.max(usable.x, usable.x + usable.w - w)), y: Math.max(usable.y, usable.y + usable.h - h), w, h }
  return { spotlight: s, card, side: 'over', connector: null, shrunk: false, clipped: false, overlaps: intersection(card, s) > 0 }
}

/** The mobile layout: a full-width bottom sheet above the rail; the cut-out is clamped to the area above the sheet. */
export function placeSheet(target: Rect | null, opts: PlaceOpts & { sheetH: number }): Placement {
  const pad = opts.pad ?? 8
  const railH = opts.railH ?? 0
  const top = opts.viewport.h - railH - opts.sheetH
  const card = { x: 0, y: top, w: opts.viewport.w, h: opts.sheetH }
  const none = { shrunk: false, clipped: false, overlaps: false }
  if (!target || target.w <= 0 || target.h <= 0) return { spotlight: { x: 0, y: 0, w: 0, h: 0 }, card, side: 'centre', connector: null, ...none }
  const s = spotlightRect(target, opts.viewport, pad, top - 8)
  return { spotlight: s, card, side: 'sheet', connector: null, ...none, clipped: target.y + target.h + pad > top - 8 }
}

/** Connector end-points: from the card's facing edge midpoint to the nearest point on the spotlight edge. */
export function connectorFor(card: Rect, s: Rect, side: Side): Placement['connector'] {
  if (side === 'centre' || side === 'sheet' || side === 'over') return null
  const from =
    side === 'right' ? { x: card.x, y: card.y + Math.min(card.h / 2, 48) }
    : side === 'left' ? { x: card.x + card.w, y: card.y + Math.min(card.h / 2, 48) }
    : side === 'below' ? { x: card.x + card.w / 2, y: card.y }
    : { x: card.x + card.w / 2, y: card.y + card.h }
  const to = { x: clamp(from.x, s.x, s.x + s.w), y: clamp(from.y, s.y, s.y + s.h) }
  return { from, to }
}

/** The six-beat spine's progress, 0..1, for a rail of n beats at index i. */
export function railProgress(i: number, n: number): number {
  if (n <= 1) return 1
  return clamp(i / (n - 1), 0, 1)
}
