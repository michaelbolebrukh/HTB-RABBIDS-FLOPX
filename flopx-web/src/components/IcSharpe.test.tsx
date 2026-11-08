// IC and Sharpe in the model-comparison grid (Research and Fund Evaluation): a payload carrying them
// renders the backend's own figures at the chosen horizon with the quiet 'no costs, research number'
// note; a payload without them, or no answer, says so and shows no number. Server-rendered; no DOM.
import { describe, expect, it } from 'vitest'
import type { ReactElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { StoreProvider } from '@/lib/store'
import { IC_SHARPE_GAP, IcSharpeView } from './ui'
import type { ResearchMetricsPayload } from './ResearchMetrics'

;(globalThis as { window?: unknown }).window = { location: { hash: '#/research' }, sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} } }
const render = (el: ReactElement) => renderToStaticMarkup(<StoreProvider>{el}</StoreProvider>)
const n = { provenance: 'research' as const, note: '', tickers: 8 }
const payload: ResearchMetricsPayload = {
  horizons: [21],
  crossSection: [
    { horizonDays: 21, model: 'baseline', nDates: 99, meanIc: 0.0909, tStat: 2.819, shareDatesFivePlus: 1, ...n },
    { horizonDays: 21, model: 'augmented', nDates: 69, meanIc: 0.1743, tStat: 3.477, shareDatesFivePlus: 1, ...n },
  ],
  tradable: [
    { horizonDays: 21, model: 'baseline', nTrades: 40, sharpe: -0.615, meanExcessLogReturn: -0.019, hitRate: 0.45, ...n },
    { horizonDays: 21, model: 'augmented', nTrades: 32, sharpe: -0.331, meanExcessLogReturn: -0.01, hitRate: 0.47, ...n },
  ],
  historyCoverage: [{ ticker: 'NVDA', firstClose: null, lastClose: null, tradingDays: 180, spanYears: 0.49, originsScored: 119, originsByHorizon: {}, inputKinds: ['sample'] }],
}

describe('IcSharpeView', () => {
  it('renders the backend IC and Sharpe at the 1M (21 day) horizon with the quiet caveat', () => {
    const html = render(<IcSharpeView payload={payload} horizon="1M" />)
    expect(html).toContain('+0.174')
    expect(html).toContain('t 3.48 · 69')
    expect(html).toContain('−0.33')
    expect(html).toContain('−0.61')
    expect(html).toContain('no costs, research number')
    expect(html).toContain('Research number · sample closes')
    // combined has no row in the payload: not scored, no invented number
    expect(html).toContain('not scored')
  })
  it('shows no number at a horizon the payload has not scored', () => {
    const html = render(<IcSharpeView payload={payload} horizon="3M" />)
    expect(html).not.toContain('+0.174')
    expect(html).not.toMatch(/[+−]\d\.\d{2}/)
  })
  it('says so when the payload lacks IC and Sharpe', () => {
    const html = render(<IcSharpeView payload={{ horizons: [21] }} horizon="1M" />)
    expect(html).toContain(IC_SHARPE_GAP)
    expect(html).not.toContain('no costs, research number')
  })
  it('says so when the evaluation did not answer', () => {
    expect(render(<IcSharpeView payload={null} horizon="1M" />)).toContain('did not answer')
  })
})
