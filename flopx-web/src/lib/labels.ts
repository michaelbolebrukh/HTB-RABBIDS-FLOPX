/** Axis-aligned box in SVG pixels. */
export interface Box { x: number; y: number; w: number; h: number }

const overlaps = (a: Box, b: Box, pad: number) =>
  a.x < b.x + b.w + pad && b.x < a.x + a.w + pad && a.y < b.y + b.h + pad && b.y < a.y + a.h + pad

const inside = (a: Box, w: number, h: number) => a.x >= 2 && a.y >= 2 && a.x + a.w <= w - 2 && a.y + a.h <= h - 2

/**
 * Greedy label placement: returns the first candidate that stays inside the
 * w×h frame and clears every box already in `placed` (and records it there),
 * or null when none fits. Callers place labels in priority order.
 */
export function place(placed: Box[], candidates: Box[], w: number, h: number, pad = 2): Box | null {
  const hit = candidates.find((c) => inside(c, w, h) && !placed.some((p) => overlaps(c, p, pad)))
  if (hit) placed.push(hit)
  return hit ?? null
}

/** Approximate rendered width of monospace text (advance ≈ 0.62em plus letter spacing). */
export const monoWidth = (chars: number, fontSize: number, tracking = 0) => chars * fontSize * (0.62 + tracking)
