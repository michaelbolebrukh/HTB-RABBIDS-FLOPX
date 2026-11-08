// Tests for the W2 finance adapters, against responses recorded from the FLOPX backend on
// 8 Nov 2026 00:40 UTC (src/live/__fixtures__/w2; evaluation rows trimmed to the last two
// realised-vs-predicted points, company detail relatedItems trimmed to three). No network.
import { describe, expect, it } from 'vitest'
import companies from './__fixtures__/w2/companies.json'
import evaluation from './__fixtures__/w2/evaluation.json'
import portfolio from './__fixtures__/w2/eval_portfolio.json'
import seriesNvda from './__fixtures__/w2/series_NVDA.json'
import nvda from './__fixtures__/w2/company_NVDA.json'
import nvdaSignedIn from './__fixtures__/w2/company_NVDA_signed_in.json'
import crwv from './__fixtures__/w2/company_CRWV.json'
import nbis from './__fixtures__/w2/company_NBIS.json'
import iren from './__fixtures__/w2/company_IREN.json'
import apld from './__fixtures__/w2/company_APLD.json'
import {
  adaptCompanies, adaptEvaluation, adaptPortfolio, applyCloses, applyEvaluation, applyForecastValidation, applyFundamentals, applyModelledLinks, applyPortfolio, applyQuarterly,
  applySignalValidation, closeBadgeOf, closesBlindSpot, financeModule, makeSignalModule, placeWeekly, quarterlyOf, revGrowthOf, validatedAt1M, verdictOf, W2_LIVE, __resetW2,
  type CompaniesResponse, type CompanyDetailResponse, type EvaluationResponse, type FundamentalWire, type PortfolioEvalResponse, type SeriesResponseW2,
} from './adapt-w2'
import type { Getter } from './boot'
import { evalPriceBasis } from './evalBasis'
import { LIVE_TOTALS } from './store'
import { AWAITING_EVALUATION, BACKTEST_STATS, DESIGN_HORIZONS, FAILURES, MODEL_COMPARE, MODEL_COMPARE_META, SIGNALS, signalSupport } from '@/data/fund'
import { SECURITIES, secByTicker, WEEK_DATES, weekEnds } from '@/data/finance'

const C = companies as unknown as CompaniesResponse
const E = evaluation as unknown as EvaluationResponse
const P = portfolio as unknown as PortfolioEvalResponse
const S = seriesNvda as unknown as SeriesResponseW2
const detail = (d: unknown) => d as CompanyDetailResponse

/** A getter that serves recorded payloads by path, and records what was asked for. */
function fakeGetter(routes: Record<string, unknown>, asked: string[] = []): Getter {
  return (async (path: string) => {
    asked.push(path)
    return path in routes ? { ok: true, data: routes[path], ms: 1 } : { ok: false, error: 'HTTP 404', status: 404, ms: 1 }
  }) as Getter
}

describe('the module, when the backend fails', () => {
  it('reports no usable data and leaves every design export untouched', async () => {
    __resetW2()
    const before = JSON.stringify({ m: MODEL_COMPARE, b: BACKTEST_STATS, f: FAILURES, s: SECURITIES.map((s) => [s.revGrowth, s.px.at(-1), s.computeLinks]) })
    const ok = await financeModule.load(fakeGetter({}), () => {})
    expect(ok).toBe(false)
    expect(JSON.stringify({ m: MODEL_COMPARE, b: BACKTEST_STATS, f: FAILURES, s: SECURITIES.map((s) => [s.revGrowth, s.px.at(-1), s.computeLinks]) })).toBe(before)
    expect(MODEL_COMPARE_META.basis).toBe('design')
    expect(W2_LIVE.evaluation).toBeNull()
  })
  it('a timed-out evaluation leaves every signal in the honest pending state, never the design table (WF4 #6)', async () => {
    __resetW2()
    const timedOut: Getter = (async () => ({ ok: false, error: 'timeout after 20000 ms', ms: 20000 })) as Getter
    expect(await financeModule.load(timedOut, () => {})).toBe(false)
    for (const s of SIGNALS) {
      expect(s.basis).toBe('pending')
      expect(s.basisNote).toBe(AWAITING_EVALUATION)
      for (const h of ['1W', '1M', '3M'] as const) expect(s.horizons[h]).toMatchObject({ status: 'insufficient', n: 0, hit: null, ic: null })
    }
    expect(validatedAt1M()).toEqual({ validated: 0, total: SIGNALS.length, basis: 'pending' })
    expect(SIGNALS.flatMap((s) => s.support).some((x) => /validated/i.test(x))).toBe(false)
    expect(LIVE_TOTALS.priceBasis).toBeNull()
  })
})

