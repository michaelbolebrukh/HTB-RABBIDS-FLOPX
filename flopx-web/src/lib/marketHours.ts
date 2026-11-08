// NYSE regular hours computed from the clock. Regular session: 14:30 to 21:00 UTC (09:30 to 16:00
// New York) on weekdays; the design's sidebar toggle becomes an override for demos. No holiday
// table: the label says "regular hours" so a holiday reads as a stated simplification, never as a
// claim that the venue is open. Daylight-saving shifts the UTC window by an hour; the label names
// the UTC window it used so the simplification is visible.

export const NYSE_OPEN_UTC = { h: 14, m: 30 }
export const NYSE_CLOSE_UTC = { h: 21, m: 0 }

const mins = (d: Date) => d.getUTCHours() * 60 + d.getUTCMinutes()
const OPEN_MIN = NYSE_OPEN_UTC.h * 60 + NYSE_OPEN_UTC.m
const CLOSE_MIN = NYSE_CLOSE_UTC.h * 60 + NYSE_CLOSE_UTC.m

/** True when `now` falls inside the regular session on a weekday (UTC). */
export function nyseRegularOpen(now: Date = new Date()): boolean {
  const day = now.getUTCDay()
  if (day === 0 || day === 6) return false
  const m = mins(now)
  return m >= OPEN_MIN && m < CLOSE_MIN
}

/** The next open (when closed) or the close (when open), as a UTC Date. */
export function nyseNextChange(now: Date = new Date()): Date {
  const at = (d: Date, h: number, mm: number) => new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), h, mm))
  if (nyseRegularOpen(now)) return at(now, NYSE_CLOSE_UTC.h, NYSE_CLOSE_UTC.m)
  const d = new Date(now)
  const beforeOpenToday = d.getUTCDay() >= 1 && d.getUTCDay() <= 5 && mins(d) < OPEN_MIN
  if (!beforeOpenToday) {
    do d.setUTCDate(d.getUTCDate() + 1)
    while (d.getUTCDay() === 0 || d.getUTCDay() === 6)
  }
  return at(d, NYSE_OPEN_UTC.h, NYSE_OPEN_UTC.m)
}

const hhmm = (d: Date) => `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`
const DAY = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

/** Sub-label for the market state, e.g. "regular hours · closes 21:00 UTC" or "regular hours · opens Mon 14:30 UTC". */
export function nyseStateLabel(open: boolean, now: Date = new Date(), overridden = false): string {
  if (overridden) return open ? 'demo override · opened by hand' : 'demo override · closed by hand'
  const next = nyseNextChange(now)
  const sameDay = next.getUTCDate() === now.getUTCDate() && next.getUTCMonth() === now.getUTCMonth()
  return open ? `regular hours · closes ${hhmm(next)} UTC` : `regular hours · opens ${sameDay ? '' : `${DAY[next.getUTCDay()]} `}${hhmm(next)} UTC`
}
