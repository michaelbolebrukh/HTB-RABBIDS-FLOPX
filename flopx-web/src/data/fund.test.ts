// The fund's signals start honest: before any backend read, nothing claims validation (WF4 #3, #6)
// and no design stamp sits ahead of the clock (WF4 #12).
import { describe, expect, it } from 'vitest'
import { AWAITING_EVALUATION, DECISIONS, DESIGN_HORIZONS, INITIAL_PROPOSALS, resetSignalToPending, SIGNALS, signalSupport } from './fund'

describe('signals at module init', () => {
  it('every horizon of every signal reads insufficient, with hit, IC and n cleared and the awaiting note', () => {
    for (const s of SIGNALS) {
      expect(s.basis).toBe('pending')
      expect(s.basisNote).toBe(AWAITING_EVALUATION)
      for (const h of ['1W', '1M', '3M'] as const) {
        expect(s.horizons[h]).toMatchObject({ status: 'insufficient', hit: null, ic: null, n: 0, baselineHit: null })
        expect(Number.isFinite(s.horizons[h].mean)).toBe(true) // the illustrative distribution is kept
      }
    }
  })
  it('keeps the design table aside for reference: three signals the design called validated at 1M', () => {
    const validated = [...DESIGN_HORIZONS.values()].filter((h) => h['1M'].status === 'validated').length
    expect(validated).toBe(3)
    expect(DESIGN_HORIZONS.get('sig-h100')!['1M']).toMatchObject({ status: 'validated', n: 26, hit: 0.64 })
  })
  it('no supporting-evidence line, proposal or decision text claims validation', () => {
    expect(SIGNALS.flatMap((s) => signalSupport(s)).some((x) => /validated/i.test(x))).toBe(false)
    expect(INITIAL_PROPOSALS.some((p) => /validated/i.test(p.reasoning))).toBe(false)
    expect(DECISIONS.some((d) => /validated/i.test(d.decision.reasoning) || d.information.some((i) => /validated/i.test(i.value)))).toBe(false)
  })
  it('resetSignalToPending is idempotent and keeps the distribution', () => {
    const s = structuredClone(SIGNALS[0])
    s.horizons['1M'] = { ...s.horizons['1M'], status: 'validated', n: 40, hit: 0.6 }
    s.basis = 'backend'
    resetSignalToPending(s)
    expect(s.horizons['1M']).toMatchObject({ status: 'insufficient', n: 0, hit: null, mean: SIGNALS[0].horizons['1M'].mean })
    expect(s.basis).toBe('pending')
  })
})

describe('design stamps', () => {
  it('the signals are stamped at a completed trading close strictly before now, never ahead of the clock', () => {
    const stamp = SIGNALS[0].updated
    expect(stamp).toMatch(/^\d{2} \w{3} \d{4} 21:00 UTC \(design example\)$/)
    const m = /^(\d{2}) (\w{3}) (\d{4}) 21:00/.exec(stamp)!
    const t = Date.parse(`${m[1]} ${m[2]} ${m[3]} 21:00:00 UTC`)
    expect(t).toBeLessThan(Date.now())
    expect([0, 6]).not.toContain(new Date(t).getUTCDay())
    expect(SIGNALS.every((s) => s.updated === stamp)).toBe(true)
  })
})