describe('the closes blind spot follows the evaluation price basis (WF4 #5)', () => {
  const at = '2026-11-08T04:00:00Z'
  it('on sample closes it names the outage the backend reports', () => {
    const basis = evalPriceBasis({ sources: ['flopx-sample-v1'], sourceDetails: [] })
    const [row] = closesBlindSpot(basis, at, [{ source: 'yahoo-finance', reason: 'http-429' }])
    expect(row.what).toBe('Equity closes: yahoo-finance unavailable (http-429)')
    expect(row.why).toMatch(/authored sample closes/)
  })
  it('on alpaca-data archived closes it says archive, through when, and never "sample closes"', () => {
    const basis = evalPriceBasis({ sources: ['alpaca-data', 'flopx-sample-v1'], sourceDetails: [{ source: 'alpaca-data', kind: 'archived', observedAt: '2026-11-06T21:00:00Z' }, { source: 'flopx-sample-v1', kind: 'sample' }] })
    const rows = closesBlindSpot(basis, at, [{ source: 'yahoo-finance', reason: 'http-429' }])
    expect(rows).toHaveLength(1)
    expect(rows[0].what).toBe('Equity closes: alpaca-data archive through 2026-11-06, not a live feed')
    expect(rows[0].why).toMatch(/archived alpaca-data closes through 2026-11-06/)
    expect(rows[0].lesson).toMatch(/scored on alpaca-data closes through 2026-11-06/)
    expect(`${rows[0].what} ${rows[0].why} ${rows[0].lesson}`).not.toMatch(/serves authored sample closes|runs on those sample closes/)
  })
  it('on live closes there is no blind-spot entry', () => {
    expect(closesBlindSpot(evalPriceBasis({ sources: ['alpaca-data'], sourceDetails: [{ source: 'alpaca-data', kind: 'live' }] }), at, [])).toEqual([])
  })
  it('applyEvaluation records the basis for the tour (source, through) and the basis names flow into the signal notes', () => {
    __resetW2()
    const freshness = { generatedAt: at, sources: ['alpaca-data'], sourceDetails: [{ source: 'alpaca-data', kind: 'archived', observedAt: '2026-11-06T21:00:00Z' }] }
    applyEvaluation(adaptEvaluation(E), at, freshness)
    expect(LIVE_TOTALS.priceBasis).toEqual({ source: 'alpaca-data', through: '2026-11-06', kind: 'archived', label: 'Archived · alpaca-data closes through 2026-11-06' })
    expect(W2_LIVE.evaluation?.priceBasis).toMatchObject({ source: 'alpaca-data', through: '2026-11-06' })
    expect(FAILURES[0].what).toMatch(/alpaca-data archive through 2026-11-06/)
    applySignalValidation({ ...E, freshness })
    expect(SIGNALS.find((s) => s.id === 'sig-chips')!.basisNote).toBe('FLOPX evaluation of NVDA (alpaca-data closes)')
    applyModelledLinks({ ...E, freshness })
    expect(secByTicker('NVDA')!.computeLinks.find((l) => l.kind === 'modelled')!.text).toMatch(/alpaca-data closes\)$/)
    __resetW2()
  })
})

