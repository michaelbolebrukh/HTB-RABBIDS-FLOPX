import { describe, expect, it } from 'vitest'
import { PROJECTS } from '@/data/infra'
import { activePerspective, capPips, DOCUMENTED_WEIGHT, exposureOf, perspectiveValue, POWER_HOT, powerTone, stagesByRegion } from './powerMap'

describe('power map helpers', () => {
  it('marks power above the threshold as expensive, at the threshold as normal', () => {
    expect(powerTone(POWER_HOT + 0.01)).toBe('down')
    expect(powerTone(POWER_HOT)).toBe('bone')
    expect(powerTone(0)).toBe('bone')
  })

  it('groups facilities by region, most built first', () => {
    const m = stagesByRegion([
      { region: 'tx', stage: 'proposed' }, { region: 'tx', stage: 'operational' },
      { region: 'tx', stage: 'announced' }, { region: 'tx', stage: 'construction' },
      { region: 'va', stage: 'construction' },
    ])
    expect(m.get('tx')).toEqual(['operational', 'construction', 'proposed', 'announced'])
    expect(m.get('va')).toEqual(['construction'])
    expect(m.has('none')).toBe(false)
    expect(stagesByRegion([]).size).toBe(0)
  })

  it('caps pips and counts the rest', () => {
    expect(capPips(['operational', 'construction'])).toEqual({ shown: ['operational', 'construction'], more: 0 })
    expect(capPips(['operational', 'operational', 'proposed', 'proposed', 'announced'], 4)).toEqual({ shown: ['operational', 'operational', 'proposed', 'proposed'], more: 1 })
    expect(capPips([], 4)).toEqual({ shown: [], more: 0 })
  })

  it('accounts for every real project exactly once', () => {
    const m = stagesByRegion(PROJECTS)
    expect([...m.values()].reduce((n, s) => n + s.length, 0)).toBe(PROJECTS.length)
  })
})

describe('X-Ray perspectives read live blocks only', () => {
  const live = { rental: null, power: null, supply: null, exposure: null }
  it('latest switched-on layer is active; empty falls back to pressure', () => {
    expect(activePerspective(['pressure', 'rental'])).toBe('rental')
    expect(activePerspective([])).toBe('pressure')
  })
  it('rental: regional median with premium, global fallback says n/a premium, no live block reads n/a', () => {
    expect(perspectiveValue({ documented: [], live: { ...live, rental: { median: 2.4696, premiumPct: -21.1, fallback: false } } }, 'rental')).toMatchObject({ value: '$2.47', note: '-21.1% vs global' })
    expect(perspectiveValue({ documented: [], live: { ...live, rental: { median: 3.1313, premiumPct: null, fallback: true } } }, 'rental')).toMatchObject({ value: '$3.13', note: 'global median, n/a premium' })
    expect(perspectiveValue({ documented: [] }, 'rental').value).toBeNull()
  })
  it('exposure counts documented and authored ties, documented weighted higher, only once loaded', () => {
    const r = { documented: [{ t: 'AAA' }], authored: [{ t: 'BBB' }, { t: 'CCC' }], live: { ...live, exposure: { freshness: 'live' } } }
    expect(exposureOf(r)).toMatchObject({ documented: 1, authored: 2, score: 1 * DOCUMENTED_WEIGHT + 2 })
    expect(perspectiveValue(r, 'exposure').value).toBe('3')
    expect(exposureOf({ ...r, live: undefined })).toBeNull()
  })
  it('power keeps the unit and labels a non-live series as sample', () => {
    expect(perspectiveValue({ documented: [], live: { ...live, power: { value: 88.4, unit: 'EUR/MWh', freshness: 'live' } } }, 'power')).toMatchObject({ value: '€88', note: 'live day-ahead' })
    expect(perspectiveValue({ documented: [], live: { ...live, power: { value: 41, unit: 'USD/MWh', freshness: 'illustrative' } } }, 'power').note).toBe('sample')
  })
})
