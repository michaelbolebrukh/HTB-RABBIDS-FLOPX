import { rng, type Freshness } from './core'
import type { Candle } from './fund'
import { axisAnchor } from '@/live/dates'

// Weekly axis: 115 Fridays. The design drew 5 Jan 2024 to 13 Mar 2026; like the month axis
// (src/live/dates.ts) the slots stay and only the labels follow the calendar: the last slot is the
// week ending on the latest Friday on or before the backend's generated time (the client clock
// until a payload lands). Design series keep their slot positions; see src/live/adapt-w2.ts.
const WEEK_LEN = 115
const fmtWeek = (d: Date) => d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: '2-digit', timeZone: 'UTC' })
export function weekEnds(now: Date): Date[] {
  const day = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate())
  const lastFri = day - ((new Date(day).getUTCDay() + 2) % 7) * 86400000
  return Array.from({ length: WEEK_LEN }, (_, i) => new Date(lastFri - (WEEK_LEN - 1 - i) * 7 * 86400000))
}
/** The Friday each weekly slot ends on (UTC midnight). */
export const WEEK_DATES: Date[] = weekEnds(axisAnchor())
export const WEEKS: string[] = WEEK_DATES.map(fmtWeek)
/** Re-label the weekly axis in place (same arrays, so every importer sees it). */
export function relabelWeeks(now: Date) {
  if (Number.isNaN(now.getTime())) return
  weekEnds(now).forEach((d, i) => {
    WEEK_DATES[i] = d
    WEEKS[i] = fmtWeek(d)
  })
}
export const PAPER_START_WK = 104 // paper trading begins 11 weeks before the last slot
const fmtDayFull = (d: Date) => d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric', timeZone: 'UTC' })
const fmtMonYY = (d: Date) => d.toLocaleDateString('en-GB', { month: 'short', year: '2-digit', timeZone: 'UTC' })
/** "02 Jan 2026": the Friday of the first paper week (the design's own wording). */
export const paperStartLabel = () => fmtDayFull(WEEK_DATES[PAPER_START_WK])
/** "Jan 24 – Dec 25": the backtest span on the weekly axis, in the design's own format. */
export const backtestSpanLabel = () => `${fmtMonYY(WEEK_DATES[0])} – ${fmtMonYY(WEEK_DATES[PAPER_START_WK - 1])}`
const fmtMonYYYY = (d: Date) => d.toLocaleDateString('en-GB', { month: 'short', year: 'numeric', timeZone: 'UTC' })
/** "Jan 2024 – Dec 2025": the same span in the design's long format (backtest panel). */
export const backtestSpanLong = () => `${fmtMonYYYY(WEEK_DATES[0])} – ${fmtMonYYYY(WEEK_DATES[PAPER_START_WK - 1])}`

function walk(seed: number, startPx: number, drift: number, vol: number, len = 115, shocks: [number, number][] = []): number[] {
  const r = rng(seed)
  const out: number[] = []
  let p = startPx
  for (let i = 0; i < len; i++) {
    const z = (r() + r() + r() - 1.5) * 1.4
    p = p * (1 + drift + vol * z)
    for (const [w, s] of shocks) if (w === i) p *= 1 + s
    out.push(+p.toFixed(2))
  }
  return out
}

export type Group = 'Chips' | 'Hyperscaler' | 'Neocloud' | 'DC Operator' | 'Power' | 'Index'
/** One colour per category, so a ticker's sector reads at a glance in lists, chips and headers. */
export const GROUP_COLOR: Record<Group | 'Baskets', string> = {
  Chips: 'var(--color-fct)',
  Hyperscaler: 'var(--color-dec)',
  Neocloud: 'var(--color-accent)',
  'DC Operator': 'var(--color-up)',
  Power: 'var(--color-warn)',
  Index: 'var(--color-mute)',
  Baskets: 'var(--color-obs)',
}

export interface Security {
  ticker: string
  name: string
  group: Group
  exchange: string
  px: number[]
  mcap: number
  pe: number | null
  evSales: number
  revGrowth: number
  grossMargin: number
  beta: number
  vol30: number
  iv30: number | null
  ivSkew: number | null
  putCall: number | null
  shortInt: number
  computeLinks: { kind: 'documented' | 'modelled'; text: string; region?: string }[]
  description: string
}

