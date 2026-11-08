import { afterEach, describe, expect, it } from 'vitest'
import { PRESSURE_BANDS, PRESSURE_BAND_WORDS, pressureWord, setPressureBands } from './model'

const DEFAULT = PRESSURE_BANDS.map((b) => ({ ...b }))

describe('pressureWord', () => {
  afterEach(() => {
    PRESSURE_BANDS.length = 0
    PRESSURE_BANDS.push(...DEFAULT.map((b) => ({ ...b })))
  })
  it('uses the backend vocabulary and thresholds (slack < 25, balanced 25 to 50, tight 50 to 75, acute >= 75)', () => {
    expect(pressureWord(0)).toBe('Slack')
    expect(pressureWord(24.9)).toBe('Slack')
    expect(pressureWord(25)).toBe('Balanced')
    // the audit's case: SG 48 read "Loose" on the map and "balanced" in the brief
    expect(pressureWord(48)).toBe('Balanced')
    expect(pressureWord(50)).toBe('Tight')
    expect(pressureWord(74.9)).toBe('Tight')
    expect(pressureWord(75)).toBe('Acute')
    expect(pressureWord(100)).toBe('Acute')
    expect(pressureWord(null)).toBe('No estimate')
  })
  it('lists the legend words low to high', () => {
    expect(PRESSURE_BAND_WORDS()).toEqual(['Slack', 'Balanced', 'Tight', 'Acute'])
  })
  it('parses the backend bands object', () => {
    expect(setPressureBands({ slack: '< 30', balanced: '30 to 60', tight: '60 to 80', acute: '>= 80' })).toBe(true)
    expect(pressureWord(55)).toBe('Balanced')
    expect(pressureWord(61)).toBe('Tight')
    expect(setPressureBands({ odd: 'n/a' })).toBe(false)
  })
})
