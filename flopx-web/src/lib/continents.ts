import type { Region } from '@/data/core'

export type Continent = 'North America' | 'South America' | 'Europe' | 'Middle East' | 'Africa' | 'Asia' | 'Oceania'

/** Continent of a region, from its marketplace zone (country decides inside the mixed MEA zone). */
export function continentOf(r: Pick<Region, 'marketplace' | 'country'>): Continent {
  const m = r.marketplace
  if (m.startsWith('US-') || m.startsWith('CA-')) return 'North America'
  if (m === 'LATAM') return 'South America'
  if (m.startsWith('EU-')) return 'Europe'
  if (m === 'APAC-OC') return 'Oceania'
  if (m.startsWith('APAC-')) return 'Asia'
  if (m === 'MEA') return r.country === 'South Africa' ? 'Africa' : 'Middle East'
  return 'Asia'
}

/** The region with the largest ring in each continent, in first-seen continent order. Ties keep the first region. */
export function continentLeaders<R extends Pick<Region, 'id' | 'marketplace' | 'country'>>(regions: R[], size: (r: R) => number | null): { continent: Continent; region: R }[] {
  const best = new Map<Continent, { region: R; s: number }>()
  for (const r of regions) {
    const s = size(r)
    if (s == null || !Number.isFinite(s)) continue
    const c = continentOf(r)
    const cur = best.get(c)
    if (!cur || s > cur.s) best.set(c, { region: r, s })
  }
  return [...best].map(([continent, { region }]) => ({ continent, region }))
}
