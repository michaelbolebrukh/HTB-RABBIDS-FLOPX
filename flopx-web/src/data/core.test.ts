// WF5 #5 and #10: horizon labels follow the month axis (no hard-coded "Mar 27"), and the design's
// placeholder evidence stamps sit on the last completed trading close, never a clock time later today.
import { describe, expect, it } from 'vitest'
import { axisLabels, AXIS_NOW_IDX } from '@/live/dates'
import { lastTradingClose } from '@/lib/designDates'
import { designStamp, horizonLabel, MONTHS, NOW_IDX } from './core'

describe('horizonLabel', () => {
  it('reads the axis slot `months` ahead of now, clamped to the axis', () => {
    expect(horizonLabel(12)).toBe(MONTHS[NOW_IDX + 12])
    expect(horizonLabel(6)).toBe(MONTHS[NOW_IDX + 6])
    expect(horizonLabel(0)).toBe(MONTHS[NOW_IDX])
    expect(horizonLabel(1000)).toBe(MONTHS[MONTHS.length - 1])
    expect(horizonLabel(-1000)).toBe(MONTHS[0])
    expect(horizonLabel(12)).toMatch(/^[A-Z][a-z]{2} \d{2}$/)
  })
  it('is Nov 27 on a November 2026 anchor, where the design said Mar 27', () => {
    const labels = axisLabels(new Date('2026-11-08T05:00:00Z'))
    expect(labels[AXIS_NOW_IDX + 12]).toBe('Nov 27')
    expect(labels[AXIS_NOW_IDX + 6]).toBe('May 27')
    expect(labels.some((l) => l === 'Mar 27')).toBe(true) // the slot exists; it is simply not +12
  })
})

describe('designStamp', () => {
  it('is the last completed trading close at 21:00, never later than now', () => {
    const close = lastTradingClose()
    expect(designStamp()).toMatch(/^\d{2} [A-Z][a-z]{2} 21:00$/)
    expect(close.getTime()).toBeLessThan(Date.now())
    expect([1, 2, 3, 4, 5]).toContain(close.getUTCDay())
  })
})
