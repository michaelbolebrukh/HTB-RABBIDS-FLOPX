// Label thinning on Securities and Fund (WF8 #3, the N-BADGE rule): a panel says its status once as a
// quiet note; a bordered chip sits only on a row whose status differs from its panel or on a single
// headline figure. Server-rendered inside the store provider, signed out (no archive placed), and
// again with an archived close placed on NVDA.
import { afterEach, describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { StoreProvider } from '@/lib/store'
import Securities from './Securities'
import Fund from './Fund'
import seriesNvda from '@/live/__fixtures__/w2/series_NVDA.json'
import { __resetW2, applyCloses, W2_LIVE, type SeriesResponseW2 } from '@/live/adapt-w2'
import { secByTicker } from '@/data/finance'

// The store reads the hash and session storage in its state initialisers; effects do not run on the server.
;(globalThis as { window?: unknown }).window = { location: { hash: '#/securities' }, sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} } }

const S = seriesNvda as unknown as SeriesResponseW2
const chips = (html: string) => html.match(/data-fresh="chip"/g)?.length ?? 0
const quiet = (html: string) => html.match(/data-fresh="quiet"/g)?.length ?? 0
const render = (el: React.ReactElement) => renderToStaticMarkup(<StoreProvider>{el}</StoreProvider>)

describe('Securities labels', () => {
  const nvda = secByTicker('NVDA')!
  const design = [...nvda.px]
  afterEach(() => { design.forEach((v, i) => (nvda.px[i] = v)); __resetW2() })

  it('signed out: one chip (the headline price), no chip on any list row, a quiet note per panel', () => {
    __resetW2()
    const html = render(<Securities />)
    expect(chips(html)).toBe(1)
    expect(html).not.toContain('Illus.')
    expect(quiet(html)).toBeGreaterThanOrEqual(6)
    expect(html).toContain('Illustrative') // the quiet notes carry the full word
  })

  it('with an archived close placed on NVDA: the NVDA row and the headline keep "Archived", the list note says "unless a row says otherwise", and the readout names the archive', () => {
    __resetW2()
    const archive = S.points.map((p) => ({ ...p, provenance: { ...p.provenance, kind: 'archived' as const, source: 'alpaca-data' } }))
    expect(applyCloses('NVDA', archive)).toBeGreaterThan(0)
    // The companies list (adaptCompanies, run before the series read) tagged the same close "live" on
    // the preview; the header and the readout follow the placed series point, as the row does.
    const last = S.points.at(-1)!
    W2_LIVE.companies.NVDA.close = { value: last.value, t: last.t, fresh: { freshness: 'live' } }
    const html = render(<Securities />)
    expect(chips(html)).toBe(2) // the NVDA list row and the headline price
    expect(html).toContain('Archived · 06 Nov') // the row
    expect(html).toContain('Archived · close 06 Nov 2026') // the headline
    expect(html).toContain('title="alpaca-data close 06 Nov 2026"')
    expect(html).not.toContain('close live')
    expect(html).toContain('Illustrative unless marked')
    expect(html).not.toContain('Historical')
    // The candle readout shows the latest week's close from the same series as the header, with its source.
    const close = nvda.px.at(-1)!.toFixed(2)
    expect(html).toContain(`$${close}`)
    expect(html).toContain('close archived · alpaca-data')
    expect(html).not.toContain(`$${design.at(-1)!.toFixed(2)}`)
  })
})

describe('Fund labels', () => {
  it('signed out: the one chip is the headline snapshot on the "Edge in" figure; every panel carries a quiet note', () => {
    const html = render(<Fund />)
    expect(chips(html)).toBe(1)
    expect(html).toContain('Snapshot')
    expect(quiet(html)).toBeGreaterThanOrEqual(8)
    expect(html).toContain('Paper since')
    expect(html).not.toContain('Historical')
  })
})
