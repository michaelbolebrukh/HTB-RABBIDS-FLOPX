// All figures in FLOPX mock data are ILLUSTRATIVE. They are designed to look plausible,
// not to describe real facilities, prices or company relationships.
// Live wiring (src/live/**) replaces them in place with backend values where the backend has
// them, and everything that stays synthetic carries the 'illustrative' freshness.
import { axisAnchor, axisLabels, AXIS_NOW_IDX } from '@/live/dates'
import { fmtAsOf, fmtDay } from '@/live/provenance'
import { liveRegionName, liveRegionShort } from '@/live/regions'
import { fmtDM, lastTradingClose } from '@/lib/designDates'

/** The stamp the design's placeholder evidence rows carry: the last completed trading close ("06 Nov 21:00"), never a clock time later today. */
export const designStamp = (): string => `${fmtDM(lastTradingClose())} 21:00`

export function rng(seed: number) {
  let a = seed >>> 0
  return () => {
    a |= 0
    a = (a + 0x6d2b79f5) | 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

// AS_OF is the real generated time: the newest backend freshness.generatedAt that has landed
// (src/live/asof.ts). Until the first payload lands it reads the client clock. `export let` is a
// live binding, so importers see the update on their next render.
export let AS_OF = fmtAsOf(axisAnchor())
export let AS_OF_SHORT = fmtDay(axisAnchor())
export function setAsOf(d: Date) {
  AS_OF = fmtAsOf(d)
  AS_OF_SHORT = fmtDay(d)
}

// Monthly axis: 39 slots with NOW_IDX (slot 26) on the real current UTC month (see
// src/live/dates.ts and src/live/README.md). The design's synthetic series keep their slot
// positions; only the labels follow the calendar.
export const MONTHS: string[] = axisLabels(axisAnchor())
export const NOW_IDX = AXIS_NOW_IDX
/**
 * The month label `months` ahead of now on the axis ("Nov 27" for +12 on a November anchor). Read at
 * render time, never captured in a constant: MONTHS is rewritten in place when the anchor lands. The
 * design's hard-coded "Mar 27" horizons all read this (WF5 #5).
 */
export const horizonLabel = (months: number): string => MONTHS[Math.max(0, Math.min(MONTHS.length - 1, NOW_IDX + months))]
/** Re-label MONTHS in place (same array, so every importer sees it). */
export function relabelAxis(now: Date) {
  const next = axisLabels(now)
  next.forEach((m, i) => (MONTHS[i] = m))
}
export const HORIZONS = [
  { id: '1M', months: 1 },
  { id: '3M', months: 3 },
  { id: '6M', months: 6 },
  { id: '12M', months: 12 },
] as const

/** Piecewise-linear interpolation through keypoints at given indices with seeded noise. */
export function shaped(seed: number, keys: [number, number][], len = 39, noise = 1.5): number[] {
  const r = rng(seed)
  const out: number[] = []
  for (let i = 0; i < len; i++) {
    let k = 0
    while (k < keys.length - 2 && i > keys[k + 1][0]) k++
    const [x0, y0] = keys[k]
    const [x1, y1] = keys[k + 1]
    const t = x1 === x0 ? 0 : Math.min(1, Math.max(0, (i - x0) / (x1 - x0)))
    const base = y0 + (y1 - y0) * t
    const n = i > NOW_IDX ? 0 : (r() - 0.5) * 2 * noise
    out.push(+(base + n).toFixed(2))
  }
  return out
}

/** Forecast band: widens with horizon from NOW_IDX. */
export function band(values: number[], unc: number, growth = 0.9): { lo: (number | null)[]; hi: (number | null)[] } {
  const lo: (number | null)[] = []
  const hi: (number | null)[] = []
  values.forEach((v, i) => {
    if (i < NOW_IDX) {
      lo.push(null)
      hi.push(null)
    } else {
      const w = unc + growth * unc * Math.sqrt(i - NOW_IDX)
      lo.push(+(v - w).toFixed(2))
      hi.push(+(v + w).toFixed(2))
    }
  })
  return { lo, hi }
}

export type Truth = 'obs' | 'inf' | 'fct' | 'dec'
export type Freshness = 'live' | 'delayed' | 'historical' | 'stale' | 'unavailable' | 'experimental' | 'simulated' | 'illustrative'
export type Quality = 'good' | 'stale' | 'sparse' | 'none' | 'experimental'

export interface Evidence {
  id: string
  label: string
  value: string
  truth: Truth
  source: string
  time: string
  freshness: Freshness
  weight: number // contribution weight in the inference, 0–1
  direction: 'up' | 'down' | 'flat'
  conflict?: boolean
  note?: string
}

export interface Anomaly {
  title: string
  detected: string
  changed: string
  changedPoints: string[]
  why: { text: string; support: 'strong' | 'moderate' | 'weak' }[]
  confidence: { level: 'High' | 'Moderate' | 'Low'; score: number; notes: string[] }
  affects: { kind: EntityKind; id: string; label: string; note: string; link: 'documented' | 'modelled' }[]
  conflicting?: string
}

export type EntityKind = 'region' | 'gpu' | 'provider' | 'project' | 'energy' | 'security' | 'basket' | 'signal' | 'news' | 'theme'

export interface Region {
  id: string
  name: string
  short: string
  country: string
  lat: number
  lon: number
  grid: string // energy market id
  marketplace: string
  quality: Quality
  pressure: number[] // inferred Compute Pressure Index 0–100 (monthly)
  unc: number
  h100: number // observed median $/GPU-hr on-demand
  h100Chg: number // 30d %
  premium: number // vs global median, %
  power: number // $/MWh, day-ahead avg 30d
  queueGW: number
  opMW: number
  conMW: number
  propMW: number
  announced27: number
  eff27: [number, number] // inferred effective MW online by Mar 2027
  documented: { t: string; tie: string; source: string }[]
  sensitivities: { t: string; beta: number; r2: number }[]
  contrib: { factor: string; value: number }[]
  anomaly?: Anomaly
  extraEvidence?: Evidence[]
  note?: string
  slipSensitivity: number // MW of 2027 effective supply dependent on pending grid upgrade
  /** Set by src/live/adapt-w1.ts when the FLOPX backend covers this region; absent means every number above is illustrative. */
  live?: RegionLive
}

/**
 * What the FLOPX backend supplied for a design region, and with which provenance. Fields the
 * backend did not supply stay design values and read as illustrative (see src/live/adapt-w1.ts).
 */
export interface RegionLive {
  backendId: string
  asOf: string
  /** cpx-v1 cell used as the region's headline pressure (the H100 SXM cell). */
  pressure: {
    gpu: string
    score: number
    band: string
    label: 'exploratory' | 'indicative' | string
    coverage: number
    factorsWithData: number
    factorsTotal: number
    sampleShare: number
    freshness: Freshness
    /** Real slots on the month axis (placeOnAxis); the rest of `pressure` is illustrative. */
    realSlots: boolean[]
    /** True when the illustrative slots were shifted to join the real present (G14, joinToNow). */
    scaled: boolean
    /** Per-factor notes from the backend, for the evidence tab. */
    factors: { factor: string; note: string; contribution: number | null; weight: number; freshness: Freshness }[]
  } | null
  /** H100 comparable-ask median and premium from the cell's price-premium factor, when the cell has asks. */
  rental: { median: number | null; premiumPct: number; freshness: Freshness; source: string; observedAt: string | null } | null
  /** Region's latest energy indicator (may be non-USD; then `power` above is left illustrative). */
  power: { value: number; unit: string; freshness: Freshness; source: string; observedAt: string | null } | null
  /** Effective supply (effective-supply-v1) over the backend's facility records. */
  supply: { freshness: Freshness; facilities: number; effectiveMw: number; method: string } | null
  /** Companies linked to the region (exposure-v1). */
  exposure: { freshness: Freshness } | null
}

/** Backend hub id -> design region id. Unlisted design regions have no backend coverage. */
export const BACKEND_REGION_IDS: Readonly<Record<string, string>> = {
  'us-tx': 'us-tx',
  'us-va': 'us-va',
  'us-or': 'us-or',
  'eu-ie': 'ie-dub',
  'eu-de': 'de-fra',
  'eu-no': 'no-nordics',
  'ap-sg': 'sg',
  'ap-jp': 'jp-tyo',
}
/** Design region id for a backend region id (identity when unmapped). Offer adapters can normalise with it. */
export const designRegionId = (backendId: string): string => BACKEND_REGION_IDS[backendId] ?? backendId

/** A backend anomaly as the X-Ray's global panel lists it (src/live/adapt-w1.ts fills this). */
export interface AnomalyCard {
  id: string
  region: string
  title: string
  sub: string
  severity: number
}
/** Open anomalies from /flopx/anomalies, most severe first. Empty until the backend answers. */
export const ANOMALY_CARDS: AnomalyCard[] = []

/** The pressure model's identity and the time it was computed; filled from /flopx/pressure. */
export const PRESSURE_MODEL: { method: string | null; asOf: string | null; regions: number; globalReal: boolean; /** GLOBAL_CPI's illustrative months were shifted to join the real present (G14). */ globalScaled: boolean; globalPrev: number | null; anomaliesLive: boolean } = { method: null, asOf: null, regions: 0, globalReal: false, globalScaled: false, globalPrev: null, anomaliesLive: false }

const R = (
  id: string,
  name: string,
  short: string,
  country: string,
  lat: number,
  lon: number,
  grid: string,
  marketplace: string,
  quality: Quality,
  keys: [number, number][],
  unc: number,
  rest: Omit<Region, 'id' | 'name' | 'short' | 'country' | 'lat' | 'lon' | 'grid' | 'marketplace' | 'quality' | 'pressure' | 'unc'>,
  seed: number,
): Region => ({ id, name, short, country, lat, lon, grid, marketplace, quality, pressure: shaped(seed, keys, 39, unc * 0.35), unc, ...rest })

export const REGIONS: Region[] = [
  R('us-tx', 'Texas (Dallas–Abilene)', 'TX', 'United States', 32.6, -97.6, 'ercot', 'US-Central', 'good',
    [[0, 52], [12, 58], [22, 63], [24, 68], [26, 82], [32, 79], [38, 71]], 6,
    {
      h100: 2.71, h100Chg: 11.8, premium: 17, power: 58.4, queueGW: 182, opMW: 2140, conMW: 3900, propMW: 6200, announced27: 4800, eff27: [2350, 3100],
      slipSensitivity: 880,
      documented: [
        { t: 'CRWV', tie: 'Leases 2 campuses in DFW metro (illustrative filing ref.)', source: '10-K FY25 · p.41' },
        { t: 'VST', tie: 'Generation fleet concentrated in ERCOT', source: 'Investor deck Q4-25' },
        { t: 'APLD', tie: 'Ellendale/Abilene HPC campus developer', source: 'Press release 08 Jan 26' },
      ],
      sensitivities: [
        { t: 'VST', beta: 0.42, r2: 0.18 },
        { t: 'CRWV', beta: 0.31, r2: 0.09 },
        { t: 'NRG', beta: 0.27, r2: 0.11 },
      ],
      contrib: [
        { factor: 'H100 rental premium vs global', value: 0.34 },
        { factor: 'Sold-out rate on marketplaces', value: 0.22 },
        { factor: 'ERCOT scarcity pricing hours', value: 0.18 },
        { factor: 'Interconnect queue slippage', value: 0.16 },
        { factor: 'Pipeline energisation lag', value: 0.1 },
      ],
      anomaly: {
        title: 'Pressure jump +14 pts in 6 weeks',
        detected: '27 Feb 2026',
        changed: 'The inferred Compute Pressure Index for Texas rose from 68 to 82 between late January and early March — the largest 6-week move of any region since FLOPX coverage began.',
        changedPoints: [
          'Median H100 on-demand quote up 11.8% in 30d ($2.42 → $2.71/GPU-hr)',
          'Marketplace sold-out rate for 8×H100 nodes up from 18% to 41%',
          '1 large campus (Red Plains Ph.2) reported energisation pushed from Q2 to Q4 2026',
          'ERCOT real-time prices cleared above $200/MWh in 37 hours over 30d (vs 9 prior)',
        ],
        why: [
          { text: 'Announced capacity is arriving later than expected: ~600 MW of Q2 energisations now look like Q4.', support: 'strong' },
          { text: 'Training demand from two frontier labs appears to be concentrating in DFW after a regional procurement round (news-derived, unconfirmed).', support: 'moderate' },
          { text: 'Late-winter grid tightness raised the cost of interruptible load, discouraging spot capacity.', support: 'weak' },
        ],
        confidence: {
          level: 'Moderate',
          score: 0.64,
          notes: [
            '5 independent observation types agree on direction',
            'Sold-out rate is from 3 marketplaces only; hyperscaler capacity is not observable',
            'Utilisation is NOT measured — inferred from price and availability',
          ],
        },
        affects: [
          { kind: 'security', id: 'VST', label: 'Vistra (VST)', note: 'Modelled sensitivity β 0.42 to TX pressure', link: 'modelled' },
          { kind: 'security', id: 'CRWV', label: 'CoreWeave (CRWV)', note: 'Documented DFW facilities; pricing power ↑, delivery risk ↑', link: 'documented' },
          { kind: 'gpu', id: 'h100', label: 'H100 SXM rental', note: 'US-Central premium widening', link: 'modelled' },
          { kind: 'signal', id: 'sig-ercot', label: 'Signal: ERCOT power for AI', note: 'Raised CPX-1 target weight in VST', link: 'modelled' },
          { kind: 'project', id: 'red-plains', label: 'Red Plains Campus Ph.2', note: 'Delay is a primary driver', link: 'documented' },
        ],
        conflicting: 'Vast.ai-style marketplace listings in Texas actually grew 6% — but most new listings are consumer-grade GPUs that do not substitute for 8×H100 training nodes.',
      },
      extraEvidence: [
        { id: 'e-tx-1', label: 'Red Plains Ph.2 energisation', value: 'Q2 → Q4 2026', truth: 'obs', source: 'County permit filing + developer call', time: '24 Feb 2026', freshness: 'historical', weight: 0.16, direction: 'up' },
        { id: 'e-tx-2', label: 'Marketplace listings (all GPUs)', value: '+6.1% 30d', truth: 'obs', source: 'Marketplace scrape (3 venues)', time: '14 Mar 2026 09:30', freshness: 'live', weight: 0.04, direction: 'down', conflict: true, note: 'Mostly consumer-grade cards' },
      ],
      note: 'Highest-conviction anomaly this week.',
    }, 11),
  R('us-va', 'Northern Virginia', 'NoVA', 'United States', 39.0, -77.5, 'pjm', 'US-East', 'good',
    [[0, 70], [12, 74], [22, 76], [26, 78], [38, 80]], 4,
    {
      h100: 2.58, h100Chg: 3.1, premium: 11, power: 71.2, queueGW: 96, opMW: 4900, conMW: 2600, propMW: 5400, announced27: 2900, eff27: [1500, 2100], slipSensitivity: 640,
      documented: [
        { t: 'EQIX', tie: 'Ashburn DC campus cluster', source: '10-K FY25' },
        { t: 'DLR', tie: 'Largest market by MW under lease', source: 'Supplemental Q4-25' },
        { t: 'AMZN', tie: 'us-east-1 region (public)', source: 'Public cloud docs' },
      ],
      sensitivities: [
        { t: 'DLR', beta: 0.36, r2: 0.14 },
        { t: 'EQIX', beta: 0.22, r2: 0.08 },
      ],
      contrib: [
        { factor: 'Transmission constraint (Dominion zone)', value: 0.38 },
        { factor: 'H100 rental premium vs global', value: 0.21 },
        { factor: 'Colocation vacancy (reported)', value: 0.24 },
        { factor: 'Sold-out rate', value: 0.17 },
      ],
    }, 12),
  R('us-az', 'Phoenix', 'PHX', 'United States', 33.4, -112.0, 'wecc-az', 'US-West', 'good',
    [[0, 54], [12, 60], [26, 69], [38, 66]], 5,
    { h100: 2.49, h100Chg: 4.2, premium: 7, power: 49.8, queueGW: 61, opMW: 1300, conMW: 1700, propMW: 2600, announced27: 1600, eff27: [1000, 1400], slipSensitivity: 300, documented: [{ t: 'TSM', tie: 'Fab 21 (chips, not compute)', source: 'Company site' }], sensitivities: [{ t: 'DLR', beta: 0.09, r2: 0.03 }], contrib: [{ factor: 'Water/cooling permitting', value: 0.3 }, { factor: 'Rental premium', value: 0.3 }, { factor: 'Pipeline lag', value: 0.4 }] }, 13),
  R('us-or', 'Pacific Northwest', 'PNW', 'United States', 45.6, -121.2, 'bpa', 'US-West', 'good',
    [[0, 49], [12, 53], [26, 58], [38, 61]], 5,
    { h100: 2.39, h100Chg: 1.4, premium: 3, power: 38.1, queueGW: 34, opMW: 1800, conMW: 700, propMW: 1100, announced27: 600, eff27: [420, 610], slipSensitivity: 120, documented: [{ t: 'GOOGL', tie: 'The Dalles campus (public)', source: 'Company site' }], sensitivities: [], contrib: [{ factor: 'Hydro availability', value: 0.4 }, { factor: 'Rental premium', value: 0.3 }, { factor: 'Queue', value: 0.3 }] }, 14),
  R('ca-qc', 'Québec', 'QC', 'Canada', 46.8, -71.2, 'hq', 'CA-East', 'good',
    [[0, 40], [12, 42], [26, 45], [38, 47]], 5,
    { h100: 2.21, h100Chg: -1.8, premium: -5, power: 31.0, queueGW: 12, opMW: 600, conMW: 350, propMW: 900, announced27: 300, eff27: [210, 300], slipSensitivity: 60, documented: [], sensitivities: [], contrib: [{ factor: 'Hydro surplus', value: 0.5 }, { factor: 'Rental discount', value: 0.5 }] }, 15),
  R('ie-dub', 'Dublin', 'DUB', 'Ireland', 53.35, -6.26, 'sem', 'EU-West', 'good',
    [[0, 66], [12, 69], [26, 70], [38, 72]], 5,
    { h100: 2.66, h100Chg: 0.6, premium: 14, power: 112.0, queueGW: 8, opMW: 1300, conMW: 180, propMW: 800, announced27: 200, eff27: [60, 140], slipSensitivity: 90, documented: [{ t: 'MSFT', tie: 'Azure North Europe (public)', source: 'Public cloud docs' }], sensitivities: [], contrib: [{ factor: 'Grid connection moratorium', value: 0.55 }, { factor: 'Power price', value: 0.25 }, { factor: 'Rental premium', value: 0.2 }], note: 'De facto moratorium on new Dublin grid connections.' }, 16),
  R('uk-lon', 'London / Slough', 'LON', 'United Kingdom', 51.5, -0.6, 'gb', 'EU-West', 'good',
    [[0, 58], [12, 61], [26, 64], [38, 66]], 5,
    { h100: 2.62, h100Chg: 2.0, premium: 12, power: 96.5, queueGW: 41, opMW: 1100, conMW: 420, propMW: 1900, announced27: 500, eff27: [260, 400], slipSensitivity: 150, documented: [{ t: 'NBIS', tie: 'UK cluster (announced)', source: 'Press release' }], sensitivities: [], contrib: [{ factor: 'West London grid limits', value: 0.45 }, { factor: 'Rental premium', value: 0.3 }, { factor: 'Power price', value: 0.25 }] }, 17),
  R('nl-ams', 'Amsterdam', 'AMS', 'Netherlands', 52.37, 4.9, 'nl', 'EU-West', 'good',
    [[0, 62], [12, 64], [26, 66], [38, 65]], 5,
    { h100: 2.55, h100Chg: -0.4, premium: 9, power: 84.3, queueGW: 19, opMW: 900, conMW: 150, propMW: 300, announced27: 150, eff27: [90, 140], slipSensitivity: 40, documented: [{ t: 'EQIX', tie: 'AM campus', source: '10-K FY25' }], sensitivities: [], contrib: [{ factor: 'Municipal build limits', value: 0.5 }, { factor: 'Power price', value: 0.3 }, { factor: 'Rental premium', value: 0.2 }] }, 18),
  R('de-fra', 'Frankfurt', 'FRA', 'Germany', 50.1, 8.68, 'de', 'EU-Central', 'good',
    [[0, 57], [12, 60], [26, 63], [38, 64]], 5,
    { h100: 2.57, h100Chg: 1.1, premium: 10, power: 88.9, queueGW: 27, opMW: 1000, conMW: 380, propMW: 900, announced27: 450, eff27: [280, 400], slipSensitivity: 100, documented: [{ t: 'DLR', tie: 'Interxion FRA campus', source: 'Supplemental Q4-25' }], sensitivities: [], contrib: [{ factor: 'Grid connection wait', value: 0.4 }, { factor: 'Power price', value: 0.35 }, { factor: 'Rental premium', value: 0.25 }] }, 19),
  R('no-nordics', 'Nordics (Norway–Sweden)', 'NORD', 'Norway', 61.0, 10.5, 'nordpool', 'EU-North', 'good',
    [[0, 48], [12, 46], [22, 44], [26, 41], [38, 37]], 5,
    {
      h100: 2.09, h100Chg: -6.4, premium: -10, power: 29.7, queueGW: 22, opMW: 780, conMW: 900, propMW: 1400, announced27: 820, eff27: [700, 880], slipSensitivity: 80,
      documented: [{ t: 'NBIS', tie: 'Finland/Nordic cluster (public)', source: 'Company site' }, { t: 'IREN', tie: 'No Nordic sites (none documented)', source: '—' }],
      sensitivities: [{ t: 'NBIS', beta: -0.12, r2: 0.04 }],
      contrib: [{ factor: 'New supply energised on time', value: 0.45 }, { factor: 'Rental discount', value: 0.35 }, { factor: 'Low hydro prices', value: 0.2 }],
      note: 'Loosening: supply arriving faster than demand.',
    }, 20),
  R('sg', 'Singapore–Johor', 'SG', 'Singapore', 1.35, 103.82, 'sg', 'APAC-SE', 'good',
    [[0, 68], [12, 71], [26, 74], [38, 70]], 5,
    { h100: 2.84, h100Chg: 2.6, premium: 22, power: 132.0, queueGW: 6, opMW: 1400, conMW: 1600, propMW: 2400, announced27: 1300, eff27: [800, 1150], slipSensitivity: 220, documented: [{ t: 'EQIX', tie: 'SG campus', source: '10-K FY25' }], sensitivities: [{ t: 'EQIX', beta: 0.12, r2: 0.04 }], contrib: [{ factor: 'SG moratorium; Johor spillover', value: 0.5 }, { factor: 'Rental premium', value: 0.3 }, { factor: 'Power price', value: 0.2 }] }, 21),
  R('jp-tyo', 'Tokyo', 'TYO', 'Japan', 35.68, 139.7, 'jepx', 'APAC-NE', 'good',
    [[0, 55], [12, 58], [26, 61], [38, 63]], 5,
    { h100: 2.68, h100Chg: 1.9, premium: 16, power: 101.0, queueGW: 14, opMW: 1100, conMW: 500, propMW: 1100, announced27: 520, eff27: [320, 470], slipSensitivity: 90, documented: [], sensitivities: [], contrib: [{ factor: 'Rental premium', value: 0.5 }, { factor: 'Power price', value: 0.5 }] }, 22),
  R('in-mum', 'Mumbai', 'BOM', 'India', 19.07, 72.88, 'in', 'APAC-S', 'sparse',
    [[0, 45], [12, 48], [26, 52], [38, 57]], 11,
    { h100: 2.31, h100Chg: 0.0, premium: 0, power: 68.0, queueGW: 0, opMW: 900, conMW: 800, propMW: 2000, announced27: 700, eff27: [250, 650], slipSensitivity: 150, documented: [], sensitivities: [], contrib: [{ factor: 'Rental premium (2 providers)', value: 0.6 }, { factor: 'Pipeline (press only)', value: 0.4 }], note: 'Sparse: only 2 providers report quotes; no grid queue data.' }, 23),
  R('ae-uae', 'UAE (Abu Dhabi)', 'UAE', 'UAE', 24.45, 54.38, 'uae', 'MEA', 'experimental',
    [[0, 38], [12, 42], [26, 48], [38, 56]], 12,
    { h100: 2.45, h100Chg: 3.4, premium: 6, power: 0, queueGW: 0, opMW: 300, conMW: 1000, propMW: 5000, announced27: 1000, eff27: [200, 800], slipSensitivity: 300, documented: [], sensitivities: [], contrib: [{ factor: 'Announced pipeline', value: 0.7 }, { factor: 'Rental premium', value: 0.3 }], note: 'Experimental coverage: no public power market; pipeline from announcements only.' }, 24),
  R('au-syd', 'Sydney', 'SYD', 'Australia', -33.87, 151.2, 'aemo', 'APAC-OC', 'stale',
    [[0, 50], [12, 52], [26, 55], [38, 56]], 7,
    { h100: 2.74, h100Chg: 0, premium: 18, power: 92.0, queueGW: 18, opMW: 700, conMW: 400, propMW: 900, announced27: 380, eff27: [220, 350], slipSensitivity: 70, documented: [], sensitivities: [], contrib: [{ factor: 'Rental premium', value: 0.5 }, { factor: 'Power price', value: 0.5 }], note: 'Rental quote feed stale since 06 Mar 2026 (provider API change).' }, 25),
  R('br-sao', 'São Paulo', 'GRU', 'Brazil', -23.55, -46.63, 'br', 'LATAM', 'sparse',
    [[0, 44], [12, 45], [26, 47], [38, 49]], 13,
    { h100: 2.95, h100Chg: 0, premium: 28, power: 0, queueGW: 0, opMW: 450, conMW: 200, propMW: 600, announced27: 180, eff27: [60, 170], slipSensitivity: 40, documented: [], sensitivities: [], contrib: [{ factor: 'Rental premium (1 provider)', value: 1 }], note: 'Sparse: 1 provider; quote may not be comparable (no InfiniBand).' }, 26),
  R('za-jnb', 'Johannesburg', 'JNB', 'South Africa', -26.2, 28.04, 'za', 'MEA', 'none',
    [[0, 0], [38, 0]], 0,
    { h100: 0, h100Chg: 0, premium: 0, power: 0, queueGW: 0, opMW: 250, conMW: 100, propMW: 300, announced27: 100, eff27: [0, 0], slipSensitivity: 0, documented: [], sensitivities: [], contrib: [], note: 'No usable rental quotes or market data. FLOPX does not estimate pressure here.' }, 27),
]

const regionStubs = new Map<string, Region>()
/**
 * Look up a design region. Live offers carry backend region ids (country codes such as 'tw',
 * backend hubs such as 'eu-de', or 'global') that the design list does not have; for those this
 * returns a stub region named from the backend (src/live/regions.ts) with quality 'none' and no
 * numbers, so `regionById(o.region)!` never crashes and never shows an invented figure.
 */
export const regionById = (id: string): Region | undefined => {
  const r = REGIONS.find((x) => x.id === id) ?? (id in BACKEND_REGION_IDS ? REGIONS.find((x) => x.id === BACKEND_REGION_IDS[id]) : undefined)
  if (r) return r
  if (!id) return undefined
  let stub = regionStubs.get(id)
  const name = liveRegionName(id)
  if (!stub || stub.name !== name) {
    stub = { id, name, short: liveRegionShort(id), country: name, lat: 0, lon: 0, grid: '', marketplace: '', quality: 'none', pressure: Array(39).fill(0), unc: 0, h100: 0, h100Chg: 0, premium: 0, power: 0, queueGW: 0, opMW: 0, conMW: 0, propMW: 0, announced27: 0, eff27: [0, 0], documented: [], sensitivities: [], contrib: [], slipSensitivity: 0, note: 'Region from a live offer; FLOPX has no regional model here.' }
    regionStubs.set(id, stub)
  }
  return stub
}

export function regionEvidence(r: Region): Evidence[] {
  if (r.quality === 'none') return []
  if (r.live) return liveRegionEvidence(r, r.live)
  // No FLOPX coverage: the design's evidence rows stay, every one marked illustrative.
  return designRegionEvidence(r).map((e) => ({ ...e, freshness: 'illustrative' as const, time: 'illustrative', source: `${e.source} (design placeholder)` }))
}

/** Evidence rows composed only from what the backend supplied for the region (see RegionLive). */
function liveRegionEvidence(r: Region, L: RegionLive): Evidence[] {
  const at = fmtAsOf(L.asOf)
  const w = (f: string) => L.pressure?.factors.find((x) => x.factor === f)?.weight ?? 0
  const rows: Evidence[] = [
    L.rental && L.rental.median != null
      ? { id: `${r.id}-q`, label: 'Median comparable H100 SXM ask', value: `$${L.rental.median.toFixed(2)}/GPU-hr`, truth: 'obs', source: L.rental.source, time: L.rental.observedAt ? fmtAsOf(L.rental.observedAt) : at, freshness: L.rental.freshness, weight: w('price-premium'), direction: L.rental.premiumPct > 1 ? 'up' : L.rental.premiumPct < -1 ? 'down' : 'flat', note: `${L.rental.premiumPct >= 0 ? '+' : ''}${L.rental.premiumPct.toFixed(1)}% vs global median; asks, not transactions` }
      : { id: `${r.id}-q`, label: 'Median comparable H100 SXM ask', value: 'No asks in this cell', truth: 'obs', source: 'FLOPX offers', time: at, freshness: 'unavailable', weight: w('price-premium'), direction: 'flat' },
    L.power
      ? { id: `${r.id}-p`, label: 'Day-ahead power price (latest slot)', value: `${L.power.value.toFixed(2)} ${L.power.unit}`, truth: 'obs', source: L.power.source, time: L.power.observedAt ? fmtAsOf(L.power.observedAt) : at, freshness: L.power.freshness, weight: w('power-stress'), direction: 'flat' }
      : { id: `${r.id}-p`, label: 'Day-ahead power price', value: 'No series in FLOPX', truth: 'obs', source: `grid zone ${r.grid.toUpperCase()}`, time: at, freshness: 'unavailable', weight: w('power-stress'), direction: 'flat' },
    { id: `${r.id}-g`, label: 'Interconnection queue (large load)', value: 'Not in FLOPX', truth: 'obs', source: 'n/a', time: at, freshness: 'unavailable', weight: 0, direction: 'flat' },
  ]
  if (L.supply) {
    rows.push(
      { id: `${r.id}-c`, label: 'Capacity: operating / under construction', value: `${r.opMW.toLocaleString()} / ${r.conMW.toLocaleString()} MW`, truth: 'obs', source: `FLOPX facility records (${L.supply.facilities})`, time: at, freshness: L.supply.freshness, weight: w('capacity-pipeline'), direction: 'flat', note: 'Reported facility power, not compute delivered' },
      { id: `${r.id}-u`, label: 'Effective capacity', value: `${r.eff27[0].toLocaleString()} to ${r.eff27[1].toLocaleString()} MW`, truth: 'inf', source: `FLOPX ${L.supply.method}`, time: at, freshness: L.supply.freshness, weight: w('capacity-pipeline'), direction: 'flat', note: `point estimate ${L.supply.effectiveMw.toLocaleString()} MW; range from the stated assumption ranges` },
    )
  }
  return [...rows, ...(r.extraEvidence ?? [])]
}

function designRegionEvidence(r: Region): Evidence[] {
  const stale = r.quality === 'stale'
  // The design's placeholder rows were stamped around its own now (14 Mar 2026). They are
  // illustrative, so their stamps keep the design's offsets from the real anchor (regionEvidence
  // overwrites `time` with 'illustrative' for uncovered regions anyway) and the horizon label
  // follows the month axis: +12M, not a hard-coded "Mar 27".
  // Stamps sit on the last completed trading close, never a clock time later today (WF5 #10).
  const d0 = designStamp()
  const base: Evidence[] = [
    { id: `${r.id}-q`, label: 'Median H100 SXM on-demand quote', value: `$${r.h100.toFixed(2)}/GPU-hr`, truth: 'obs', source: r.quality === 'sparse' ? 'Provider APIs (sparse)' : 'Provider APIs + marketplace scrape (9 sources)', time: stale ? `${MONTHS[NOW_IDX]} (stale)` : d0, freshness: stale ? 'stale' : 'live', weight: 0.3, direction: r.h100Chg > 1 ? 'up' : r.h100Chg < -1 ? 'down' : 'flat' },
    { id: `${r.id}-p`, label: 'Day-ahead power price (30d avg)', value: r.power ? `$${r.power.toFixed(1)}/MWh` : 'No public market', truth: 'obs', source: r.power ? `${r.grid.toUpperCase()} market data` : '—', time: r.power ? d0 : '—', freshness: r.power ? 'delayed' : 'unavailable', weight: 0.2, direction: 'flat' },
    { id: `${r.id}-g`, label: 'Interconnection queue (large load)', value: r.queueGW ? `${r.queueGW} GW` : 'Not reported', truth: 'obs', source: r.queueGW ? 'Grid operator queue report' : '—', time: r.queueGW ? MONTHS[NOW_IDX] : '—', freshness: r.queueGW ? 'historical' : 'unavailable', weight: 0.15, direction: 'up' },
    { id: `${r.id}-c`, label: 'Capacity: operational / construction', value: `${r.opMW.toLocaleString()} / ${r.conMW.toLocaleString()} MW`, truth: 'obs', source: 'Project tracker (permits, filings, press)', time: MONTHS[NOW_IDX], freshness: 'historical', weight: 0.15, direction: 'flat', note: 'Reported facility power, not compute delivered' },
    { id: `${r.id}-u`, label: `Effective capacity online by ${MONTHS[NOW_IDX + 12]}`, value: `${r.eff27[0].toLocaleString()}–${r.eff27[1].toLocaleString()} MW`, truth: 'inf', source: 'FLOPX supply model v0.7', time: d0, freshness: 'experimental', weight: 0.2, direction: 'flat', note: `vs ${r.announced27.toLocaleString()} MW announced` },
  ]
  return [...base, ...(r.extraEvidence ?? [])]
}

/** Global FLOPX Compute Pressure Index: weighted mean of covered regions. */
export const GLOBAL_CPI: number[] = MONTHS.map((_, i) => {
  const covered = REGIONS.filter((r) => r.quality !== 'none')
  const w = covered.reduce((s, r) => s + r.opMW, 0)
  return +(covered.reduce((s, r) => s + r.pressure[i] * r.opMW, 0) / w).toFixed(1)
})

// ---------- GPU rental markets ----------
export interface Gpu {
  id: string
  name: string
  arch: string
  memory: string
  index: number[] // observed monthly $/GPU-hr index (median on-demand)
  unc: number
  pressure: number
  /** Set by src/live/gpu.ts when the backend index lands; absent means every value is illustrative. */
  live?: GpuLive
  /** Set when `pressure` is the median of the backend's regional cpx scores for this GPU. */
  pressureLive?: { cells: number; method: string; asOf: string }
}

export interface GpuLive {
  /** Backend gpuModelId, e.g. 'h100-sxm-80gb'. */
  modelId: string
  seriesId: string
  /** Latest index value, USD/GPU-hour. */
  current: number
  freshness: Freshness
  freshLabel?: string
  /** Formatted observedAt of the latest value. */
  observed: string
  /** Slots of `index` that carry a real value; the rest are the design's illustrative values, scaled to join them (G14). */
  real: boolean[]
  /** True when illustrative slots of `index` were multiplied to join the real points. */
  scaled: boolean
  /** Factor applied to the forecast slots (and to a band width drawn with them); 1 when nothing was scaled. */
  bandFactor: number
  /** The untouched design series. */
  designIndex: number[]
  /** Change between the first and last real point, %; null when only one point exists. */
  chgPct: number | null
  /** What chgPct spans, e.g. "since 08 Nov 00:09". */
  chgWindow: string
  coverage: { providers: number; regions: number; offerCount: number }
  basket: string[]
  limitations: string[]
  methodology: string
  historyPoints: number
  /**
   * Third-party price history drawn as the chart's past months (set by src/live/gpu.ts when the
   * backend serves a GetDeploying weekly-median series for this model). `slots` marks the axis
   * slots whose value is a monthly mean of those weeks; they are real and archived, not FLOPX's own.
   */
  history?: GpuHistory
  /** The FLOPX index's own values on the month axis, drawn as separate marks (null where it has no point). */
  indexMarks?: (number | null)[]
  /** The 30-day change shown on cards: FLOPX's own when its index spans 30 days, else GetDeploying's; null when neither exists. */
  chg30?: GpuChange30 | null
}

export interface GpuHistory {
  source: 'getdeploying'
  seriesId: string
  licence: string
  /** The attribution line CC BY 4.0 asks for, shown once where the series is drawn. */
  attribution: string
  sourceUrl: string
  /** Weekly points used (each week stamped at its end). */
  weeks: number
  /** Axis slots filled from this series. */
  slots: boolean[]
  /** Monthly means behind those slots. */
  monthly: { slot: number; mean: number; weeks: number }[]
  weekly: { t: string; value: number }[]
  /** Formatted first and last week-end stamps. */
  first: string
  last: string
}

export interface GpuChange30 {
  pct: number
  source: 'flopx' | 'getdeploying'
  /** The label drawn beside the delta, e.g. "30d · GetDeploying". */
  label: string
  from: string
  to: string
  spanDays: number
}

export const GPUS: Gpu[] = [
  { id: 'b200', name: 'B200', arch: 'Blackwell', memory: '192 GB HBM3e', index: shaped(31, [[0, 0], [14, 0], [15, 7.2], [20, 5.6], [26, 4.85], [38, 3.9]], 39, 0.12).map((v, i) => (i < 15 ? NaN : v)), unc: 0.35, pressure: 76 },
  { id: 'h200', name: 'H200', arch: 'Hopper', memory: '141 GB HBM3e', index: shaped(32, [[0, 0], [8, 0], [9, 4.6], [20, 3.4], [26, 3.18], [38, 2.75]], 39, 0.08).map((v, i) => (i < 9 ? NaN : v)), unc: 0.22, pressure: 70 },
  { id: 'h100', name: 'H100 SXM', arch: 'Hopper', memory: '80 GB HBM3', index: shaped(33, [[0, 3.9], [10, 2.9], [20, 2.25], [24, 2.21], [26, 2.31], [38, 2.08]], 39, 0.06), unc: 0.14, pressure: 66 },
  { id: 'mi300x', name: 'MI300X', arch: 'CDNA3', memory: '192 GB HBM3', index: shaped(34, [[0, 4.4], [12, 3.1], [26, 2.04], [38, 1.85]], 39, 0.1), unc: 0.2, pressure: 49 },
  { id: 'a100', name: 'A100 80GB', arch: 'Ampere', memory: '80 GB HBM2e', index: shaped(35, [[0, 1.9], [12, 1.45], [26, 1.12], [38, 0.98]], 39, 0.05), unc: 0.08, pressure: 41 },
  { id: 'l40s', name: 'L40S', arch: 'Ada', memory: '48 GB GDDR6', index: shaped(36, [[0, 1.3], [12, 1.05], [26, 0.86], [38, 0.79]], 39, 0.04), unc: 0.07, pressure: 38 },
]
export const gpuById = (id: string) => GPUS.find((g) => g.id === id)

export interface Provider {
  id: string
  name: string
  type: 'Hyperscaler' | 'Neocloud' | 'Marketplace'
  ticker?: string
  reliability: number // 0–100 composite (observed uptime + incident reports); NaN = not reported
  note: string
  /** 'illustrative' for the design's own providers; set from provenance for backend providers. */
  freshness?: Freshness
  sourceUrl?: string
}
export const PROVIDERS: Provider[] = [
  { id: 'coreweave', name: 'CoreWeave', type: 'Neocloud', ticker: 'CRWV', reliability: 92, note: 'InfiniBand clusters, reserved-heavy' },
  { id: 'lambda', name: 'Lambda', type: 'Neocloud', reliability: 88, note: '1-click clusters, on-demand' },
  { id: 'nebius', name: 'Nebius', type: 'Neocloud', ticker: 'NBIS', reliability: 89, note: 'EU-heavy footprint' },
  { id: 'crusoe', name: 'Crusoe', type: 'Neocloud', reliability: 86, note: 'Energy-first sites' },
  { id: 'aws', name: 'AWS', type: 'Hyperscaler', ticker: 'AMZN', reliability: 97, note: 'Capacity blocks; list price' },
  { id: 'azure', name: 'Azure', type: 'Hyperscaler', ticker: 'MSFT', reliability: 96, note: 'ND-series; list price' },
  { id: 'gcp', name: 'Google Cloud', type: 'Hyperscaler', ticker: 'GOOGL', reliability: 96, note: 'A3 series; list price' },
  { id: 'vast', name: 'Vast-style marketplace', type: 'Marketplace', reliability: 71, note: 'Peer hosts; variable quality' },
  { id: 'runpod', name: 'RunPod', type: 'Marketplace', reliability: 82, note: 'Secure + community cloud' },
]
// The design's providers are fictional composites: mark them illustrative. Backend providers are
// added by src/live/gpu.ts when offers land.
PROVIDERS.forEach((p) => (p.freshness = 'illustrative'))
export const providerById = (id: string) => PROVIDERS.find((p) => p.id === id)
/** Reliability for display: 'n/a' when the backend does not report one. */
export const fmtReliability = (p: Provider | undefined) => (p && Number.isFinite(p.reliability) ? String(p.reliability) : 'n/a')

export interface Offer {
  id: string
  provider: string
  gpu: string
  region: string
  price: number
  contract: 'On-demand' | 'Spot' | '1-mo reserved' | '1-yr reserved'
  interconnect: 'InfiniBand 3.2T' | 'RoCE 400G' | 'PCIe only' | 'Not reported'
  nodeSize: number
  available: 'Available' | 'Limited' | 'Waitlist' | 'Sold out'
  observed: string
  freshness: Freshness
  egress: string
  sla: string
  /** Backend provenance note, e.g. Vast.ai's "a current market reading, not an index". */
  note?: string
  /** Full provenance note, for a tooltip. */
  detail?: string
  sourceUrl?: string
  /** True for offers that came from the backend. */
  live?: boolean
}

function makeOffers(): Offer[] {
  const r = rng(99)
  const combos: [string, string, string[]][] = [
    ['coreweave', 'h100', ['us-tx', 'us-va', 'uk-lon']],
    ['coreweave', 'b200', ['us-tx', 'us-va']],
    ['lambda', 'h100', ['us-tx', 'us-az', 'us-or']],
    ['lambda', 'b200', ['us-tx']],
    ['lambda', 'a100', ['us-or', 'us-tx']],
    ['nebius', 'h100', ['no-nordics', 'nl-ams', 'uk-lon']],
    ['nebius', 'h200', ['no-nordics', 'nl-ams']],
    ['nebius', 'b200', ['no-nordics']],
    ['crusoe', 'h100', ['us-tx', 'ca-qc']],
    ['crusoe', 'h200', ['us-tx']],
    ['aws', 'h100', ['us-va', 'us-or', 'ie-dub', 'jp-tyo', 'sg']],
    ['aws', 'h200', ['us-va']],
    ['azure', 'h100', ['us-va', 'ie-dub', 'nl-ams', 'jp-tyo']],
    ['azure', 'mi300x', ['us-va', 'ca-qc']],
    ['gcp', 'h100', ['us-va', 'us-or', 'de-fra', 'sg', 'jp-tyo', 'au-syd']],
    ['vast', 'h100', ['us-tx', 'de-fra', 'no-nordics', 'in-mum']],
    ['vast', 'l40s', ['us-tx', 'de-fra', 'br-sao']],
    ['vast', 'a100', ['us-tx', 'in-mum']],
    ['runpod', 'h100', ['us-tx', 'ca-qc', 'de-fra']],
    ['runpod', 'l40s', ['us-tx', 'ca-qc']],
    ['runpod', 'mi300x', ['us-tx']],
  ]
  const out: Offer[] = []
  let n = 0
  for (const [p, g, regions] of combos) {
    const gpu = gpuById(g)!
    const prov = providerById(p)!
    for (const reg of regions) {
      const region = regionById(reg)!
      const base = gpu.index[NOW_IDX] * (1 + region.premium / 100 / 1.4)
      const typeMult = prov.type === 'Hyperscaler' ? 1.85 + r() * 0.5 : prov.type === 'Marketplace' ? 0.78 + r() * 0.15 : 0.95 + r() * 0.15
      const contracts: Offer['contract'][] = prov.type === 'Marketplace' ? ['On-demand', 'Spot'] : prov.type === 'Hyperscaler' ? ['On-demand', '1-yr reserved'] : ['On-demand', '1-mo reserved', '1-yr reserved']
      for (const c of contracts) {
        const cm = c === 'Spot' ? 0.72 : c === '1-mo reserved' ? 0.9 : c === '1-yr reserved' ? 0.74 : 1
        const avR = r()
        const avail: Offer['available'] = reg === 'us-tx' && g === 'h100' && c === 'On-demand' ? (avR > 0.4 ? 'Sold out' : 'Limited') : avR > 0.85 ? 'Waitlist' : avR > 0.6 ? 'Limited' : 'Available'
        out.push({
          id: `o${n++}`,
          provider: p,
          gpu: g,
          region: reg,
          price: +(base * typeMult * cm).toFixed(2),
          contract: c,
          interconnect: prov.type === 'Marketplace' ? (g === 'h100' && r() > 0.5 ? 'RoCE 400G' : 'PCIe only') : 'InfiniBand 3.2T',
          nodeSize: prov.type === 'Marketplace' ? (r() > 0.5 ? 8 : 1) : 8,
          available: avail,
          observed: region.quality === 'stale' ? '06 Mar 22:10' : `14 Mar 0${Math.floor(r() * 9)}:${String(Math.floor(r() * 59)).padStart(2, '0')}`,
          // Design offers are synthetic: always illustrative (the design shipped them as live/delayed).
          freshness: 'illustrative',
          egress: prov.type === 'Hyperscaler' ? '$0.09/GB' : prov.type === 'Marketplace' ? 'Varies' : 'Free',
          sla: prov.type === 'Hyperscaler' ? '99.9%' : prov.type === 'Marketplace' ? 'None' : '99.5%',
        })
      }
    }
  }
  return out
}
export const OFFERS = makeOffers()

export function fmtUsd(v: number, d = 0) {
  const s = Math.abs(v)
  const str = s >= 1e9 ? `${(s / 1e9).toFixed(2)}B` : s >= 1e6 ? `${(s / 1e6).toFixed(2)}M` : s >= 1e4 ? `${(s / 1e3).toFixed(1)}k` : s.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d })
  return `${v < 0 ? '−' : ''}$${str}`
}
export function fmtPct(v: number, d = 1, sign = true) {
  return `${v > 0 && sign ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(d)}%`
}
