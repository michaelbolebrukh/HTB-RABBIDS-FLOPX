// The design's worked examples (paper orders, target changes, decision records) were authored
// with "now" on Sat 14 Mar 2026 and carry March dates. Those rows are illustrative, so they keep
// their distance from "now" and slide onto the real anchor (the backend's generated time, or the
// client clock before it lands) instead of predating the paper account. Trading rows step back in
// weekdays so a fill never lands on a weekend.
import { axisAnchor } from '@/live/dates'

const DAY_MS = 86400000
const utcMidnight = (d: Date) => new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()))

/** `n` calendar days before (negative: after) the anchor, at UTC midnight. */
export function daysAgo(n: number, now: Date = axisAnchor()): Date {
  return new Date(utcMidnight(now).getTime() - n * DAY_MS)
}

/** `n` weekdays before the anchor (0 = the anchor's own day, or the Friday before it on a weekend). */
export function weekdaysAgo(n: number, now: Date = axisAnchor()): Date {
  const d = utcMidnight(now)
  const back = () => d.setUTCDate(d.getUTCDate() - 1)
  while (d.getUTCDay() === 0 || d.getUTCDay() === 6) back()
  for (let k = 0; k < n; k++) {
    back()
    while (d.getUTCDay() === 0 || d.getUTCDay() === 6) back()
  }
  return d
}

/**
 * The last completed US trading close (weekday 21:00 UTC) strictly before `now`: on a Sunday
 * morning that is Friday 21:00. A design example stamped "updated <date> 08:00 UTC" must never sit
 * ahead of the clock (WF4 #12 found one four hours in the future), so design stamps anchor here.
 */
export function lastTradingClose(now: Date = axisAnchor()): Date {
  const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), 21, 0, 0))
  const weekend = () => d.getUTCDay() === 0 || d.getUTCDay() === 6
  if (d.getTime() >= now.getTime()) d.setUTCDate(d.getUTCDate() - 1)
  while (weekend()) d.setUTCDate(d.getUTCDate() - 1)
  return d
}

/** "07 Nov" */
export const fmtDM = (d: Date) => d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', timeZone: 'UTC' })
/** "07 Nov 2026" */
export const fmtDMY = (d: Date) => d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric', timeZone: 'UTC' })
/** "07 Nov 15:58": the design's per-order stamp, with a fixed illustrative clock time. */
export const fmtStampAt = (d: Date, hhmm: string) => `${fmtDM(d)} ${hhmm}`