describe('adaptCompanies', () => {
  it('maps the 8 backend companies and marks sample closes illustrative', () => {
    const rows = adaptCompanies(C)
    expect(rows.map((r) => r.ticker)).toEqual(['NVDA', 'CRWV', 'NBIS', 'IREN', 'APLD', 'CORZ', 'VRT', 'SMCI'])
    const n = rows[0]
    expect(n.close?.value).toBe(182.4)
    expect(n.close?.fresh.freshness).toBe('illustrative')
    expect(rows.every((r) => r.close?.fresh.freshness === 'illustrative')).toBe(true)
  })
})

describe('revGrowthOf (SEC EDGAR revenue, like-for-like periods)', () => {
  it.each([
    ['NVDA', nvda, 95.8, '2026-07-26', '2025-07-27', '10-Q'],
    ['CRWV', crwv, 112.0, '2026-06-30', '2025-06-30', '10-Q'],
    ['NBIS', nbis, 350.9, '2025-12-31', '2024-12-31', '20-F'],
    ['IREN', iren, -5.0, '2026-09-30', '2025-09-30', '10-Q'],
    ['APLD', apld, 322.4, '2026-08-31', '2025-08-31', '10-Q'],
  ])('%s: latest filed period against the same period a year earlier', (_t, d, pct, latest, prior, form) => {
    const g = revGrowthOf(detail(d).fundamentals)!
    expect(g.pct).toBe(pct)
    expect(g.latest.periodEnd).toBe(latest)
    expect(g.prior.periodEnd).toBe(prior)
    expect(g.latest.form).toBe(form)
    expect(g.fresh.freshness).toBe('live')
  })

  it('never compares a quarter with a year: the 3-month IREN quarter skips the annual 10-K rows', () => {
    const g = revGrowthOf(detail(iren).fundamentals)!
    expect(g.latest.value).toBe(228_000_000)
    expect(g.prior.value).toBe(240_000_000)
  })

  it('ignores authored sample fundamentals', () => {
    const fs = detail(nvda).fundamentals.map((f): FundamentalWire => ({ ...f, provenance: { ...f.provenance, kind: 'sample' } }))
    expect(revGrowthOf(fs)).toBeNull()
  })
})

describe('placeWeekly (weekly twin of the month-axis rule)', () => {
  const ends = weekEnds(new Date('2026-11-08T00:40:00Z'))
  const design = ends.map((_, i) => 100 + i)

  it('anchors the last slot on the latest Friday on or before the generated time', () => {
    expect(ends.at(-1)!.toISOString()).toBe('2026-11-06T00:00:00.000Z')
    expect(ends[0].toISOString()).toBe('2024-08-30T00:00:00.000Z') // 114 weeks = 798 days earlier
    expect(ends).toHaveLength(115)
  })

  it('places no authored sample close', () => {
    const r = placeWeekly(design, ends, S.points)
    expect(r.used).toBe(0)
    expect(r.values).toEqual(design)
  })

  it('places observed closes in the week they end, last close of the week wins, rest stays design', () => {
    const live = S.points.map((p) => ({ ...p, provenance: { ...p.provenance, kind: 'live' as const } }))
    const r = placeWeekly(design, ends, live)
    const last = S.points.at(-1)!
    expect(last.t).toBe('2026-11-06T21:00:00Z')
    expect(r.values.at(-1)).toBe(last.value)
    expect(r.real.at(-1)).toBe(true)
    const firstReal = r.real.indexOf(true)
    expect(firstReal).toBeGreaterThan(80)
    for (let i = 0; i < firstReal; i++) expect(r.values[i]).toBe(design[i])
    const weekOfOct30 = ends.findIndex((d) => d.toISOString().startsWith('2026-10-30'))
    expect(r.values[weekOfOct30]).toBe(S.points.find((p) => p.t.startsWith('2026-10-30'))!.value)
  })
})

