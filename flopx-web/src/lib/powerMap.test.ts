import { describe, expect, it } from 'vitest'
import { PROJECTS } from '@/data/infra'
import { capPips, POWER_HOT, powerTone, stagesByRegion } from './powerMap'

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
