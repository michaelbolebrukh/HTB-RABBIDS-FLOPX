// The research metrics panel: the recorded evaluation (taken before the backend carried IC and Sharpe)
// renders the honest gap, a payload carrying them renders the backend's own figures and the history
// coverage, and a missing payload says the evaluation has not answered. Server-rendered; no DOM.
import { describe, expect, it } from 'vitest'
import type { ReactElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { StoreProvider } from '@/lib/store'
import recorded from '@/live/__fixtures__/w2/evaluation.json'
import { basisOf, fmtDay, fmtIc, fmtShare, fmtSharpe, fmtT, NOT_IN_PAYLOAD, NOT_LOADED, NOT_SCORED, ResearchMetricsView, type ResearchMetricsPayload } from './ResearchMetrics'

// The store reads the hash and session storage in its state initialisers; effects do not run on the server.
;(globalThis as { window?: unknown }).window = { location: { hash: '#/research' }, sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} } }

/** The Panel reads the collapse store, so every render sits inside the provider the app mounts (as pages/labels.test.tsx does). */
const render = (el: ReactElement) => renderToStaticMarkup(<StoreProvider>{el}</StoreProvider>)

const note = { provenance: 'research' as const }
// The sample-bag run of 8 Nov 2026 (fastapi_backend, live adapters off): the h = 21 augmented row and two empty rows.
const payload: ResearchMetricsPayload = {
  horizons: [21, 63],
  crossSection: [
    { horizonDays: 21, model: 'baseline', nDates: 99, meanIc: 0.0909, tStat: 2.819, shareDatesFivePlus: 1, tickers: 8, ...note, note: 'Spearman across tickers per origin date; origins overlap for h > 1, so the t-statistic is optimistic' },
    { horizonDays: 21, model: 'augmented', nDates: 69, meanIc: 0.1743, tStat: 3.477, shareDatesFivePlus: 1, tickers: 8, ...note, note: 'Spearman across tickers per origin date; origins overlap for h > 1, so the t-statistic is optimistic' },
    { horizonDays: 21, model: 'combined', nDates: 0, meanIc: null, tStat: null, shareDatesFivePlus: null, tickers: 0, ...note, note: '' },
    { horizonDays: 63, model: 'baseline', nDates: 57, meanIc: -0.0368, tStat: -1.317, shareDatesFivePlus: 1, tickers: 8, ...note, note: '' },
    { horizonDays: 63, model: 'augmented', nDates: 0, meanIc: null, tStat: null, shareDatesFivePlus: null, tickers: 0, ...note, note: '' },
    { horizonDays: 63, model: 'combined', nDates: 0, meanIc: null, tStat: null, shareDatesFivePlus: null, tickers: 0, ...note, note: '' },
  ],
  tradable: [
    { horizonDays: 21, model: 'baseline', nTrades: 40, sharpe: -0.615, meanExcessLogReturn: -0.018958, hitRate: 0.45, tickers: 8, ...note, note: 'no costs, no slippage, point in time; a research number, not a traded record' },
    { horizonDays: 21, model: 'augmented', nTrades: 32, sharpe: -0.331, meanExcessLogReturn: -0.010163, hitRate: 0.4688, tickers: 8, ...note, note: '' },
    { horizonDays: 21, model: 'combined', nTrades: 0, sharpe: null, meanExcessLogReturn: null, hitRate: null, tickers: 0, ...note, note: '' },
    { horizonDays: 63, model: 'baseline', nTrades: 8, sharpe: null, meanExcessLogReturn: 0, hitRate: 0.625, tickers: 8, ...note, note: '' },
    { horizonDays: 63, model: 'augmented', nTrades: 0, sharpe: null, meanExcessLogReturn: null, hitRate: null, tickers: 0, ...note, note: '' },
    { horizonDays: 63, model: 'combined', nTrades: 0, sharpe: null, meanExcessLogReturn: null, hitRate: null, tickers: 0, ...note, note: '' },
  ],
  historyCoverage: [
    { ticker: 'NVDA', firstClose: '2026-05-11T21:00:00Z', lastClose: '2026-11-06T21:00:00Z', tradingDays: 180, spanYears: 0.49, originsScored: 119, originsByHorizon: { '1': 119, '5': 115, '21': 99, '63': 57 }, inputKinds: ['sample'] },
    { ticker: 'CRWV', firstClose: '2026-05-11T21:00:00Z', lastClose: '2026-11-06T21:00:00Z', tradingDays: 180, spanYears: 0.49, originsScored: 119, originsByHorizon: { '1': 119, '5': 115, '21': 99, '63': 57 }, inputKinds: ['sample'] },
  ],
}