describe('adaptEvaluation (pooled across tickers with the backend verdict rule)', () => {
  const a = adaptEvaluation(E)

  it('keeps the backend thresholds: n < 20 insufficient, skill > 5% with n >= 40 improves, < -5% worsens', () => {
    expect(verdictOf('augmented', 19, 0.5)).toBe('insufficient-sample')
    expect(verdictOf('augmented', 39, 0.5)).toBe('inconclusive')
    expect(verdictOf('augmented', 40, 0.051)).toBe('improves')
    expect(verdictOf('augmented', 400, -0.051)).toBe('worsens')
    expect(verdictOf('baseline', 400, 0)).toBe('reference')
  })

  it('pools n and n-weighted MAE (1M, hand-derived from the payload)', () => {
    const [base, aug, comb] = a.rows['1M']
    expect(base.n).toBe(99 * 8)
    expect(aug.n).toBe(69 * 8)
    // mean of the eight 21-day augmented MAEs (equal n = 69 each)
    expect(aug.mae).toBe(0.164)
    expect(base.status).toBe('reference')
    expect(aug.status).toBe('no-edge')
    expect(aug.skill).toBeLessThan(-0.05)
    expect(comb.n).toBe(0)
    expect(comb.status).toBe('insufficient')
    expect(aug.ic).toBeNull()
    expect(aug.sharpe).toBeNull()
  })

  it('counts per-ticker verdicts of the compute model, and scored event forecasts', () => {
    expect(a.augmented['1M'].worsens).toBe(8)
    expect(a.augmented['3M']['insufficient-sample']).toBe(8)
    expect(a.combinedScored['1W']).toBe(3)
    expect(a.rows['1W'][2].n).toBe(29 + 20 + 23 + 2)
  })

  it('names the worst compute forecasts as blind spots, worst first, and marks the input class', () => {
    expect(a.failures).toHaveLength(3)
    expect(a.failures[0].what).toBe('APLD 1M forecast with compute data: RMSE 88% worse than baseline · n = 69')
    expect(a.failures[1].what).toMatch(/^CORZ 1M .* 62% worse/)
    expect(a.failures[2].what).toMatch(/^CRWV 1M .* 46% worse/)
    expect(a.fresh.freshness).toBe('illustrative')
    expect(a.tickers).toHaveLength(8)
  })
})

describe('adaptPortfolio (compute variants, equal capital per strategy)', () => {
  const a = adaptPortfolio(P)
  it('restates the backend backtests without estimating what it does not compute', () => {
    expect(a.stats.basis).toBe('backend')
    expect(a.stats.annRet).toBe(11.7) // (34.794 + 0 + 0.388) / 3
    expect(a.stats.maxDD).toBe(-17.0) // worst compute strategy
    expect(a.stats.hitRate).toBeCloseTo(2 / 3, 6) // (1 x 1 + 0.5 x 2) / 3 round trips
    expect(a.stats.trades).toBe(7)
    expect(a.stats.costBps).toBe(5)
    expect(a.stats.costsUsd).toBe(207.2)
    expect(a.stats.period).toBe('Jul 2026 to Nov 2026')
    expect([a.stats.grossSharpe, a.stats.netSharpe, a.stats.vol, a.stats.costDrag]).toEqual([null, null, null, null])
    expect(a.live.priceOnlyReturnPct).toBe(-8.6) // (-14.875 - 16.674 + 5.688) / 3
    expect(a.live.benchmarkReturnPct).toBe(17.6)
    expect(a.live.fresh.freshness).toBe('illustrative')
  })
})

