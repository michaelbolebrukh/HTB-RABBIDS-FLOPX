import { describe, expect, it } from 'vitest'
import { daysAgo, fmtDM, fmtDMY, fmtStampAt, lastTradingClose, weekdaysAgo } from './designDates'

const sun = new Date('2026-11-08T02:51:00Z')

describe('designDates', () => {
  it('counts calendar days back from the anchor at UTC midnight', () => {
    expect(daysAgo(1, sun).toISOString()).toBe('2026-11-07T00:00:00.000Z')
    expect(daysAgo(-30, sun).toISOString()).toBe('2026-12-08T00:00:00.000Z')
  })
  it('steps back in weekdays, skipping weekends, and lands a Sunday anchor on Friday first', () => {
    expect(fmtDM(weekdaysAgo(0, sun))).toBe('06 Nov')
    expect(fmtDM(weekdaysAgo(1, sun))).toBe('05 Nov')
    expect(fmtDM(weekdaysAgo(6, sun))).toBe('29 Oct')
    const wed = new Date('2026-11-11T12:00:00Z')
    expect(fmtDM(weekdaysAgo(0, wed))).toBe('11 Nov')
    expect(fmtDM(weekdaysAgo(3, wed))).toBe('06 Nov')
  })
  it('never lands on a weekend', () => {
    for (let n = 0; n < 15; n++) expect([0, 6]).not.toContain(weekdaysAgo(n, sun).getUTCDay())
  })
  it('finds the last completed trading close strictly before now (WF4 #12: never a stamp in the future)', () => {
    expect(lastTradingClose(sun).toISOString()).toBe('2026-11-06T21:00:00.000Z') // Sunday 02:51 -> Friday close
    expect(lastTradingClose(new Date('2026-11-08T04:10:00Z')).toISOString()).toBe('2026-11-06T21:00:00.000Z') // the WF4 capture minute
    expect(lastTradingClose(new Date('2026-11-09T10:00:00Z')).toISOString()).toBe('2026-11-06T21:00:00.000Z') // Monday morning, before the close
    expect(lastTradingClose(new Date('2026-11-09T22:00:00Z')).toISOString()).toBe('2026-11-09T21:00:00.000Z') // Monday evening, after the close
    expect(lastTradingClose(new Date('2026-11-09T21:00:00Z')).toISOString()).toBe('2026-11-06T21:00:00.000Z') // exactly at the close: not yet completed
    expect(lastTradingClose(new Date('2026-11-11T12:00:00Z')).toISOString()).toBe('2026-11-10T21:00:00.000Z')
    for (let k = 0; k < 14; k++) {
      const now = new Date(Date.UTC(2026, 10, 1 + k, 7, 30))
      const c = lastTradingClose(now)
      expect(c.getTime()).toBeLessThan(now.getTime())
      expect([0, 6]).not.toContain(c.getUTCDay())
    }
  })
  it('formats in the design wording', () => {
    expect(fmtDMY(daysAgo(0, sun))).toBe('08 Nov 2026')
    expect(fmtStampAt(daysAgo(1, sun), '15:58')).toBe('07 Nov 15:58')
  })
})