describe('ResearchMetricsView', () => {
  it('renders the honest gap for the recorded evaluation, which predates IC and Sharpe', () => {
    const html = render(<ResearchMetricsView payload={recorded as unknown as ResearchMetricsPayload} />)
    expect(html).toContain(NOT_IN_PAYLOAD)
    expect(html).toContain('Not in this payload')
    expect(html).not.toContain('data-row=')
    expect(html).not.toContain('Enough history?')
  })
  it('renders the backend figures per (horizon, model), the notes, and the history each ticker stood on', () => {
    const html = render(<ResearchMetricsView payload={payload} />)
    expect(html).toContain('Research number · sample closes')
    expect(html).toContain('2 tickers · 21d · 63d')
    // The h = 21 compute-augmented row: mean IC +0.174, t 3.48 over 69 dates all with five or more tickers, Sharpe −0.33 over 32 trades, hit 47%.
    const row = html.match(/data-row="21-augmented"[\s\S]*?<\/div>/)?.[0] ?? ''
    expect(row).toContain('+0.174')
    expect(row).toContain('t 3.48')
    expect(row).toContain('69 · 100%')
    expect(row).toContain('−0.33')
    expect(row).toContain('>32<')
    expect(row).toContain('47%')
    // The h = 63 combined row scored nothing: dashes and "not scored", never a zero dressed as a figure.
    const empty = html.match(/data-row="63-combined"[\s\S]*?<\/div>/)?.[0] ?? ''
    expect(empty).toContain(NOT_SCORED)
    expect((empty.match(/—/g) ?? []).length).toBeGreaterThanOrEqual(4)
    // The 63d baseline traded once per ticker: eight trades, no Sharpe (no spread), hit 63%.
    const once = html.match(/data-row="63-baseline"[\s\S]*?<\/div>/)?.[0] ?? ''
    expect(once).toContain('>8<')
    expect(once).toContain('63%')
    expect(html).toContain('the t-statistic is optimistic')
    expect(html).toContain('a research number, not a traded record')
    // Coverage: NVDA, 180 trading days from 2026-05-11 to 2026-11-06, 0.5 years, 119 origins at 1d.
    const cov = html.match(/data-ticker="NVDA"[\s\S]*?<\/div>/)?.[0] ?? ''
    expect(cov).toContain('2026-05-11 → 2026-11-06')
    expect(cov).toContain('>180<')
    expect(cov).toContain('0.5')
    expect(cov).toContain('21d 99 · 63d 57')
  })
  it('says the evaluation has not answered when there is no payload', () => {
    expect(render(<ResearchMetricsView payload={null} />)).toContain(NOT_LOADED)
    expect(render(<ResearchMetricsView payload={undefined} />)).toContain(NOT_LOADED)
  })
  it('formats without inventing a missing figure, and badges observed closes differently from sample ones', () => {
    expect(fmtIc(0.1743)).toBe('+0.174')
    expect(fmtIc(-0.0368)).toBe('−0.037')
    expect(fmtIc(null)).toBe('—')
    expect(fmtSharpe(-0.331)).toBe('−0.33')
    expect(fmtSharpe(null)).toBe('—')
    expect(fmtT(3.477)).toBe('t 3.48')
    expect(fmtT(null)).toBe('')
    expect(fmtShare(0.4688)).toBe('47%')
    expect(fmtShare(null)).toBe('—')
    expect(fmtDay('2026-05-11T21:00:00Z')).toBe('2026-05-11')
    expect(fmtDay(null)).toBe('—')
    expect(basisOf(payload.historyCoverage)).toEqual({ f: 'illustrative', label: 'Research number · sample closes' })
    expect(basisOf([{ ...payload.historyCoverage![0]!, inputKinds: ['live'] }]).f).toBe('historical')
    expect(basisOf(undefined).f).toBe('illustrative')
  })
})