describe('apply functions write into the design exports', () => {
  it('evaluation, links, fundamentals and backtests land in place', () => {
    __resetW2()
    W2_LIVE.unavailable.push({ source: 'yahoo-finance', reason: 'http-429' })
    applyEvaluation(adaptEvaluation(E), E.freshness.generatedAt)
    applyModelledLinks(E)
    applyPortfolio(adaptPortfolio(P), P.freshness.generatedAt)
    expect(applyFundamentals('NVDA', revGrowthOf(detail(nvda).fundamentals))).toBe(true)
    expect(applyFundamentals('CORZ', revGrowthOf(detail(nvda).fundamentals))).toBe(false) // not in the design list

    expect(MODEL_COMPARE['1M'].map((r) => r.model)).toEqual(['Trailing-return baseline', '+ Compute-market data', '+ News event intensity'])
    expect(MODEL_COMPARE_META).toMatchObject({ basis: 'backend', maeUnit: 'log ret.', scope: '8 tickers · sample closes' })
    expect(FAILURES[0].what).toBe('Equity closes: yahoo-finance unavailable (http-429)')
    expect(FAILURES).toHaveLength(4)
    expect(BACKTEST_STATS.basis).toBe('backend')
    expect(secByTicker('NVDA')!.revGrowth).toBe(96)
    const links = secByTicker('NVDA')!.computeLinks
    expect(links.filter((l) => l.kind === 'documented')).toHaveLength(1)
    expect(links.find((l) => l.kind === 'modelled')!.text).toBe('Compute features vs baseline at 1M: worsens, RMSE skill −22% (n = 69, sample closes)')
    expect(secByTicker('VST')!.computeLinks.find((l) => l.kind === 'modelled')!.text).toMatch(/TX pressure/) // no backend coverage: design text stays
  })
})

describe('the module, end to end on recorded payloads', () => {
  it('lands everything and never asks for a sample close series', async () => {
    __resetW2()
    const asked: string[] = []
    const routes: Record<string, unknown> = {
      '/flopx/companies': C,
      '/flopx/research/evaluation': E,
      '/flopx/research/evaluation/portfolio': P,
      '/flopx/companies/NVDA': nvda,
      '/flopx/companies/CRWV': crwv,
      '/flopx/companies/NBIS': nbis,
      '/flopx/companies/IREN': iren,
      '/flopx/companies/APLD': apld,
    }
    let applied = 0
    const ok = await financeModule.load(fakeGetter(routes, asked), () => applied++)
    expect(ok).toBe(true)
    expect(applied).toBeGreaterThanOrEqual(3)
    expect(asked.some((p) => p.startsWith('/flopx/gpu/series'))).toBe(false)
    expect(asked.filter((p) => /^\/flopx\/companies\/[A-Z]+$/.test(p)).sort()).toEqual(['/flopx/companies/APLD', '/flopx/companies/CRWV', '/flopx/companies/IREN', '/flopx/companies/NBIS', '/flopx/companies/NVDA'])
    expect(Object.values(W2_LIVE.companies).filter((c) => c.revGrowth).map((c) => c.ticker).sort()).toEqual(['APLD', 'CRWV', 'IREN', 'NBIS', 'NVDA'])
    expect(secByTicker('CRWV')!.revGrowth).toBe(112)
    expect(secByTicker('IREN')!.revGrowth).toBe(-5)
    expect(W2_LIVE.unavailable.map((u) => u.source)).toContain('yahoo-finance')
    expect(FAILURES[0].what).toMatch(/yahoo-finance unavailable/)
  })
})