const S = (ticker: string, name: string, group: Group, exchange: string, seed: number, p0: number, drift: number, vol: number, rest: Omit<Security, 'ticker' | 'name' | 'group' | 'exchange' | 'px'>, shocks: [number, number][] = []): Security => ({
  ticker, name, group, exchange, px: walk(seed, p0, drift, vol, 115, shocks), ...rest,
})

export const SECURITIES: Security[] = [
  S('NVDA', 'NVIDIA', 'Chips', 'NASDAQ', 1, 48, 0.013, 0.035, { mcap: 3.4e12, pe: 38, evSales: 22, revGrowth: 62, grossMargin: 74, beta: 1.7, vol30: 44, iv30: 46, ivSkew: 4.1, putCall: 0.82, shortInt: 1.1, computeLinks: [{ kind: 'documented', text: 'Supplier of H100/H200/B200 rented across all tracked regions' }, { kind: 'modelled', text: 'Weak association with global CPI (β 0.08, R² 0.02) — no edge found' }], description: 'Accelerated compute GPUs and networking.' }),
  S('AMD', 'Advanced Micro Devices', 'Chips', 'NASDAQ', 2, 140, 0.002, 0.04, { mcap: 2.6e11, pe: 44, evSales: 9, revGrowth: 18, grossMargin: 53, beta: 1.8, vol30: 48, iv30: 50, ivSkew: 3.2, putCall: 0.91, shortInt: 2.0, computeLinks: [{ kind: 'documented', text: 'MI300X supplier; rental index falling faster than H100' }], description: 'CPUs and Instinct accelerators.' }),
  S('TSM', 'Taiwan Semiconductor', 'Chips', 'NYSE', 3, 100, 0.008, 0.028, { mcap: 1.1e12, pe: 26, evSales: 11, revGrowth: 34, grossMargin: 59, beta: 1.2, vol30: 33, iv30: 34, ivSkew: 2.4, putCall: 0.7, shortInt: 0.6, computeLinks: [{ kind: 'modelled', text: 'No significant association with rental or pressure features (exploratory)' }], description: 'Leading-edge foundry.' }),
  S('MSFT', 'Microsoft', 'Hyperscaler', 'NASDAQ', 4, 370, 0.003, 0.018, { mcap: 3.1e12, pe: 34, evSales: 12, revGrowth: 15, grossMargin: 69, beta: 0.9, vol30: 22, iv30: 23, ivSkew: 1.8, putCall: 0.75, shortInt: 0.5, computeLinks: [{ kind: 'documented', text: 'Azure regions in NoVA, Dublin, Amsterdam, Tokyo' }], description: 'Cloud (Azure), software.' }),
  S('AMZN', 'Amazon', 'Hyperscaler', 'NASDAQ', 5, 150, 0.005, 0.022, { mcap: 2.2e12, pe: 41, evSales: 3.5, revGrowth: 11, grossMargin: 48, beta: 1.1, vol30: 27, iv30: 28, ivSkew: 1.9, putCall: 0.8, shortInt: 0.7, computeLinks: [{ kind: 'documented', text: 'AWS us-east-1 (NoVA) is largest region' }], description: 'AWS cloud and retail.' }),
  S('GOOGL', 'Alphabet', 'Hyperscaler', 'NASDAQ', 6, 140, 0.004, 0.022, { mcap: 2.1e12, pe: 23, evSales: 6, revGrowth: 13, grossMargin: 58, beta: 1.0, vol30: 26, iv30: 27, ivSkew: 1.6, putCall: 0.72, shortInt: 0.6, computeLinks: [{ kind: 'documented', text: 'GCP regions incl. Oregon, Frankfurt, Singapore' }], description: 'Search, cloud, TPUs.' }),
  S('ORCL', 'Oracle', 'Hyperscaler', 'NYSE', 7, 105, 0.008, 0.03, { mcap: 5.2e11, pe: 39, evSales: 10, revGrowth: 21, grossMargin: 70, beta: 1.1, vol30: 39, iv30: 41, ivSkew: 2.9, putCall: 0.86, shortInt: 1.4, computeLinks: [{ kind: 'documented', text: 'Large Texas campus commitments (reported)', region: 'us-tx' }], description: 'Database, OCI cloud.' }),
  S('CRWV', 'CoreWeave', 'Neocloud', 'NASDAQ', 8, 40, 0.009, 0.06, { mcap: 6.4e10, pe: null, evSales: 14, revGrowth: 140, grossMargin: 72, beta: 2.4, vol30: 82, iv30: 88, ivSkew: 6.8, putCall: 1.12, shortInt: 9.8, computeLinks: [{ kind: 'documented', text: 'DFW facilities (10-K)', region: 'us-tx' }, { kind: 'modelled', text: 'Sensitivity to H100 rental index β 0.55 (R² 0.12)' }], description: 'GPU cloud provider.' }, [[66, 0.25], [90, -0.18]]),
  S('NBIS', 'Nebius Group', 'Neocloud', 'NASDAQ', 9, 18, 0.01, 0.055, { mcap: 1.9e10, pe: null, evSales: 21, revGrowth: 310, grossMargin: 61, beta: 2.1, vol30: 74, iv30: 79, ivSkew: 5.5, putCall: 1.02, shortInt: 6.4, computeLinks: [{ kind: 'documented', text: 'Nordic + EU footprint', region: 'no-nordics' }, { kind: 'modelled', text: 'Negative exposure to Nordic loosening (β −0.12, weak)' }], description: 'European AI infrastructure.' }),
  S('IREN', 'IREN', 'Neocloud', 'NASDAQ', 10, 6, 0.012, 0.07, { mcap: 1.1e10, pe: null, evSales: 18, revGrowth: 95, grossMargin: 66, beta: 2.8, vol30: 96, iv30: null, ivSkew: null, putCall: null, shortInt: 12.1, computeLinks: [{ kind: 'documented', text: 'Texas (Childress) site — illustrative', region: 'us-tx' }], description: 'Bitcoin miner pivoting to AI cloud.' }),
  S('APLD', 'Applied Digital', 'DC Operator', 'NASDAQ', 11, 5, 0.01, 0.07, { mcap: 4.8e9, pe: null, evSales: 15, revGrowth: 80, grossMargin: 31, beta: 2.6, vol30: 91, iv30: null, ivSkew: null, putCall: null, shortInt: 18.2, computeLinks: [{ kind: 'documented', text: 'Developer of Red Plains Campus (illustr.)', region: 'us-tx' }], description: 'HPC data centre developer.' }, [[110, -0.14]]),
  S('EQIX', 'Equinix', 'DC Operator', 'NASDAQ', 12, 800, 0.0015, 0.018, { mcap: 8.1e10, pe: 72, evSales: 11, revGrowth: 7, grossMargin: 49, beta: 0.8, vol30: 21, iv30: 22, ivSkew: 1.5, putCall: 0.66, shortInt: 1.8, computeLinks: [{ kind: 'documented', text: 'Ashburn, Amsterdam, Singapore campuses', region: 'us-va' }], description: 'Interconnection-focused data centre REIT.' }),
  S('DLR', 'Digital Realty', 'DC Operator', 'NYSE', 13, 135, 0.003, 0.02, { mcap: 5.7e10, pe: 88, evSales: 11, revGrowth: 9, grossMargin: 54, beta: 0.9, vol30: 24, iv30: 25, ivSkew: 1.7, putCall: 0.71, shortInt: 2.3, computeLinks: [{ kind: 'documented', text: 'Largest NoVA lessor by MW', region: 'us-va' }, { kind: 'modelled', text: 'NoVA pressure sensitivity β 0.36 (R² 0.14)' }], description: 'Hyperscale data centre REIT.' }),
  S('VST', 'Vistra', 'Power', 'NYSE', 14, 38, 0.012, 0.035, { mcap: 5.3e10, pe: 24, evSales: 3.2, revGrowth: 14, grossMargin: 38, beta: 1.2, vol30: 41, iv30: 43, ivSkew: 2.8, putCall: 0.77, shortInt: 2.6, computeLinks: [{ kind: 'documented', text: 'ERCOT-heavy generation fleet', region: 'us-tx' }, { kind: 'modelled', text: 'TX pressure sensitivity β 0.42 (R² 0.18)' }], description: 'Integrated power producer.' }, [[110, 0.06]]),
  S('CEG', 'Constellation Energy', 'Power', 'NASDAQ', 15, 115, 0.009, 0.03, { mcap: 9.8e10, pe: 31, evSales: 4.1, revGrowth: 9, grossMargin: 31, beta: 1.0, vol30: 36, iv30: 37, ivSkew: 2.2, putCall: 0.68, shortInt: 1.5, computeLinks: [{ kind: 'documented', text: 'Nuclear PPAs with hyperscalers (PJM)', region: 'us-va' }], description: 'Nuclear-heavy generator.' }),
  S('NRG', 'NRG Energy', 'Power', 'NYSE', 16, 52, 0.008, 0.028, { mcap: 2.9e10, pe: 19, evSales: 1.1, revGrowth: 6, grossMargin: 22, beta: 1.0, vol30: 33, iv30: 34, ivSkew: 2.0, putCall: 0.74, shortInt: 3.1, computeLinks: [{ kind: 'documented', text: 'Texas retail + generation', region: 'us-tx' }, { kind: 'modelled', text: 'TX pressure β 0.27 (R² 0.11)' }], description: 'Power generation and retail.' }),
  S('SOX', 'PHLX Semiconductor Index', 'Index', 'Index', 17, 3800, 0.005, 0.025, { mcap: 0, pe: null, evSales: 0, revGrowth: 0, grossMargin: 0, beta: 1.4, vol30: 31, iv30: null, ivSkew: null, putCall: null, shortInt: 0, computeLinks: [], description: 'Benchmark.' }),
  S('SPX', 'S&P 500', 'Index', 'Index', 18, 4700, 0.0028, 0.011, { mcap: 0, pe: null, evSales: 0, revGrowth: 0, grossMargin: 0, beta: 1, vol30: 14, iv30: null, ivSkew: null, putCall: null, shortInt: 0, computeLinks: [], description: 'Benchmark.' }),
]
export const secByTicker = (t: string) => SECURITIES.find((s) => s.ticker === t)

