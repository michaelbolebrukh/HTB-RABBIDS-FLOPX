// W1 adapters against payloads recorded from https://flopx-backend.onrender.com on 08 Nov 2026
// 00:39 UTC (trimmed: evidence lists cut to two entries, only the H100 and RTX 4090 pressure cells).
import { describe, expect, it } from 'vitest'
import { ANOMALY_CARDS, GLOBAL_CPI, NOW_IDX, PRESSURE_MODEL, REGIONS, regionById, regionEvidence } from '@/data/core'
import { adaptAnomalies, adaptExposure, adaptMapRegions, adaptPressure, adaptPressureCell, adaptSupply, applyW1, composeRegion, globalFromPressure, regionsModule, type AnomaliesResponse, type ExposureResponse, type MapRegionsW1Response, type PressureW1Response, type SupplyResponse } from './adapt-w1'
import { makeGetter } from './boot'
import pressureJson from './__fixtures__/w1/pressure.json'
import mapJson from './__fixtures__/w1/map-regions.json'
import supplyJson from './__fixtures__/w1/supply.json'
import anomaliesJson from './__fixtures__/w1/anomalies.json'
import exposureJson from './__fixtures__/w1/exposure-us-tx.json'

const PRESSURE = pressureJson as unknown as PressureW1Response
const MAP = mapJson as unknown as MapRegionsW1Response
const SUPPLY = supplyJson as unknown as SupplyResponse
const ANOMALIES = anomaliesJson as unknown as AnomaliesResponse
const EXPOSURE = exposureJson as unknown as ExposureResponse
const NOW = new Date('2026-11-08T00:40:00Z')
const names = (id: string) => adaptMapRegions(MAP).get(id)?.name ?? id

describe('adaptPressure', () => {
  const p = adaptPressure(PRESSURE)
  it('maps the eight backend hubs onto design region ids, H100 SXM cell only', () => {
    expect([...p.keys()].sort()).toEqual(['de-fra', 'ie-dub', 'jp-tyo', 'no-nordics', 'sg', 'us-or', 'us-tx', 'us-va'])
    expect(p.get('us-tx')!.score).toBe(26.89)
    expect(p.get('jp-tyo')!.score).toBe(53.61)
    expect(p.get('de-fra')!.backendId).toBe('eu-de')
  })
  it('reads the comparable-ask median and premium only where the cell has asks', () => {
    const de = p.get('de-fra')!.rental!
    expect(de.median).toBe(2.4698)
    expect(de.premiumPct).toBeCloseTo(-0.03, 1)
    expect(de.freshness).toBe('live')
    expect(de.source).toMatch(/not an index/)
    expect(p.get('us-tx')!.rental).toBeNull()
  })
  it('turns factor contributions into driver shares that sum to one', () => {
    const c = p.get('us-tx')!.contrib
    expect(c.map((x) => x.factor)).toEqual(['Facility pipeline relief', 'Power-price stress', 'Demand signals (markets, feed)'])
    expect(c[0].value).toBeCloseTo(0.6, 2)
    expect(c[1].value).toBeCloseTo(0.385, 2)
    expect(c.reduce((s, x) => s + x.value, 0)).toBeCloseTo(1, 2)
  })
  it('labels factor evidence from its inputs: sample-backed factors are illustrative', () => {
    const f = p.get('us-tx')!.factors
    expect(f.find((x) => x.factor === 'capacity-pipeline')!.freshness).toBe('illustrative')
    expect(f.find((x) => x.factor === 'price-premium')!.freshness).toBe('unavailable')
  })
  it('keeps the archived score as the previous point', () => {
    expect(p.get('us-tx')!.prevScore).toBe(26.89)
    expect(p.get('jp-tyo')!.prevScore).toBeNull()
  })
})

describe('adaptMapRegions', () => {
  const m = adaptMapRegions(MAP)
  it('takes the backend region names and hub coordinates', () => {
    expect(m.get('us-va')!.name).toBe('US East and Midwest (Ashburn hub)')
    expect(m.get('us-tx')!.lat).toBe(32.78)
  })
  it('keeps the energy indicator with its own unit and provenance', () => {
    expect(m.get('de-fra')!.power).toMatchObject({ value: 223.12, unit: 'EUR/MWh', freshness: 'live', source: 'energy-charts' })
    expect(m.get('us-tx')!.power).toMatchObject({ value: 34.71, unit: 'USD/MWh', freshness: 'illustrative' })
    expect(m.get('us-va')!.power).toBeNull()
  })
})