describe('closeBadgeOf: a row is badged from the provenance of the close it shows (WF7 #1)', () => {
  const archived = { t: '2026-11-06T21:00:00Z', value: 186.7, fresh: { freshness: 'historical' as const }, source: 'alpaca-data', latestWeek: true }
  it('nothing placed, or a company unknown to the backend, stays "Illus."', () => {
    expect(closeBadgeOf(undefined)).toEqual({ f: 'illustrative', label: 'Illus.' })
    expect(closeBadgeOf({ placed: null })).toEqual({ f: 'illustrative', label: 'Illus.' })
  })
  it('an archived alpaca-data close in the latest week reads "Archived · 06 Nov"', () => {
    expect(closeBadgeOf({ placed: archived })).toEqual({ f: 'historical', label: 'Archived · 06 Nov', title: 'alpaca-data close 06 Nov 2026' })
  })
  it('a live close reads "Live · 06 Nov"; a sample point (never placed) stays "Illus."', () => {
    expect(closeBadgeOf({ placed: { ...archived, fresh: { freshness: 'live' } } }).label).toBe('Live · 06 Nov')
    expect(closeBadgeOf({ placed: { ...archived, fresh: { freshness: 'illustrative' } } })).toEqual({ f: 'illustrative', label: 'Illus.' })
  })
  it('an archive that stops short of the latest week leaves the row "Illus." (the figure shown is the design, joined), saying so on hover', () => {
    const b = closeBadgeOf({ placed: { ...archived, latestWeek: false } })
    expect(b.f).toBe('illustrative')
    expect(b.label).toBe('Illus.')
    expect(b.title).toMatch(/Last observed close 06 Nov 2026 \(alpaca-data\)/)
  })
  it('applyCloses records the placed point, and a later all-sample series clears it and restores the design weeks (sign-out)', () => {
    __resetW2()
    const nvda = secByTicker('NVDA')!
    const design = [...nvda.px]
    const archive = S.points.map((p) => ({ ...p, provenance: { ...p.provenance, kind: 'archived' as const, source: 'alpaca-data' } }))
    expect(applyCloses('NVDA', archive)).toBeGreaterThan(0)
    expect(W2_LIVE.companies.NVDA.placed).toEqual({ t: '2026-11-06T21:00:00Z', value: S.points.at(-1)!.value, fresh: { freshness: 'historical' }, source: 'alpaca-data', latestWeek: true })
    expect(closeBadgeOf(W2_LIVE.companies.NVDA).label).toBe('Archived · 06 Nov')
    expect(nvda.px.at(-1)).toBe(S.points.at(-1)!.value)
    // The per-week flags the candle readout reads to name the archive on an observed week (WF8 #4).
    const real = W2_LIVE.companies.NVDA.realWeeks
    expect(real).toHaveLength(nvda.px.length)
    expect(real.filter(Boolean)).toHaveLength(W2_LIVE.companies.NVDA.observedWeeks)
    expect(real.at(-1)).toBe(true)
    expect(applyCloses('NVDA', S.points)).toBe(0) // the anonymous series: every point sample
    expect(W2_LIVE.companies.NVDA.placed).toBeNull()
    expect(W2_LIVE.companies.NVDA.observedWeeks).toBe(0)
    expect(W2_LIVE.companies.NVDA.realWeeks).toEqual([])
    expect(closeBadgeOf(W2_LIVE.companies.NVDA).label).toBe('Illus.')
    expect(nvda.px).toEqual(design)
    expect((nvda as { pxScaled?: boolean }).pxScaled).toBe(false)
    __resetW2()
  })
})

