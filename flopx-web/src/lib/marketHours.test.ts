import { describe, expect, it } from 'vitest'
import { nyseNextChange, nyseRegularOpen, nyseStateLabel } from './marketHours'

const at = (iso: string) => new Date(iso)

describe('nyseRegularOpen', () => {
  it('is closed on a Sunday night (the audit capture: 08 Nov 2026 02:51 UTC)', () => {
    expect(nyseRegularOpen(at('2026-11-08T02:51:00Z'))).toBe(false)
  })
  it('is open on a weekday inside 14:30 to 21:00 UTC', () => {
    expect(nyseRegularOpen(at('2026-11-09T14:30:00Z'))).toBe(true)
    expect(nyseRegularOpen(at('2026-11-09T20:59:00Z'))).toBe(true)
  })
  it('is closed at the boundaries and on Saturday', () => {
    expect(nyseRegularOpen(at('2026-11-09T14:29:00Z'))).toBe(false)
    expect(nyseRegularOpen(at('2026-11-09T21:00:00Z'))).toBe(false)
    expect(nyseRegularOpen(at('2026-11-07T16:00:00Z'))).toBe(false)
  })
})

describe('nyseNextChange', () => {
  it('points at the close while open', () => {
    expect(nyseNextChange(at('2026-11-09T15:00:00Z')).toISOString()).toBe('2026-11-09T21:00:00.000Z')
  })
  it('points at Monday 14:30 from a Sunday night, and at today 14:30 before the open', () => {
    expect(nyseNextChange(at('2026-11-08T02:51:00Z')).toISOString()).toBe('2026-11-09T14:30:00.000Z')
    expect(nyseNextChange(at('2026-11-09T09:00:00Z')).toISOString()).toBe('2026-11-09T14:30:00.000Z')
  })
  it('skips the weekend after a Friday close', () => {
    expect(nyseNextChange(at('2026-11-13T22:00:00Z')).toISOString()).toBe('2026-11-16T14:30:00.000Z')
  })
})

describe('nyseStateLabel', () => {
  it('names regular hours and the next change', () => {
    expect(nyseStateLabel(false, at('2026-11-08T02:51:00Z'))).toBe('regular hours · opens Mon 14:30 UTC')
    expect(nyseStateLabel(true, at('2026-11-09T15:00:00Z'))).toBe('regular hours · closes 21:00 UTC')
  })
  it('says so when the demo toggle overrides the clock', () => {
    expect(nyseStateLabel(true, at('2026-11-08T02:51:00Z'), true)).toContain('demo override')
  })
})