describe('adaptSupply', () => {
  const s = adaptSupply(SUPPLY)
  it('maps operating, construction and pipeline MW', () => {
    expect(s.get('us-tx')).toMatchObject({ opMW: 500, conMW: 1000, propMW: 500, pipelineMW: 1500, effectiveMw: 1150, facilities: 9 })
  })
  it('widens the under-construction contributions by the stated completion-ceiling range', () => {
    // Two facilities each contribute 400 MW at ceiling 0.8 (range 0.5 to 1.0) of 500 MW capacity.
    expect(s.get('us-tx')!.effAdd).toEqual([500, 1000])
    expect(s.get('jp-tyo')!.effAdd).toEqual([25, 50])
  })
  it('is illustrative because the facility records are sample data', () => {
    expect(s.get('us-tx')!.freshness).toBe('illustrative')
  })
})

describe('adaptAnomalies', () => {
  const a = adaptAnomalies(ANOMALIES, names)
  it('orders open anomalies by severity, then by real-input coverage', () => {
    expect(a.cards).toHaveLength(7)
    expect(a.cards[0].region).toBe('us-tx')
    expect(a.cards[0].title).toBe('US Central: price dispersion, RTX 4090')
    expect(a.cards[1].region).toBe('de-fra')
    expect(a.cards[1].sub).toContain('coverage 5 of 6 factors · indicative')
  })
  it('gives each region its most severe anomaly in the design shape', () => {
    const tx = a.byRegion.get('us-tx')!
    expect(tx.title).toBe('Price dispersion: RTX 4090')
    expect(tx.changed).toMatch(/^RTX 4090 in US Central \(Dallas hub\): comparable asks spread 94%/)
    expect(tx.confidence).toMatchObject({ level: 'Low', score: 0.67 })
    expect(tx.affects.some((x) => x.kind === 'security' && x.link === 'modelled')).toBe(true)
    expect(a.byRegion.get('de-fra')!.confidence.level).toBe('Moderate')
    expect(a.byRegion.has('jp-tyo')).toBe(false)
  })
})

describe('adaptExposure', () => {
  it('lists no documented tie when the backend documents none, and never invents an R²', () => {
    const x = adaptExposure(EXPOSURE)
    expect(x.documented).toEqual([])
    expect(x.sensitivities[0]).toMatchObject({ beta: 0.8 })
    expect(Number.isNaN(x.sensitivities[0].r2)).toBe(true)
    expect(x.freshness).toBe('illustrative')
  })
})

describe('composeRegion', () => {
  const parts = { pressure: adaptPressure(PRESSURE), map: adaptMapRegions(MAP), supply: adaptSupply(SUPPLY), anomalies: adaptAnomalies(ANOMALIES, names) }
  const designTx = { ...regionById('us-tx')!, pressure: [...regionById('us-tx')!.pressure] }
  it('puts only the current month on the axis, and shifts the design months so they join it (G14)', () => {
    const r = composeRegion(designTx, parts, NOW)
    expect(r.pressure[NOW_IDX]).toBe(26.89) // the real cell score, untouched
    expect(r.live!.pressure!.realSlots.filter(Boolean)).toHaveLength(1)
    expect(r.live!.pressure!.scaled).toBe(true)
    const shift = 26.89 - designTx.pressure[NOW_IDX]
    expect(r.pressure[NOW_IDX - 1]).toBeCloseTo(Math.max(0, designTx.pressure[NOW_IDX - 1] + shift), 2)
    expect(r.pressure[NOW_IDX + 6]).toBeCloseTo(Math.max(0, designTx.pressure[NOW_IDX + 6] + shift), 2)
    expect(r.pressure.every((v) => v >= 0 && v <= 100)).toBe(true)
    // Continuity at the join: the step into Now is the design's own step, not the design-to-real gap.
    expect(Math.abs(r.pressure[NOW_IDX] - r.pressure[NOW_IDX - 1])).toBeCloseTo(Math.abs(designTx.pressure[NOW_IDX] - designTx.pressure[NOW_IDX - 1]), 1)
  })
  it('zeroes the design dollar power price for a covered region with no USD series once the map lands (WF3 G1)', () => {
    // us-or has no energy indicator in the recorded map payload: the design's $38 must not draw.
    const or = regionById('us-or')!
    const r = composeRegion({ ...or, pressure: [...or.pressure] }, parts, NOW)
    expect(r.live).toBeDefined()
    expect(r.power).toBe(0)
    // Without the map part nothing is known about the series yet, so the field is left alone.
    expect(composeRegion({ ...or, pressure: [...or.pressure] }, { pressure: parts.pressure }, NOW).power).toBe(or.power)
  })
  it('shows no H100 quote where the backend cell has no asks, and no 30-day change', () => {
    const r = composeRegion(designTx, parts, NOW)
    expect(r.h100).toBe(0)
    expect(Number.isNaN(r.h100Chg)).toBe(true)
    expect(r.name).toBe('US Central (Dallas hub)')
    expect(r.quality).toBe('experimental')
    expect(r.power).toBe(34.7)
    expect(r.eff27).toEqual([500, 1000])
    expect(r.announced27).toBe(1500)
  })
  it('does not write a EUR power price into the design dollar field: the dollar map shows no bar, the EUR value stays in live.power', () => {
    const de = regionById('de-fra')!
    const r = composeRegion({ ...de }, parts, NOW)
    expect(r.power).toBe(0)
    expect(r.live!.power).toMatchObject({ value: 223.12, unit: 'EUR/MWh' })
    expect(r.quality).toBe('good')
  })
  it('leaves an uncovered region as designed until the backend answers', () => {
    const az = regionById('us-az')!
    const r = composeRegion({ ...az }, {}, NOW)
    expect(r.live).toBeUndefined()
    expect(r.pressure).toEqual(az.pressure)
  })
  it('turns an uncovered region into the design no-data state once the pressure grid lands', () => {
    const r = composeRegion({ ...regionById('us-az')! }, parts, NOW)
    expect(r.quality).toBe('none')
    expect(r.live).toBeUndefined()
    expect([r.opMW, r.h100, r.power, r.pressure[NOW_IDX]]).toEqual([0, 0, 0, 0])
  })
})