// ---------- Price-chart series per security (illustrative, derived from the weekly closes) ----------
export interface SecBars { candles: Candle[]; volume: number[]; drawdown: number[] }
const barsCache = new Map<string, { sig: string; bars: SecBars }>()
/**
 * Weekly OHLC (open = previous close, seeded intraweek range scaled by 30d vol),
 * dollar volume (bigger on big moves) and drawdown from the running peak, in %.
 *
 * Cached per ticker, keyed on the closes themselves: `applyCloses` (src/live/adapt-w2.ts) rewrites
 * `s.px` in place when archived closes land for a signed-in reader and again on sign-out, and a cache
 * keyed on the ticker alone kept serving the design's candles under a header that showed the
 * archive's close (WF8 #4). The same `px` still returns the same object.
 */
export function secBars(s: Security): SecBars {
  const sig = s.px.join(',')
  const hit = barsCache.get(s.ticker)
  if (hit && hit.sig === sig) return hit.bars
  const r = rng([...s.ticker].reduce((h, ch) => h * 31 + ch.charCodeAt(0), 7) % 100000)
  const wkVol = s.vol30 / 100 / Math.sqrt(52)
  let peak = -Infinity
  const candles: Candle[] = [], volume: number[] = [], drawdown: number[] = []
  s.px.forEach((c, i) => {
    const o = i ? s.px[i - 1] : c * (1 - wkVol * 0.3)
    const span = Math.abs(c - o) * 0.3 + c * wkVol * (0.25 + r() * 0.45)
    candles.push({ o, c, h: Math.max(o, c) + span * r(), l: Math.min(o, c) - span * r() })
    const move = Math.abs(c - o) / o / (wkVol || 0.01)
    volume.push(s.mcap * (0.004 + r() * 0.006) * (1 + 0.6 * move))
    peak = Math.max(peak, c)
    drawdown.push(((c - peak) / peak) * 100)
  })
  const out = { candles, volume, drawdown }
  barsCache.set(s.ticker, { sig, bars: out })
  return out
}
export const last = (a: number[]) => a[a.length - 1]
export const chg = (a: number[], n = 1) => ((a[a.length - 1] - a[a.length - 1 - n]) / a[a.length - 1 - n]) * 100

