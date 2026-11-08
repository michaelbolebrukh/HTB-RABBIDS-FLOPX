// The ticker strip's GPU index change: the Overview cards' 30-day figure and label when the index
// carries one (GpuLive.chg30), the index's own since-first-point change otherwise.
import { describe, expect, it } from 'vitest'
import { gpuTickerChange } from './Shell'

const base = { chgPct: 0, chgWindow: 'since 08 Nov 00:09' }

describe('gpuTickerChange', () => {
  it("shows GetDeploying's 30-day change with its label when the index carries chg30", () => {
    const r = gpuTickerChange({ ...base, chg30: { pct: 15.95, source: 'getdeploying', label: '30d · GetDeploying', from: '09 Oct', to: '08 Nov', spanDays: 30 } })
    expect(r).toEqual({ d: 15.95, note: '30d · GetDeploying', window: '30d · GetDeploying (09 Oct to 08 Nov)' })
  })
  it("keeps FLOPX's own 30-day change and label when that is what the card shows", () => {
    const r = gpuTickerChange({ ...base, chg30: { pct: -2.1, source: 'flopx', label: '30d · FLOPX index', from: '09 Oct', to: '08 Nov', spanDays: 30 } })
    expect(r.d).toBe(-2.1)
    expect(r.note).toBe('30d · FLOPX index')
  })
  it("falls back to today's since-first-point change, with no label, when chg30 is null or absent", () => {
    expect(gpuTickerChange({ ...base, chgPct: 0.4, chg30: null })).toEqual({ d: 0.4, window: 'since 08 Nov 00:09' })
    expect(gpuTickerChange({ ...base, chgPct: null })).toEqual({ d: null, window: 'since 08 Nov 00:09' })
  })
})