describe('globalFromPressure', () => {
  it('is the equal-weight mean of the eight headline cells', () => {
    expect(globalFromPressure(adaptPressure(PRESSURE))).toEqual({ now: 37.5, prev: null, n: 8 })
  })
})

describe('regionEvidence', () => {
  it('marks every row of an uncovered region illustrative before the backend answers', () => {
    const ev = regionEvidence(regionById('us-az')!)
    expect(ev.length).toBeGreaterThan(0)
    expect(ev.every((e) => e.freshness === 'illustrative')).toBe(true)
  })
})

describe('applyW1 and the module', () => {
  it('fails cleanly when the backend is down, leaving the design values', async () => {
    const before = regionById('us-tx')!.name
    const get = makeGetter({ base: 'http://x', fetchImpl: (async () => new Response('down', { status: 502 })) as typeof fetch })
    expect(await regionsModule.load(get, () => {})).toBe(false)
    expect(regionById('us-tx')!.name).toBe(before)
  })
  it('writes in place: same objects, real global now-point, anomaly cards, evidence from the backend', async () => {
    const tx = REGIONS.find((r) => r.id === 'us-tx')!
    const designGlobal = [...GLOBAL_CPI]
    const routes: Record<string, unknown> = { '/flopx/pressure': PRESSURE, '/flopx/map/regions': MAP, '/flopx/supply': SUPPLY, '/flopx/anomalies': ANOMALIES }
    const fetchImpl = (async (url: string) => {
      const path = new URL(url).pathname + new URL(url).search
      if (path.startsWith('/flopx/exposure')) return new Response(JSON.stringify(EXPOSURE), { status: 200 })
      const body = routes[path]
      return body ? new Response(JSON.stringify(body), { status: 200 }) : new Response('nf', { status: 404 })
    }) as typeof fetch
    expect(await regionsModule.load(makeGetter({ base: 'http://x', fetchImpl }), () => {})).toBe(true)
    expect(REGIONS.find((r) => r.id === 'us-tx')).toBe(tx)
    expect(tx.name).toBe('US Central (Dallas hub)')
    expect(GLOBAL_CPI[NOW_IDX]).toBe(37.5)
    expect(PRESSURE_MODEL).toMatchObject({ method: 'cpx-v1', regions: 8, globalReal: true, globalScaled: true, anomaliesLive: true })
    // G14 on the global series: every other month is the design's, shifted by (37.5 - design Now).
    const shift = 37.5 - designGlobal[NOW_IDX]
    expect(GLOBAL_CPI[NOW_IDX - 1]).toBeCloseTo(designGlobal[NOW_IDX - 1] + shift, 1)
    expect(GLOBAL_CPI[NOW_IDX + 1]).toBeCloseTo(designGlobal[NOW_IDX + 1] + shift, 1)
    expect(GLOBAL_CPI[0]).toBeCloseTo(designGlobal[0] + shift, 1)
    expect(regionById('us-or')!.power).toBe(0) // no USD series in the map payload: no design dollar price on the map
    expect(tx.power).toBe(34.7) // ERCOT sample USD indicator, badged illustrative via live.power
    expect(ANOMALY_CARDS[0].region).toBe('us-tx')
    expect(regionById('eu-de')!.id).toBe('de-fra')
    const ev = regionEvidence(tx)
    expect(ev.find((e) => e.id === 'us-tx-g')).toMatchObject({ value: 'Not in FLOPX', freshness: 'unavailable' })
    expect(ev.find((e) => e.id === 'us-tx-c')!.freshness).toBe('illustrative')
    expect(ev.some((e) => e.label.startsWith('cpx-v1 factor'))).toBe(true)
    expect(regionById('us-az')!.quality).toBe('none')
    // Re-applying with no parts restores the design values exactly.
    applyW1({})
    expect(tx.live).toBeUndefined()
    expect(tx.power).toBe(58.4)
  })
})

