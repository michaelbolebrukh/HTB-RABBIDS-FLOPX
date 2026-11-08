import { describe, expect, it } from 'vitest'
import { energyStripCounts, stripKind, type StripRow } from './energyStrip'

const row = (id: string, freshness: StripRow['freshness'], priceNow: number, real?: boolean[]): StripRow => ({ id, name: id.toUpperCase(), priceNow, currency: 'USD', freshness, real })

describe('energyStrip', () => {
  it('classifies observed, backend-sample and design-only rows', () => {
    expect(stripKind(row('de', 'historical', 89, [true]))).toBe('real')
    expect(stripKind(row('ercot', 'illustrative', 35, [true]))).toBe('sample')
    expect(stripKind(row('pjm', 'illustrative', 71))).toBe('none')
  })
  it('counts the audit state: 1 real (DE-LU), 1 sample (ERCOT), 11 with no series', () => {
    const rows = [row('de', 'historical', 89, [true]), row('ercot', 'illustrative', 35, [true]), ...Array.from({ length: 11 }, (_, i) => row(`m${i}`, 'illustrative', 50 + i))]
    const c = energyStripCounts(rows)
    expect([c.real, c.sample, c.none, c.total]).toEqual([1, 1, 11, 13])
    expect(c.label).toBe('1 real zone (DE) · 1 sample (ERCOT) · 11 no series')
    // the price range runs over the rows with a backend series (real or sample), never over design-only rows
    expect(c.cheapest?.id).toBe('ercot')
    expect(c.dearest?.id).toBe('de')
  })
  it('is null with no series at all, and strips the design name parenthetical in the label', () => {
    expect(energyStripCounts([row('x', 'illustrative', 50)]).cheapest).toBeNull()
    expect(energyStripCounts([{ ...row('de', 'historical', 89, [true]), name: 'DE (EPEX)' }]).label).toBe('1 real zone (DE) · 0 sample · 0 no series')
  })
})
