import { describe, expect, it } from 'vitest'
import { fmtAsOf, fmtObserved, freshOf, isObserved } from './provenance'

describe('freshOf', () => {
  it('never maps backend sample data to live', () => {
    expect(freshOf({ kind: 'sample' }).freshness).toBe('illustrative')
    expect(freshOf({ kind: 'derived', inputKinds: ['live', 'sample'] }).freshness).toBe('illustrative')
    expect(isObserved({ kind: 'sample' })).toBe(false)
  })
  it('maps live, archived and derived-from-live', () => {
    expect(freshOf({ kind: 'live' })).toEqual({ freshness: 'live' })
    expect(freshOf({ kind: 'archived' }).freshness).toBe('historical')
    expect(freshOf({ kind: 'derived', inputKinds: ['live'] })).toEqual({ freshness: 'live', label: 'Live (derived)' })
    expect(freshOf({ kind: 'derived', inputKinds: [] }).freshness).toBe('historical')
    expect(freshOf({ kind: 'derived', inputKinds: ['archived', 'live'] }).freshness).toBe('historical')
  })
  it('treats a missing provenance as illustrative', () => {
    expect(freshOf(undefined).freshness).toBe('illustrative')
  })
})

describe('formatting', () => {
  it('formats AS_OF and per-offer observed times in UTC', () => {
    expect(fmtAsOf('2026-11-08T00:39:13.275725Z')).toBe('08 Nov 2026 00:39 UTC')
    expect(fmtObserved('2026-11-08T00:39:13Z')).toBe('08 Nov 00:39')
    expect(fmtObserved(null)).toBe('n/a')
  })
})