export interface Basket {
  id: string
  name: string
  members: { t: string; w: number }[]
  thesis: string
}
export const BASKETS: Basket[] = [
  { id: 'b-power', name: 'Power for AI', members: [{ t: 'VST', w: 0.4 }, { t: 'CEG', w: 0.35 }, { t: 'NRG', w: 0.25 }], thesis: 'Generators exposed to data-centre load growth in constrained grids.' },
  { id: 'b-neo', name: 'Neoclouds', members: [{ t: 'CRWV', w: 0.5 }, { t: 'NBIS', w: 0.3 }, { t: 'IREN', w: 0.2 }], thesis: 'Pure-play GPU rental providers; sensitive to rental prices and delivery timing.' },
  { id: 'b-dc', name: 'Data-centre landlords', members: [{ t: 'EQIX', w: 0.5 }, { t: 'DLR', w: 0.5 }], thesis: 'Lessors benefiting from scarce powered shells.' },
  { id: 'b-chips', name: 'Accelerator supply chain', members: [{ t: 'NVDA', w: 0.5 }, { t: 'AMD', w: 0.2 }, { t: 'TSM', w: 0.3 }], thesis: 'Upstream of compute; weakly related to rental pressure.' },
]
export function basketSeries(b: Basket): number[] {
  const ss = b.members.map((m) => ({ w: m.w, px: secByTicker(m.t)!.px }))
  return ss[0].px.map((_, i) => +(ss.reduce((s, x) => s + x.w * (x.px[i] / x.px[0]) * 100, 0)).toFixed(2))
}

