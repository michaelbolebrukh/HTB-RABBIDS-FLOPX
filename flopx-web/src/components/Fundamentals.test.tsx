// The signed-in branch of the Securities fundamentals panel: the WRDS Compustat quarterlies render
// for a detail payload that carries them (recorded signed in, 8 Nov 2026 08:57 UTC) and nothing
// renders for the anonymous payload (SEC EDGAR only). Server-rendered; no DOM.
import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import signedIn from '@/live/__fixtures__/w2/company_NVDA_signed_in.json'
import anonymous from '@/live/__fixtures__/w2/company_NVDA.json'
import { quarterlyOf, type CompanyDetailResponse } from '@/live/adapt-w2'
import { fmtIntensity, fmtUsd, quarterLabel, QuarterlySection, WRDS_AUDIENCE, WRDS_BADGE } from './Fundamentals'

const detail = (d: unknown) => d as CompanyDetailResponse

describe('QuarterlySection (the signed-in panel branch)', () => {
  it('renders the last eight fiscal quarters, badged for the archive and the audience', () => {
    const q = quarterlyOf(detail(signedIn).fundamentals)!
    const html = renderToStaticMarkup(<QuarterlySection quarterly={q} />)
    expect(html).toContain(WRDS_BADGE)
    expect(html).toContain(`Served to ${WRDS_AUDIENCE} · last 8 of 35 fiscal quarters`)
    expect(html.match(/<tr class="border-t/g)).toHaveLength(8)
    // The latest quarter (Q2 to 31 Jul 2026): revenue 96.222bn, capex 2.677bn, 2.8% intensity.
    expect(html).toContain('Q2 · Jul 2026')
    expect(html).toContain('$96.2B')
    expect(html).toContain('$2.7B')
    expect(html).toContain('2.8%')
    // A quarter without a revenue row (Q1 to 30 Apr 2026) shows its capex and no ratio, never an estimate.
    expect(html).toMatch(/Apr 2026<\/td><td class="num[^"]*">—<\/td><td class="num[^"]*">\$1\.8B<\/td><td class="num[^"]*">—<\/td>/)
    expect(html).toContain('Revenue growth above stays the SEC filing figure')
  })
  it('renders nothing for the anonymous payload, so the panel is as it is for everyone else', () => {
    expect(quarterlyOf(detail(anonymous).fundamentals)).toBeNull()
    expect(renderToStaticMarkup(<QuarterlySection quarterly={null} />)).toBe('')
    expect(renderToStaticMarkup(<QuarterlySection quarterly={undefined} />)).toBe('')
  })
  it('formats money and ratios without inventing a missing side', () => {
    expect(fmtUsd(96_222_000_000)).toBe('$96.2B')
    expect(fmtUsd(977_000_000)).toBe('$977M')
    expect(fmtUsd(null)).toBe('—')
    expect(fmtIntensity(0.0278)).toBe('2.8%')
    expect(fmtIntensity(null)).toBe('—')
    expect(quarterLabel({ periodEnd: '2026-07-31', fq: 2, revenue: null, capex: null, totalAssets: null, intensity: null })).toBe('Q2 · Jul 2026')
    expect(quarterLabel({ periodEnd: '2026-07-31', fq: null, revenue: null, capex: null, totalAssets: null, intensity: null })).toBe('Jul 2026')
  })
})