describe('quarterlyOf: the archived WRDS Compustat quarterlies a signed-in detail carries (WF7 #2)', () => {
  const signedIn = detail(nvdaSignedIn)
  it('groups the 99 wrds-compustat rows into 35 fiscal quarters, ascending, with capex intensity where both sides exist', () => {
    const q = quarterlyOf(signedIn.fundamentals)!
    expect(q).not.toBeNull()
    expect(q.source).toBe('wrds-compustat')
    expect(q.fresh).toEqual({ freshness: 'historical' })
    expect(q.quarters).toHaveLength(35)
    expect(q.from).toBe('2018-01-31')
    expect(q.through).toBe('2026-07-31')
    expect(q.licence).toMatch(/signed-in accounts only/)
    const latest = q.quarters.at(-1)!
    expect(latest).toMatchObject({ periodEnd: '2026-07-31', fq: 2, revenue: 96_222_000_000, capex: 2_677_000_000, totalAssets: 320_272_000_000 })
    expect(latest.intensity).toBeCloseTo(2_677 / 96_222, 6)
    const q1 = q.quarters.find((x) => x.periodEnd === '2026-04-30')!
    expect(q1).toMatchObject({ revenue: null, capex: 1_757_000_000, intensity: null })
    expect(q.quarters.map((x) => x.periodEnd)).toEqual([...q.quarters.map((x) => x.periodEnd)].sort())
  })
  it('ignores SEC EDGAR and sample rows: the anonymous detail yields null', () => {
    expect(quarterlyOf(detail(nvda).fundamentals)).toBeNull()
    const sample = signedIn.fundamentals.map((f) => ({ ...f, provenance: { ...f.provenance, kind: 'sample' as const } }))
    expect(quarterlyOf(sample)).toBeNull()
  })
  it('applyQuarterly sets, reports no change on the same payload, and clears on an anonymous one', () => {
    __resetW2()
    const q = quarterlyOf(signedIn.fundamentals)
    expect(applyQuarterly('NVDA', q)).toBe(true)
    expect(W2_LIVE.companies.NVDA.quarterly?.quarters).toHaveLength(35)
    expect(applyQuarterly('NVDA', q)).toBe(false)
    expect(applyQuarterly('NVDA', null)).toBe(true)
    expect(W2_LIVE.companies.NVDA.quarterly).toBeNull()
    expect(applyQuarterly('CORZ', q)).toBe(false) // not in the design list
    __resetW2()
  })
  it('the module lands the quarterlies from a signed-in detail and leaves the SEC growth where it was', async () => {
    __resetW2()
    const ok = await financeModule.load(fakeGetter({ '/flopx/companies': C, '/flopx/companies/NVDA': nvdaSignedIn }), () => {})
    expect(ok).toBe(true)
    expect(W2_LIVE.companies.NVDA.quarterly?.through).toBe('2026-07-31')
    // The growth stays the SEC filing's (live, a 10-Q), not the Compustat quarter whose period end comes later.
    const rg = W2_LIVE.companies.NVDA.revGrowth!
    expect(rg.fresh.freshness).toBe('live')
    expect(rg.latest.form).toBe('10-Q')
    expect(rg.pct).toBe(revGrowthOf(detail(nvda).fundamentals)!.pct)
    expect(W2_LIVE.companies.CRWV?.quarterly ?? null).toBeNull()
    __resetW2()
  })
})

describe('applyCloses joins the design weeks onto observed closes (G14, weekly axis)', () => {
  it('real weeks replace the design, earlier weeks are the design multiplied to meet the first real week', () => {
    __resetW2()
    const nvda = secByTicker('NVDA')!
    const design = [...nvda.px]
    const live = S.points.map((p) => ({ ...p, provenance: { ...p.provenance, kind: 'live' as const } }))
    const n = applyCloses('NVDA', live)
    expect(n).toBeGreaterThan(0)
    const last = S.points.at(-1)!
    expect(nvda.px.at(-1)).toBe(last.value)
    const firstReal = placeWeekly(design, WEEK_DATES, live).real.indexOf(true)
    expect(firstReal).toBeGreaterThan(0)
    const f = nvda.px[firstReal] / design[firstReal]
    expect(nvda.px[firstReal - 1]).toBeCloseTo(design[firstReal - 1] * f, 2)
    expect(nvda.px[0]).toBeCloseTo(design[0] * f, 2)
    expect((nvda as { pxScaled?: boolean }).pxScaled).toBe(true)
    design.forEach((v, i) => (nvda.px[i] = v))
    __resetW2()
  })
})

