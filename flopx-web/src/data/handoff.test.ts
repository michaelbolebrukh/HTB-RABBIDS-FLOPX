import { describe, expect, it } from 'vitest'
import { AGENT_TOOLS, CLAIMS, derived, GPU_FV, PROCUREMENT, TOKENS } from './handoff'

describe('hand-off snapshot', () => {
  it('reproduces the headline ratios quoted in the hand-off', () => {
    expect(derived.providerRatio).toBeCloseTo(6.8, 1)
    expect(derived.energizedShare).toBeCloseTo(0.39, 2)
    expect(derived.procurementSaving * 100).toBeCloseTo(10.8, 1)
    expect(derived.sameGpuPremium * 100).toBeCloseTo(52, 0)
    expect(derived.regionLogPts).toBeCloseTo(22.2, 6)
    expect(derived.brevPremium * 100).toBeCloseTo(22, 0)
  })

  it('agrees with the 41.6 log-point gap between the two H200 NVL asks', () => {
    const { low, high } = GPU_FV.example
    expect(Math.log(high.price / low.price) * 100).toBeCloseTo(GPU_FV.example.gapLogPts, 0)
  })

  it('has exactly one recommended, feasible procurement plan, and it is the cheapest feasible one', () => {
    const rec = PROCUREMENT.plans.filter((p) => p.recommended)
    expect(rec).toHaveLength(1)
    const feasible = PROCUREMENT.plans.filter((p) => p.feasible)
    expect(Math.min(...feasible.map((p) => p.cost))).toBe(rec[0].cost)
  })

  it('keeps lab rows internally consistent', () => {
    for (const l of TOKENS.labs) {
      expect(l.cheapest.price).toBeLessThanOrEqual(l.median)
      expect(l.median).toBeLessThanOrEqual(l.dearest.price)
    }
  })

  it('lists 13 agent tools with unique ids, and claims with a file each', () => {
    expect(AGENT_TOOLS).toHaveLength(13)
    expect(new Set(AGENT_TOOLS.map((t) => t.id)).size).toBe(13)
    expect(CLAIMS.every((c) => c.file.length > 0)).toBe(true)
  })
})
