import { describe, expect, it } from 'vitest'
import { evalPriceBasis } from './evalBasis'

describe('evalPriceBasis', () => {
  it('reads sample closes as illustrative', () => {
    const b = evalPriceBasis({ sources: ['flopx-sample-v1'], sourceDetails: [] })
    expect(b.kind).toBe('sample')
    expect(b.freshness).toBe('illustrative')
    expect(b.label).toBe('Illustrative · sample closes')
  })
  it('reads the live backend state (wrds-crsp, archived through 2025-12-31) as archived with the date', () => {
    const b = evalPriceBasis({ sources: ['wrds-crsp'], oldestObservedAt: '2025-12-31T21:00:00Z', sourceDetails: [{ source: 'wrds-crsp', kind: 'archived', observedAt: '2025-12-31T21:00:00Z' }] })
    expect(b.kind).toBe('archived')
    expect(b.freshness).toBe('historical')
    expect(b.label).toBe('Archived · WRDS CRSP closes through 2025-12-31')
    expect(b.caption).toBe('scored on WRDS CRSP closes through 2025-12-31')
  })
  it('reads what the backend serves tonight (alpaca-data, archived through 2026-11-06) and exposes source and through for the tour', () => {
    const b = evalPriceBasis({ sources: ['alpaca-data', 'flopx-sample-v1'], oldestObservedAt: '2026-10-30T21:00:00Z', sourceDetails: [{ source: 'alpaca-data', kind: 'archived', observedAt: '2026-11-06T21:00:00Z' }, { source: 'flopx-sample-v1', kind: 'sample' }] })
    expect(b.kind).toBe('archived')
    expect(b.source).toBe('alpaca-data')
    expect(b.through).toBe('2026-11-06')
    expect(b.label).toBe('Archived · alpaca-data closes through 2026-11-06')
    expect(b.short).toBe('alpaca-data closes')
    expect(evalPriceBasis({ sources: ['flopx-sample-v1'] })).toMatchObject({ source: 'sample', through: null })
  })
  it('names a sample fill-in when both appear, and falls back to illustrative with no freshness block', () => {
    const b = evalPriceBasis({ sources: ['wrds-crsp', 'flopx-sample-v1'], sourceDetails: [{ source: 'wrds-crsp', kind: 'archived', observedAt: '2025-12-31T21:00:00Z' }] })
    expect(b.caption).toContain('sample closes where the archive has none')
    expect(evalPriceBasis(undefined).kind).toBe('sample')
  })
})