// ---------- News & prediction markets ----------
export type NewsType = 'News' | 'Corporate' | 'Project change' | 'Prediction market' | 'Regulatory'

export interface NewsItem {
  id: string
  type: NewsType
  title: string
  source: string
  time: string
  ago: string
  summary: string
  regions: string[]
  tickers: string[]
  projects?: string[]
  impact: 'high' | 'medium' | 'low'
  verification: 'Confirmed by filing' | 'Single source' | 'Multiple sources' | 'Unverified'
  /** Link to the original item (live feed). */
  url?: string
  /** 'illustrative' for the design's authored items; the live feed sets its own (src/live/adapt-w3.ts). */
  freshness?: Freshness
}

// Authored design items, shown as illustrative until the live feed replaces them.
export const NEWS: NewsItem[] = ([
  { id: 'n1', type: 'Project change', title: 'Red Plains Ph.2 substation completion pushed to September', source: 'Developer Q4 earnings call', time: '24 Feb 2026 21:30 UTC', ago: '18d', summary: 'Management cited transformer lead times. First 200 MW energisation now guided to Q4 2026 (was Q2).', regions: ['us-tx'], tickers: ['APLD', 'CRWV'], projects: ['red-plains'], impact: 'high', verification: 'Confirmed by filing' },
  { id: 'n2', type: 'News', title: 'Two frontier labs said to be seeking 300 MW+ in Dallas–Fort Worth', source: 'Trade publication (paywalled)', time: '05 Mar 2026 14:10 UTC', ago: '9d', summary: 'Report cites people familiar; not confirmed by companies. FLOPX treats as weak evidence.', regions: ['us-tx'], tickers: ['CRWV', 'ORCL'], impact: 'medium', verification: 'Single source' },
  { id: 'n3', type: 'Regulatory', title: 'ERCOT opens consultation on large-load interconnection standards', source: 'ERCOT market notice', time: '11 Mar 2026 16:00 UTC', ago: '3d', summary: 'Proposal would require curtailment capability for loads >75 MW connecting after 2026.', regions: ['us-tx'], tickers: ['VST', 'NRG'], impact: 'high', verification: 'Confirmed by filing' },
  { id: 'n4', type: 'Corporate', title: 'Neocloud raises $2.1B debt facility secured by GPU fleet', source: '8-K filing', time: '12 Mar 2026 21:05 UTC', ago: '2d', summary: 'Proceeds to fund B200 deployments in US-Central and US-East.', regions: ['us-tx', 'us-va'], tickers: ['CRWV'], impact: 'medium', verification: 'Confirmed by filing' },
  { id: 'n5', type: 'Project change', title: 'Fjordlys Hall 3 energised on schedule (75 MW)', source: 'Company update', time: '15 Jan 2026 08:00 UTC', ago: '58d', summary: 'Adds H200/B200 capacity in NO5 zone.', regions: ['no-nordics'], tickers: ['NBIS'], projects: ['fjord-1'], impact: 'medium', verification: 'Multiple sources' },
  { id: 'n6', type: 'Regulatory', title: 'Johor tightens water and power approval criteria for new data centres', source: 'State regulator statement', time: '02 Feb 2026 03:00 UTC', ago: '40d', summary: 'Applies to new applications; existing approvals unaffected (per statement).', regions: ['sg'], tickers: ['EQIX'], projects: ['johor-7'], impact: 'medium', verification: 'Confirmed by filing' },
  { id: 'n7', type: 'News', title: 'H100 spot quotes in Europe slip below $2/hr on marketplaces', source: 'Marketplace data (FLOPX observed)', time: '13 Mar 2026 18:00 UTC', ago: '1d', summary: 'Primarily Nordic and community-cloud hosts; enterprise reserved pricing steady.', regions: ['no-nordics', 'de-fra'], tickers: ['NBIS'], impact: 'low', verification: 'Multiple sources' },
  { id: 'n8', type: 'Corporate', title: 'Data-centre REIT reports record NoVA pre-leasing', source: 'Q4-25 supplemental', time: '20 Feb 2026 21:00 UTC', ago: '22d', summary: 'Pre-leased 92% of capacity delivering through 2027.', regions: ['us-va'], tickers: ['DLR'], impact: 'medium', verification: 'Confirmed by filing' },
] as NewsItem[]).map((n) => ({ ...n, freshness: 'illustrative' as const }))