describe('signal validation comes from the backend, never the design table', () => {
  const refused = { kind: 'refused' as const, detail: { code: 'flopx.insufficient-history', reason: 'series too short', history_points: 4, horizon: 30, required_points: 60 } }

  it('ticker signals take the compute model verdict, n and hit rate per horizon; baskets and uncovered tickers read insufficient, n = 0', () => {
    __resetW2()
    const n = applySignalValidation(E)
    expect(n).toBe(SIGNALS.length - 1)
    const nvda = SIGNALS.find((s) => s.id === 'sig-chips')! // asset NVDA, evaluated
    expect(nvda.basis).toBe('backend')
    expect(nvda.horizons['1M']).toMatchObject({ status: 'no-edge', n: 69 }) // backend verdict 'worsens' at 21 days
    expect(nvda.horizons['3M']).toMatchObject({ status: 'insufficient', n: 0 })
    expect(nvda.horizons['1W'].hit).toBeCloseTo(0.5149, 4) // 1W = 5 trading days in the backend
    const vst = SIGNALS.find((s) => s.id === 'sig-ercot')! // asset VST, not in the backend
    expect(vst.horizons['1M']).toMatchObject({ status: 'insufficient', n: 0, hit: null })
    expect(vst.basisNote).toMatch(/no evaluation of VST/)
    const basket = SIGNALS.find((s) => s.id === 'sig-neo')!
    expect(basket.horizons['1M'].status).toBe('insufficient')
    expect(SIGNALS.find((s) => s.id === 'sig-h100')!.basis).toBe('pending') // the index signal waits for the forecast call
    __resetW2()
    expect(vst.horizons['1M']).toMatchObject({ status: 'insufficient', n: 0 }) // the pending state, never the design's validated, after reset
    expect(vst.basis).toBe('pending')
    expect(DESIGN_HORIZONS.get('sig-ercot')!['1M']).toMatchObject({ status: 'validated', n: 26 }) // the design table survives only here
  })

  it('"Validated at 1M" is derived from a backend-granted status and from nothing else (WF4 #3)', () => {
    __resetW2()
    const s = SIGNALS.find((x) => x.id === 'sig-nova')!
    expect(signalSupport(s).some((x) => /validated/i.test(x))).toBe(false) // pending
    applySignalValidation(E)
    expect(signalSupport(s).some((x) => /validated/i.test(x))).toBe(false) // backend says insufficient for DLR
    s.horizons['1M'] = { ...s.horizons['1M'], status: 'validated', n: 41 }
    expect(signalSupport(s).at(-1)).toBe('Validated at 1M by the FLOPX evaluation (n = 41)')
    s.basis = 'design'
    expect(signalSupport(s).some((x) => /validated/i.test(x))).toBe(false) // a design-basis validated status earns no sentence
    __resetW2()
  })

  it('the H100 index signal reads insufficient at every horizon when the backend refuses to forecast', () => {
    __resetW2()
    expect(applyForecastValidation(refused)).toBe(true)
    const s = SIGNALS.find((x) => x.id === 'sig-h100')!
    expect(s.basis).toBe('backend')
    expect(s.horizons['1W'].status).toBe('insufficient')
    expect(s.horizons['1M']).toMatchObject({ status: 'insufficient', n: 4, hit: null, ic: null })
    expect(s.horizons['3M'].status).toBe('insufficient')
    expect(s.basisNote).toBe('FLOPX refuses to forecast this index: 4 of 60 daily points of history')
    expect(s.support).toEqual([])
    expect(applyForecastValidation({ kind: 'failed', error: 'down' })).toBe(false)
    __resetW2()
    expect(s.horizons['1M']).toMatchObject({ status: 'insufficient', n: 0 })
    expect(s.basisNote).toBe(AWAITING_EVALUATION)
  })

  it('counts validated signals at 1M with the basis the count rests on', () => {
    __resetW2()
    expect(validatedAt1M()).toEqual({ validated: 0, total: SIGNALS.length, basis: 'pending' })
    applySignalValidation(E)
    applyForecastValidation(refused)
    expect(validatedAt1M()).toEqual({ validated: 0, total: SIGNALS.length, basis: 'backend' })
    __resetW2()
  })

  it('the signal module posts to /flopx/forecast and reads a 422 refusal as data', async () => {
    __resetW2()
    const calls: string[] = []
    const fetchImpl = (async (url: string, init?: RequestInit) => {
      calls.push(`${init?.method} ${new URL(url).pathname}`)
      return new Response(JSON.stringify(refused), { status: 422, headers: { 'content-type': 'application/json' } })
    }) as typeof fetch
    let applied = 0
    const ok = await makeSignalModule({ fetchImpl, base: 'http://x' }).load((async () => ({ ok: false, error: 'unused' })) as unknown as Getter, () => applied++)
    expect(ok).toBe(true)
    expect(applied).toBe(1)
    expect(calls).toEqual(['POST /flopx/forecast'])
    expect(SIGNALS.find((x) => x.id === 'sig-h100')!.horizons['1M'].status).toBe('insufficient')
    __resetW2()
  })
})