// N-XRAY: the Financial exposure and Rental economics answers for every region.
describe('N-XRAY exposure ties and rental fallback', () => {
  const rentalWire = (over: Partial<NonNullable<PressureW1Response['cells'][number]['rental']>>) => ({
    basis: 'global-fallback' as const, medianUsdPerGpuHour: 3.18, premiumPct: null, premiumLabel: 'n/a (no regional asks)', askCount: 0,
    globalMedianUsdPerGpuHour: 3.18, globalAskCount: 2, weeklyMedianUsdPerGpuHour: 2.98, weeklyWeekEnding: '2026-11-02T00:00:00Z',
    source: 'flopx-sample-v1, getdeploying weekly median (CC BY 4.0, archived)', note: 'fallback: no live asks for h100-sxm-80gb in US Central (Dallas hub); shown instead: ...',
    evidence: PRESSURE.cells[0].factors[0].evidence, observedAt: '2026-11-06T21:00:00Z', ...over,
  })
  it('lists authored ties with their role and "not a filing" note, and keeps documented empty on the sample tier', () => {
    const wire: ExposureResponse = { ...EXPOSURE, exposures: EXPOSURE.exposures.map((e, i) => ({ ...e, role: 'gpu-cloud', tieKind: 'authored', tieBasis: `profile role gpu-cloud reaches every region (${i})`, tieNote: 'authored tie from company profile, not a filing', position: i === 0 ? { quantity: 10, side: 'long', avgCostUsd: 100, marketValueUsd: 1200, weight: 0.01 } : null })) }
    const x = adaptExposure(wire, () => false)
    expect(x.documented).toEqual([])
    expect(x.authored).toHaveLength(EXPOSURE.exposures.length)
    expect(x.authored[0]).toMatchObject({ t: EXPOSURE.exposures[0].ticker, role: 'gpu-cloud', note: 'authored tie from company profile, not a filing', tie: 'profile role gpu-cloud reaches every region (0)' })
    expect(x.positions.get(EXPOSURE.exposures[0].ticker)).toMatchObject({ side: 'long', quantity: 10 })
    expect(x.sensitivities).toEqual([]) // the known-ticker filter still governs the modelled list
    const docd = adaptExposure({ ...wire, exposures: [{ ...wire.exposures[0], documented: true, tieKind: 'documented', tieBasis: 'SEC filing names the region: 10-K', tieNote: null }] })
    expect(docd.documented).toEqual([{ t: wire.exposures[0].ticker, tie: 'SEC filing names the region: 10-K', source: wire.exposures[0].provenance.source }])
    expect(docd.authored).toEqual([])
  })
  it('maps the global fallback to a labelled median with no premium, regional asks to the stated premium, and none to null', () => {
    const base = PRESSURE.cells.find((c) => c.score != null)!
    const fb = adaptPressureCell({ ...base, rental: rentalWire({}) })!
    expect(fb.rental).toMatchObject({ median: 3.18, premiumPct: null, premiumLabel: 'n/a (no regional asks)', fallback: true, weekly: { median: 2.98, weekEnding: '2026-11-02T00:00:00Z' } })
    expect(fb.rental!.fallbackNote).toMatch(/^fallback: no live asks/)
    const reg = adaptPressureCell({ ...base, rental: rentalWire({ basis: 'regional-asks', medianUsdPerGpuHour: 2.77, premiumPct: -16.9, premiumLabel: '-16.9%', askCount: 2, source: 'flopx-sample-v1' }) })!
    expect(reg.rental).toMatchObject({ median: 2.77, premiumPct: -16.9, premiumLabel: '-16.9%', fallback: false })
    expect(adaptPressureCell({ ...base, rental: rentalWire({ basis: 'none', medianUsdPerGpuHour: null, globalMedianUsdPerGpuHour: null, weeklyMedianUsdPerGpuHour: null, weeklyWeekEnding: null, evidence: [] }) })!.rental).toBeNull()
  })
  it('composes the fallback median into h100 with premium 0 and no NaN anywhere', () => {
    const base = PRESSURE.cells.find((c) => c.regionId === 'us-tx' && c.gpuModelId === 'h100-sxm-80gb')!
    const parts = { pressure: adaptPressure({ ...PRESSURE, cells: PRESSURE.cells.map((c) => (c === base ? { ...c, rental: rentalWire({}) } : c)) }) }
    const r = composeRegion({ ...regionById('us-tx')!, pressure: [...regionById('us-tx')!.pressure] }, parts, NOW)
    expect(r.h100).toBe(3.18)
    expect(r.premium).toBe(0)
    expect(r.live!.rental).toMatchObject({ fallback: true, premiumPct: null })
  })
})