/** Summary of the live feed for the News page's Headlines panel; `live` stays false until it lands. */
export const NEWS_FEED_SUMMARY = { live: false, headlines: 0, days: 0, sources: [] as string[], themes: [] as string[], asOf: '' }

export interface PredMarket {
  id: string
  question: string
  venue: string
  prob: number
  probHist: number[]
  /** Points over 7 days; null when the venue gives no history. */
  chg7: number | null
  /** USD; live cards carry 24-hour volume, null when the venue reports none. */
  volume: number | null
  liquidity: number
  /** Bid-ask spread as a probability; null when the venue does not report one. */
  spread: number | null
  closes: string
  regions: string[]
  tickers: string[]
  resolution: string
  caution?: string
  url?: string
  freshness?: Freshness
  /** Last price update, "08 Nov 2026 00:38 UTC". */
  updated?: string
}
const ph = (seed: number, startP: number, endP: number) => {
  const r = rng(seed)
  return Array.from({ length: 30 }, (_, i) => Math.max(0.01, Math.min(0.99, +(startP + ((endP - startP) * i) / 29 + (r() - 0.5) * 0.04).toFixed(3))))
}
export const PRED: PredMarket[] = ([
  { id: 'pm1', question: 'Will ERCOT declare an Energy Emergency Alert before 30 Sep 2026?', venue: 'Kalshi (illustr.)', prob: 0.31, probHist: ph(1, 0.19, 0.31), chg7: 6, volume: 412000, liquidity: 48000, spread: 0.03, closes: '30 Sep 2026', regions: ['us-tx'], tickers: ['VST', 'NRG'], resolution: 'Resolves YES if ERCOT issues EEA level 1+.' },
  { id: 'pm2', question: 'Will a US federal data-centre power moratorium bill pass either chamber in 2026?', venue: 'Polymarket (illustr.)', prob: 0.07, probHist: ph(2, 0.09, 0.07), chg7: -1, volume: 86000, liquidity: 6100, spread: 0.06, closes: '31 Dec 2026', regions: ['us-va', 'us-tx'], tickers: ['DLR', 'EQIX'], resolution: 'Resolves YES on passage in House or Senate.', caution: 'Thin liquidity: $6.1k depth. A single trader could move this price by >5 pts.' },
  { id: 'pm3', question: 'H100 on-demand median above $2.50/hr on 30 Jun 2026?', venue: 'Kalshi (illustr.)', prob: 0.24, probHist: ph(3, 0.33, 0.24), chg7: -2, volume: 39000, liquidity: 3800, spread: 0.08, closes: '30 Jun 2026', regions: [], tickers: ['CRWV'], resolution: 'Per named third-party GPU index.', caution: 'Thin liquidity and index-definition differs from FLOPX index.' },
  { id: 'pm4', question: 'Will Ireland lift Dublin DC connection restrictions in 2026?', venue: 'Polymarket (illustr.)', prob: 0.12, probHist: ph(4, 0.15, 0.12), chg7: 0, volume: 22000, liquidity: 2400, spread: 0.07, closes: '31 Dec 2026', regions: ['ie-dub'], tickers: [], resolution: 'Official policy change by EirGrid/CRU.', caution: 'Thin liquidity.' },
] as PredMarket[]).map((p) => ({ ...p, freshness: 'illustrative' as const }))
