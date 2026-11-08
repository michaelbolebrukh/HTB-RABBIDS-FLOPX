import { describe, expect, it } from 'vitest'
import { REGIONS } from '@/data/core'
import { continentLeaders, continentOf } from './continents'

const r = (id: string, marketplace: string, country = 'X') => ({ id, marketplace, country })

describe('continentOf', () => {
  it('maps marketplace zones to continents', () => {
    expect(continentOf(r('a', 'US-Central'))).toBe('North America')
    expect(continentOf(r('a', 'CA-East'))).toBe('North America')
    expect(continentOf(r('a', 'EU-North'))).toBe('Europe')
    expect(continentOf(r('a', 'APAC-SE'))).toBe('Asia')
    expect(continentOf(r('a', 'APAC-OC'))).toBe('Oceania')
    expect(continentOf(r('a', 'LATAM'))).toBe('South America')
  })
  it('splits MEA by country', () => {
    expect(continentOf(r('a', 'MEA', 'UAE'))).toBe('Middle East')
    expect(continentOf(r('a', 'MEA', 'South Africa'))).toBe('Africa')
  })
  it('covers every real region', () => {
    const cs = new Set(REGIONS.map(continentOf))
    expect(cs).toEqual(new Set(['North America', 'South America', 'Europe', 'Middle East', 'Africa', 'Asia', 'Oceania']))
  })
})

describe('continentLeaders', () => {
  const regions = [r('tx', 'US-Central'), r('va', 'US-East'), r('fra', 'EU-Central'), r('dub', 'EU-West'), r('sg', 'APAC-SE')]
  const size: Record<string, number | null> = { tx: 5, va: 9, fra: 3, dub: 3, sg: null }

  it('picks the largest region per continent', () => {
    const out = continentLeaders(regions, (x) => size[x.id])
    expect(out.map((o) => [o.continent, o.region.id])).toEqual([['North America', 'va'], ['Europe', 'fra']])
  })
  it('keeps the first region on ties and skips null sizes', () => {
    const out = continentLeaders(regions, (x) => size[x.id])
    expect(out.find((o) => o.continent === 'Europe')!.region.id).toBe('fra')
    expect(out.find((o) => o.continent === 'Asia')).toBeUndefined()
  })
  it('returns nothing for empty input', () => {
    expect(continentLeaders([], () => 1)).toEqual([])
  })
})
