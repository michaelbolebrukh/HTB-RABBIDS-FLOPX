// Live module 'finance' (W2): equities, the fund's evaluation and its backtests.
//
// Reads: /flopx/companies, /flopx/companies/{t} (SEC EDGAR fundamentals), /flopx/research/evaluation,
// /flopx/research/evaluation/portfolio, and /flopx/gpu/series?id=equity:<T>:close only for a ticker
// whose latest close is not authored sample data.
// Writes, in place: SECURITIES[*].revGrowth, SECURITIES[*].px (weekly slots with an observed close),
// SECURITIES[*].computeLinks (modelled rows), MODEL_COMPARE, MODEL_COMPARE_META, BACKTEST_STATS,
// FAILURES, the weekly axis labels (WEEKS), and W2_LIVE (what landed, with its provenance), which
// the pages read to choose each badge.
//
// What stays illustrative, and why: the design's account (NAV, positions, orders) because
// /flopx/fund/summary and /flopx/paper/* need sign-in; the design's signals and decision records
// because the backend's signals carry no forecast (all skipped: no documented exposure). The
// evaluation and the backtests are the backend's own numbers; the closes they were scored on are
// whatever the payload's freshness block says (src/live/evalBasis.ts: authored sample closes, or an
// archive such as alpaca-data through a stated date), and every badge, caption and failures entry
// here is derived from that block, never from an assumption about the feed.
import { BACKTEST_STATS, FAILURES, MODEL_COMPARE, MODEL_COMPARE_META, resetSignalToPending, SIGNALS, type HorizonId, type ModelRow, type Signal, type ValidationStatus } from '@/data/fund'
import { relabelWeeks, SECURITIES, WEEK_DATES } from '@/data/finance'
import type { Getter, LiveModule } from './boot'
import { joinToNow } from './dates'
import { fetchForecast, timingGateOf, type ForecastOutcome } from './adapt-w5'
import { GPU_MODEL } from './gpu'
import { fmtDay, freshOf, type FreshTag } from './provenance'
import type { Provenance, SeriesPoint } from './types'
import type { Freshness } from '@/data/core'
import { evalPriceBasis, type PriceBasis } from './evalBasis'
import { setLiveTotals } from './store'

// ---------- Wire types (only the fields read here) ----------
export interface Unavailable { source: string; reason: string }
export interface W2Freshness { generatedAt: string; unavailable?: (Unavailable | string)[]; sources?: string[]; oldestObservedAt?: string; sourceDetails?: { source: string; kind?: string; observedAt?: string }[] }

export interface CompanyWire {
  ticker: string
  name: string
  exchange: string
  provenance: Provenance
}
export interface CloseWire { seriesId: string; t: string; value: number; unit: string; provenance: Provenance }
export interface CompaniesResponse {
  companies: { company: CompanyWire; latestClose: CloseWire | null; relatedItemCount?: number }[]
  freshness: W2Freshness
}
export interface FundamentalWire {
  ticker: string
  concept: string
  value: number
  unit: string
  periodEnd: string
  filedAt: string
  provenance: Provenance
}
export interface CompanyDetailResponse {
  company: CompanyWire
  latestClose: CloseWire | null
  fundamentals: FundamentalWire[]
  unavailable?: Unavailable | null
  freshness: W2Freshness
}

export type Verdict = 'improves' | 'inconclusive' | 'worsens' | 'insufficient-sample' | 'reference'
export interface ModelResultWire {
  model: 'baseline' | 'augmented' | 'combined' | string
  n: number
  mae: number | null
  rmse: number | null
  hitRate: number | null
  coverage80: number | null
  skillVsBaseline: number | null
  verdict: Verdict
  featuresUsed: string[]
  baselineRmseMatched: number | null
}
export interface HorizonEvalWire { ticker: string; horizonDays: number; origins: number; models: ModelResultWire[]; verdict: Verdict; notes: string[] }
export interface TickerEvalWire {
  ticker: string
  horizons: HorizonEvalWire[]
  featuresAvailable: string[]
  featuresDropped: Record<string, string>
  inputKinds: Provenance['kind'][]
  provenance: Provenance
  method: string
  reliabilityNote: string
}
export interface EvaluationResponse { target: string; horizons: number[]; evaluations: TickerEvalWire[]; methodText: string; freshness: W2Freshness }

export interface StrategyBacktestWire {
  strategyId: string
  variant: 'compute' | 'price-only' | string
  mode: string
  start: string
  end: string
  trades: number
  totalReturnPct: number
  benchmarkReturnPct: number
  maxDrawdownPct: number
  hitRate: number | null
  roundTrips: number
  benchmarkTicker: string
  turnoverRatio: number
  costBps: number
  costsUsd: number
  provenance: Provenance
}
export interface PortfolioEvalResponse {
  strategies: StrategyBacktestWire[]
  benchmark: { tickers: string[]; start: string; end: string; totalReturnPct: number; rule: string }
  costBps: number
  inputKinds: Provenance['kind'][]
  notes: string[]
  freshness: W2Freshness
}
export interface SeriesResponseW2 { seriesId: string; unit: string; points: SeriesPoint[] }

// ---------- What landed (read by the pages) ----------
export interface RevGrowth {
  /** Year-on-year revenue change, %, latest filed period against the same period a year earlier. */
  pct: number
  latest: { value: number; periodEnd: string; filedAt: string; form: string }
  prior: { value: number; periodEnd: string }
  fresh: FreshTag
  sourceUrl: string | null
}
/** The latest observed close that landed on the weekly axis, with the provenance of that very point. */
export interface PlacedClose {
  t: string
  value: number
  fresh: FreshTag
  source: string
  /** True when it sits in the axis's last week, so the figure a list row shows (`last(px)`) is this close. */
  latestWeek: boolean
}
/** One fiscal quarter of the archived Compustat rows (served to signed-in readers only). */
export interface Quarter {
  periodEnd: string
  /** Fiscal quarter number from the row's note, when stated. */
  fq: number | null
  revenue: number | null
  capex: number | null
  totalAssets: number | null
  /** capex / revenue as a fraction; null when either side is missing. */
  intensity: number | null
}
export interface Quarterly {
  source: string
  fresh: FreshTag
  licence: string | null
  /** Ascending by period end. */
  quarters: Quarter[]
  from: string
  through: string
}
export interface CompanyLive {
  ticker: string
  /** Provenance of the backend's latest close; 'illustrative' while it is authored sample data. */
  close: { value: number; t: string; fresh: FreshTag } | null
  /** Weekly slots of SECURITIES[t].px that now carry an observed close. */
  observedWeeks: number
  /** The observed close in the latest placed week, or null when nothing observed landed (WF7 #1). */
  placed: PlacedClose | null
  /** Per weekly slot of SECURITIES[t].px (WEEK_DATES order): true where the slot carries an observed close; empty when none landed. */
  realWeeks: boolean[]
  revGrowth: RevGrowth | null
  /** Archived quarterly fundamentals (WRDS Compustat), present only when the detail read carried them (WF7 #2). */
  quarterly: Quarterly | null
}
export interface EvalLive {
  generatedAt: string
  fresh: FreshTag
  tickers: string[]
  reliabilityNote: string
  /** What closes the harness scored on, from the payload's freshness block (src/live/evalBasis.ts). */
  priceBasis: PriceBasis
  /** Per horizon: count of tickers by the backend verdict of the compute-augmented model. */
  augmented: Record<HorizonId, Record<Verdict, number>>
  /** Per horizon: tickers where the event feature produced a scored combined forecast (n >= 20). */
  combinedScored: Record<HorizonId, number>
}
export interface PortfolioLive {
  generatedAt: string
  fresh: FreshTag
  sleeves: number
  priceOnlyReturnPct: number
  benchmarkReturnPct: number
  benchmarkTickers: number
  costsUsd: number
}
export interface W2State {
  companies: Record<string, CompanyLive>
  evaluation: EvalLive | null
  portfolio: PortfolioLive | null
  unavailable: Unavailable[]
}
export const W2_LIVE: W2State = { companies: {}, evaluation: null, portfolio: null, unavailable: [] }

// ---------- Small helpers ----------
const HZ_DAYS: Record<HorizonId, number> = { '1W': 5, '1M': 21, '3M': 63 }
export const HORIZON_DAYS = HZ_DAYS
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const monYear = (iso: string) => {
  const d = new Date(iso)
  return `${MON[d.getUTCMonth()]} ${d.getUTCFullYear()}`
}
const round = (v: number, d: number) => +v.toFixed(d)

/** freshness.unavailable arrives as objects on the wire; tolerate bare strings too. */
export function unavailableOf(f: W2Freshness | null | undefined): Unavailable[] {
  return (f?.unavailable ?? []).map((u) => (typeof u === 'string' ? { source: u, reason: 'unavailable' } : u))
}

// ---------- Companies ----------
export interface CompanyRow { ticker: string; name: string; exchange: string; close: CompanyLive['close'] }

export function adaptCompanies(p: CompaniesResponse): CompanyRow[] {
  return p.companies.map(({ company, latestClose }) => ({
    ticker: company.ticker,
    name: company.name,
    exchange: company.exchange,
    close: latestClose && Number.isFinite(latestClose.value) ? { value: latestClose.value, t: latestClose.t, fresh: freshOf(latestClose.provenance) } : null,
  }))
}

const PERIOD_RE = /period (\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2})/
const FORM_RE = /from (10-K|10-Q|20-F|40-F|6-K)\b/
const DAY = 86_400_000

interface RevRow { value: number; periodEnd: string; filedAt: string; form: string; days: number | null; p: Provenance }

/** The archived quarterly source (WRDS Compustat) has its own panel (quarterlyOf); it never feeds the filing-based growth. */
const QUARTERLY_ARCHIVE = 'wrds-compustat'

function revRows(fs: FundamentalWire[]): RevRow[] {
  return fs
    // A signed-in detail also carries the Compustat quarterlies; without this filter their latest
    // quarter (period end after the last SEC filing's) would displace the SEC figure and badge it
    // archived, with an empty form (found by the N-FIX5 module test, 8 Nov 2026).
    .filter((f) => f.concept === 'revenue' && f.unit === 'USD' && Number.isFinite(f.value) && f.value > 0 && f.provenance.kind !== 'sample' && f.provenance.source !== QUARTERLY_ARCHIVE)
    .map((f) => {
      const note = f.provenance.note ?? ''
      const m = PERIOD_RE.exec(note)
      const days = m ? Math.round((Date.parse(m[2]) - Date.parse(m[1])) / DAY) : null
      const form = FORM_RE.exec(note)?.[1] ?? (f.provenance.sourceUrl?.match(/type=([0-9A-Z-]+)/)?.[1] ?? '')
      return { value: f.value, periodEnd: f.periodEnd, filedAt: f.filedAt, form, days, p: f.provenance }
    })
}

/**
 * Revenue growth from SEC filings: the latest filed revenue period against the period ending about
 * a year earlier with the same length (a 6-month year-to-date against the prior 6-month
 * year-to-date, a fiscal year against the prior fiscal year). Sample fundamentals are ignored.
 * Returns null when no like-for-like pair exists, rather than comparing unequal periods.
 */
export function revGrowthOf(fs: FundamentalWire[]): RevGrowth | null {
  const rows = revRows(fs).sort((a, b) => Date.parse(b.periodEnd) - Date.parse(a.periodEnd))
  for (const latest of rows.slice(0, 3)) {
    const target = Date.parse(latest.periodEnd) - 365 * DAY
    const prior = rows.find((r) => {
      if (r === latest) return false
      if (Math.abs(Date.parse(r.periodEnd) - target) > 14 * DAY) return false
      if (latest.days != null && r.days != null) return Math.abs(latest.days - r.days) <= 15
      return r.form === latest.form
    })
    if (prior) {
      return {
        pct: round(((latest.value - prior.value) / prior.value) * 100, 1),
        latest: { value: latest.value, periodEnd: latest.periodEnd, filedAt: latest.filedAt, form: latest.form },
        prior: { value: prior.value, periodEnd: prior.periodEnd },
        fresh: freshOf(latest.p),
        sourceUrl: latest.p.sourceUrl ?? null,
      }
    }
  }
  return null
}

const QUARTERLY_CONCEPTS = new Set(['revenue', 'capex', 'totalAssets'])
const FQ_RE = /fiscal quarter (\d)/

/**
 * The archived quarterly rows of a company detail (WRDS Compustat: revenue, capex, total assets),
 * grouped by fiscal period end and ascending. Sample rows and every other source are ignored, so
 * an anonymous payload (SEC EDGAR only) yields null and the panel stays as it is for everyone
 * else. A quarter missing one side keeps the other with the ratio null, never an estimate.
 */
export function quarterlyOf(fs: FundamentalWire[], source = QUARTERLY_ARCHIVE): Quarterly | null {
  const rows = fs.filter((f) => f.provenance.source === source && f.provenance.kind !== 'sample' && f.unit === 'USD' && Number.isFinite(f.value) && QUARTERLY_CONCEPTS.has(f.concept))
  if (!rows.length) return null
  const by = new Map<string, Quarter>()
  for (const f of rows) {
    const q = by.get(f.periodEnd) ?? { periodEnd: f.periodEnd, fq: null, revenue: null, capex: null, totalAssets: null, intensity: null }
    if (f.concept === 'revenue') q.revenue = f.value
    else if (f.concept === 'capex') q.capex = f.value
    else q.totalAssets = f.value
    const fq = FQ_RE.exec(f.provenance.note ?? '')
    if (fq) q.fq = +fq[1]
    by.set(f.periodEnd, q)
  }
  const quarters = [...by.values()].sort((a, b) => Date.parse(a.periodEnd) - Date.parse(b.periodEnd))
  for (const q of quarters) q.intensity = q.revenue != null && q.revenue > 0 && q.capex != null ? q.capex / q.revenue : null
  return { source, fresh: freshOf(rows[0].provenance), licence: rows[0].provenance.licence ?? null, quarters, from: quarters[0].periodEnd, through: quarters[quarters.length - 1].periodEnd }
}

export interface CloseBadge { f: Freshness; label: string; title?: string }
const ILLUS_BADGE: CloseBadge = { f: 'illustrative', label: 'Illus.' }
const BADGE_WORD: Partial<Record<Freshness, string>> = { live: 'Live', historical: 'Archived', delayed: 'Delayed', stale: 'Stale' }

/**
 * The badge a ticker-strip or list row carries for the figure it shows (`last(px)`), derived from
 * the provenance of the series point that landed in the latest week (WF7 #1): an archived
 * alpaca-data close reads "Archived · 06 Nov", a live one "Live · 06 Nov"; authored sample points
 * are never placed, so with nothing placed, or an archive that stops short of the latest week
 * (the figure is then the design's, joined), the row stays "Illus.".
 */
export function closeBadgeOf(c: Pick<CompanyLive, 'placed'> | null | undefined): CloseBadge {
  const p = c?.placed
  if (!p || p.fresh.freshness === 'illustrative') return ILLUS_BADGE
  if (!p.latestWeek) return { ...ILLUS_BADGE, title: `Last observed close ${fmtDay(p.t)} (${p.source}); later weeks illustrative` }
  const day = fmtDay(p.t).split(' ').slice(0, 2).join(' ')
  const word = BADGE_WORD[p.fresh.freshness] ?? p.fresh.freshness.charAt(0).toUpperCase() + p.fresh.freshness.slice(1)
  return { f: p.fresh.freshness, label: `${word} · ${day}`, title: `${p.source} close ${fmtDay(p.t)}` }
}

/**
 * Weekly placement, the weekly twin of the month-axis rule in src/live/README.md: each observed
 * close lands in the slot of the week ending on the first Friday on or after it; several closes in
 * one week keep the last; closes outside the axis are ignored; every other slot keeps the design
 * value (the caller then joins them onto the real weeks with `joinToNow`, G14). Authored sample
 * closes are never placed.
 */
export function placeWeekly<P extends { t: string; value: number | null; provenance?: Pick<Provenance, 'kind' | 'inputKinds'> }>(design: number[], weekEnds: Date[], points: P[]): { values: number[]; real: boolean[]; used: number; /** The point each real slot took, null elsewhere. */ picked: (P | null)[] } {
  const values = [...design]
  const real = design.map(() => false)
  const picked: (P | null)[] = design.map(() => null)
  const lastT = design.map(() => -Infinity)
  let used = 0
  if (!weekEnds.length) return { values, real, used, picked }
  const first = weekEnds[0].getTime() - 7 * DAY
  for (const p of points) {
    if (p.value == null || !Number.isFinite(p.value)) continue
    if (p.provenance && freshOf(p.provenance).freshness === 'illustrative') continue
    const t = Date.parse(p.t)
    if (Number.isNaN(t) || t <= first) continue
    const i = weekEnds.findIndex((d) => t <= d.getTime() + DAY - 1)
    if (i < 0 || i >= values.length) continue
    used++
    if (t >= lastT[i]) {
      lastT[i] = t
      values[i] = p.value
      real[i] = true
      picked[i] = p
    }
  }
  return { values, real, used, picked }
}

// ---------- Evaluation ----------
const VERDICT_STATUS: Record<Exclude<Verdict, 'reference'>, ValidationStatus> = {
  improves: 'validated',
  inconclusive: 'no-edge',
  worsens: 'no-edge',
  'insufficient-sample': 'insufficient',
}
/** The backend's own verdict rule (product/flopx/evaluation.py), applied to the pooled numbers. */
export function verdictOf(model: string, n: number, skill: number | null): Verdict {
  if (n < 20) return 'insufficient-sample'
  if (model === 'baseline') return 'reference'
  if (skill != null && skill > 0.05 && n >= 40) return 'improves'
  if (skill != null && skill < -0.05) return 'worsens'
  return 'inconclusive'
}

const MODEL_LABEL: Record<string, { model: string; short: string }> = {
  baseline: { model: 'Trailing-return baseline', short: 'Baseline' },
  augmented: { model: '+ Compute-market data', short: '+Compute' },
  combined: { model: '+ News event intensity', short: '+Events' },
}

export interface EvalAdapted {
  rows: Record<HorizonId, ModelRow[]>
  augmented: EvalLive['augmented']
  combinedScored: EvalLive['combinedScored']
  tickers: string[]
  reliabilityNote: string
  fresh: FreshTag
  failures: { date: string; what: string; why: string; lesson: string }[]
}

function pool(results: ModelResultWire[], model: string, hDays: number): ModelRow {
  const scored = results.filter((m) => m.n > 0 && m.mae != null && m.rmse != null)
  const n = scored.reduce((s, m) => s + m.n, 0)
  const label = MODEL_LABEL[model] ?? { model, short: model }
  if (!n) return { ...label, mae: null, hit: null, ic: null, sharpe: null, n: 0, ci: '—', status: 'insufficient' }
  const w = (f: (m: ModelResultWire) => number) => scored.reduce((s, m) => s + m.n * f(m), 0) / n
  const mae = w((m) => m.mae!)
  const hitRows = scored.filter((m) => m.hitRate != null)
  const hitN = hitRows.reduce((s, m) => s + m.n, 0)
  const hit = hitN ? hitRows.reduce((s, m) => s + m.n * m.hitRate!, 0) / hitN : null
  let skill: number | null = model === 'baseline' ? 0 : null
  if (model !== 'baseline') {
    const matched = scored.filter((m) => m.baselineRmseMatched != null)
    const mn = matched.reduce((s, m) => s + m.n, 0)
    if (mn) {
      const rmse = Math.sqrt(matched.reduce((s, m) => s + m.n * m.rmse! ** 2, 0) / mn)
      const base = Math.sqrt(matched.reduce((s, m) => s + m.n * m.baselineRmseMatched! ** 2, 0) / mn)
      skill = base > 0 ? 1 - rmse / base : null
    }
  }
  const verdict = verdictOf(model, n, skill)
  // Non-overlapping windows: daily origins overlap at h > 1, so the interval uses n / h per ticker.
  const nEff = scored.reduce((s, m) => s + Math.floor(m.n / hDays), 0)
  const ci = hit != null && nEff >= 2 ? `±${(1.96 * Math.sqrt((hit * (1 - hit)) / nEff)).toFixed(2)}` : '—'
  return {
    ...label,
    mae: round(mae, 3),
    hit: hit == null ? null : round(hit, 2),
    ic: null,
    sharpe: null,
    n,
    ci,
    status: verdict === 'reference' ? 'reference' : VERDICT_STATUS[verdict],
    skill: skill == null ? null : round(skill, 3),
  }
}

export function adaptEvaluation(p: EvaluationResponse): EvalAdapted {
  const hz = Object.keys(HZ_DAYS) as HorizonId[]
  const rows = {} as Record<HorizonId, ModelRow[]>
  const augmented = {} as EvalLive['augmented']
  const combinedScored = {} as EvalLive['combinedScored']
  const byH = (e: TickerEvalWire, d: number) => e.horizons.find((h) => h.horizonDays === d)
  for (const h of hz) {
    const d = HZ_DAYS[h]
    const per = p.evaluations.map((e) => byH(e, d)).filter((x): x is HorizonEvalWire => !!x)
    const of = (model: string) => per.map((x) => x.models.find((m) => m.model === model)).filter((m): m is ModelResultWire => !!m)
    rows[h] = ['baseline', 'augmented', 'combined'].map((m) => pool(of(m), m, d))
    const counts: Record<Verdict, number> = { improves: 0, inconclusive: 0, worsens: 0, 'insufficient-sample': 0, reference: 0 }
    of('augmented').forEach((m) => counts[m.verdict]++)
    augmented[h] = counts
    combinedScored[h] = of('combined').filter((m) => m.n >= 20).length
  }
  const fresh = freshOf(p.evaluations[0]?.provenance ?? { kind: 'derived', inputKinds: ['sample'] })
  const date = fmtDay(p.freshness.generatedAt)
  // Blind spots, worst first: the compute-augmented forecasts that most worsened on the baseline.
  const worst = p.evaluations
    .flatMap((e) =>
      hz.map((h) => {
        const x = byH(e, HZ_DAYS[h])
        const m = x?.models.find((mm) => mm.model === 'augmented')
        return m && m.verdict === 'worsens' && m.skillVsBaseline != null ? { e, h, m } : null
      }),
    )
    .filter((x): x is { e: TickerEvalWire; h: HorizonId; m: ModelResultWire } => !!x)
    .sort((a, b) => a.m.skillVsBaseline! - b.m.skillVsBaseline!)
    .slice(0, 3)
  const failures = worst.map(({ e, h, m }) => {
    const dropped = Object.entries(e.featuresDropped).map(([k, why]) => `${k} (${why})`)
    return {
      date,
      what: `${e.ticker} ${h} forecast with compute data: RMSE ${Math.round(-m.skillVsBaseline! * 100)}% worse than baseline · n = ${m.n}`,
      why: `Features used: ${m.featuresUsed.join(', ') || 'none'}.${dropped.length ? ` Dropped: ${dropped.join('; ')}.` : ''}`,
      lesson: `Reported as "worsens", not hidden. ${e.reliabilityNote.charAt(0).toUpperCase()}${e.reliabilityNote.slice(1)}.`,
    }
  })
  return {
    rows,
    augmented,
    combinedScored,
    tickers: p.evaluations.map((e) => e.ticker),
    reliabilityNote: p.evaluations[0]?.reliabilityNote ?? '',
    fresh,
    failures,
  }
}

// ---------- Portfolio backtests ----------
export interface PortfolioAdapted {
  stats: typeof BACKTEST_STATS
  live: Omit<PortfolioLive, 'generatedAt'>
}

/**
 * The fund's backtest panel from the backend's strategy backtests: the compute variant of every
 * strategy, combined with equal capital per strategy (so the combined period return is the mean of
 * the strategy returns). Sharpe and volatility are not in the payload and stay null ("—"), never
 * estimated. Max drawdown is the worst strategy's, an upper bound for the equal-capital mix.
 */
export function adaptPortfolio(p: PortfolioEvalResponse): PortfolioAdapted {
  const compute = p.strategies.filter((s) => s.variant === 'compute')
  const price = p.strategies.filter((s) => s.variant === 'price-only')
  const mean = (a: number[]) => (a.length ? a.reduce((s, x) => s + x, 0) / a.length : 0)
  const trips = compute.reduce((s, x) => s + (x.hitRate != null ? x.roundTrips : 0), 0)
  const hit = trips ? compute.reduce((s, x) => s + (x.hitRate != null ? x.hitRate * x.roundTrips : 0), 0) / trips : null
  const starts = compute.map((s) => s.start).sort()
  const ends = compute.map((s) => s.end).sort()
  const costsUsd = round(compute.reduce((s, x) => s + x.costsUsd, 0), 2)
  return {
    stats: {
      basis: 'backend',
      grossSharpe: null,
      netSharpe: null,
      turnover: round(mean(compute.map((s) => s.turnoverRatio)) * 100, 0),
      costBps: p.costBps,
      costDrag: null,
      costsUsd,
      maxDD: compute.length ? -round(Math.max(...compute.map((s) => s.maxDrawdownPct)), 1) : null,
      period: starts.length ? `${monYear(starts[0])} to ${monYear(ends[ends.length - 1])}` : '',
      vol: null,
      annRet: round(mean(compute.map((s) => s.totalReturnPct)), 1),
      hitRate: hit,
      trades: compute.reduce((s, x) => s + x.trades, 0),
    },
    live: {
      fresh: freshOf(compute[0]?.provenance ?? { kind: 'derived', inputKinds: p.inputKinds }),
      sleeves: compute.length,
      priceOnlyReturnPct: round(mean(price.map((s) => s.totalReturnPct)), 1),
      benchmarkReturnPct: round(p.benchmark.totalReturnPct, 1),
      benchmarkTickers: p.benchmark.tickers.length,
      costsUsd,
    },
  }
}

// ---------- Apply (mutate the design exports in place) ----------
const designPx = new Map<string, number[]>()

export function applyCompanies(rows: CompanyRow[]): void {
  for (const r of rows) {
    const prev = W2_LIVE.companies[r.ticker]
    W2_LIVE.companies[r.ticker] = { ticker: r.ticker, close: r.close, observedWeeks: prev?.observedWeeks ?? 0, placed: prev?.placed ?? null, realWeeks: prev?.realWeeks ?? [], revGrowth: prev?.revGrowth ?? null, quarterly: prev?.quarterly ?? null }
  }
}

const emptyCompany = (ticker: string): CompanyLive => ({ ticker, close: null, observedWeeks: 0, placed: null, realWeeks: [], revGrowth: null, quarterly: null })

export function applyFundamentals(ticker: string, rg: RevGrowth | null): boolean {
  const s = SECURITIES.find((x) => x.ticker === ticker)
  if (!s || !rg) return false
  s.revGrowth = Math.round(rg.pct)
  const c = (W2_LIVE.companies[ticker] ??= emptyCompany(ticker))
  c.revGrowth = rg
  return true
}

/**
 * Record (or, on a payload without them, clear) a company's archived quarterlies. Clearing matters:
 * the modules re-run on sign-out, and the anonymous detail carries no Compustat rows, so the panel
 * must fall back to what everyone sees. Returns whether anything changed.
 */
export function applyQuarterly(ticker: string, q: Quarterly | null): boolean {
  if (!SECURITIES.some((x) => x.ticker === ticker)) return false
  const c = (W2_LIVE.companies[ticker] ??= emptyCompany(ticker))
  const changed = (c.quarterly === null) !== (q === null) || (q != null && c.quarterly != null && (q.through !== c.quarterly.through || q.quarters.length !== c.quarterly.quarters.length))
  c.quarterly = q
  return changed
}

export function applyCloses(ticker: string, points: SeriesPoint[]): number {
  const s = SECURITIES.find((x) => x.ticker === ticker)
  if (!s) return 0
  if (!designPx.has(ticker)) designPx.set(ticker, [...s.px])
  const design = designPx.get(ticker)!
  // G14: observed closes replace the design's weeks; the design's other weeks are multiplied so
  // the curve joins them (src/live/README.md, "The date model").
  const weekly = placeWeekly(design, WEEK_DATES, points)
  const placed = joinToNow(weekly, design, 'multiply')
  const count = placed.real.filter(Boolean).length
  if (count) {
    placed.values.forEach((v, i) => (s.px[i] = v))
    ;(s as { pxScaled?: boolean }).pxScaled = placed.scaled
  } else {
    // Nothing observed this time (the anonymous series after a sign-out is all sample): the design
    // weeks come back, so no row shows an archive close it no longer has the right to.
    design.forEach((v, i) => (s.px[i] = v))
    ;(s as { pxScaled?: boolean }).pxScaled = false
  }
  const c = (W2_LIVE.companies[ticker] ??= emptyCompany(ticker))
  c.observedWeeks = count
  c.realWeeks = count ? [...weekly.real] : []
  const lastReal = weekly.real.lastIndexOf(true)
  const pt = lastReal >= 0 ? weekly.picked[lastReal] : null
  c.placed = pt ? { t: pt.t, value: pt.value, fresh: freshOf(pt.provenance), source: pt.provenance.source, latestWeek: lastReal === weekly.real.length - 1 } : null
  return count
}

/** Replace a security's modelled compute links with the backend's evaluation of that ticker. */
export function applyModelledLinks(p: EvaluationResponse): void {
  const closes = evalPriceBasis(p.freshness).short
  for (const e of p.evaluations) {
    const s = SECURITIES.find((x) => x.ticker === e.ticker)
    if (!s) continue
    const h = e.horizons.find((x) => x.horizonDays === HZ_DAYS['1M'])
    const m = h?.models.find((x) => x.model === 'augmented')
    if (!m) continue
    const text =
      m.verdict === 'insufficient-sample'
        ? `Compute features vs baseline at 1M: insufficient sample (n = ${m.n}), ${closes}`
        : `Compute features vs baseline at 1M: ${m.verdict}, RMSE skill ${m.skillVsBaseline! >= 0 ? '+' : '−'}${Math.abs(Math.round(m.skillVsBaseline! * 100))}% (n = ${m.n}, ${closes})`
    s.computeLinks = [...s.computeLinks.filter((l) => l.kind !== 'modelled'), { kind: 'modelled', text }]
  }
}

let evalFailures: { generatedAt: string; rows: EvalAdapted['failures']; basis: PriceBasis } | null = null

/**
 * The closes blind spot, derived from the evaluation's own price basis (WF4 #5), never from the
 * overview's unavailable list: on sample closes it is a failure entry naming the outage the backend
 * reports; on an archive it says which archive and through when, and that no live feed scores the
 * harness; on live closes there is no entry.
 */
export function closesBlindSpot(basis: PriceBasis, generatedAt: string, unavailable: Unavailable[] = W2_LIVE.unavailable): EvalAdapted['failures'] {
  const date = fmtDay(generatedAt)
  if (basis.kind === 'sample') {
    const blind = unavailable.find((u) => u.source === 'yahoo-finance') ?? unavailable.find((u) => /close|price|alpaca|yahoo|crsp/i.test(u.source))
    return [{
      date,
      what: blind ? `Equity closes: ${blind.source} unavailable (${blind.reason})` : 'Equity closes: no market feed',
      why: 'The backend could not fetch market closes, so it serves authored sample closes.',
      lesson: 'Every evaluation and backtest here runs on those sample closes until a feed returns.',
    }]
  }
  if (basis.kind === 'archived') {
    const through = basis.through ? ` through ${basis.through}` : ''
    return [{
      date,
      what: `Equity closes: ${basis.source} archive${through}, not a live feed`,
      why: `The backend serves archived ${basis.source} closes${through}; no live closes feed is configured.`,
      lesson: `Every evaluation and backtest here is ${basis.caption}; verdicts after that date wait for the archive to extend.`,
    }]
  }
  return []
}

/**
 * FAILURES = the closes blind spot (from the evaluation's price basis) + the worst compute forecasts.
 * Re-run whenever either input lands, so the result does not depend on which payload came first.
 */
export function rebuildFailures(): void {
  if (!evalFailures || !evalFailures.rows.length) return
  const head = closesBlindSpot(evalFailures.basis, evalFailures.generatedAt)
  FAILURES.splice(0, FAILURES.length, ...head, ...evalFailures.rows)
}

export function applyEvaluation(a: EvalAdapted, generatedAt: string, freshness?: W2Freshness): void {
  const priceBasis = evalPriceBasis(freshness)
  ;(Object.keys(a.rows) as HorizonId[]).forEach((h) => {
    MODEL_COMPARE[h].splice(0, MODEL_COMPARE[h].length, ...a.rows[h])
  })
  Object.assign(MODEL_COMPARE_META, { basis: 'backend', maeUnit: 'log ret.', scope: `${a.tickers.length} tickers · ${priceBasis.short}`, horizonDays: HZ_DAYS })
  evalFailures = { generatedAt, rows: a.failures, basis: priceBasis }
  rebuildFailures()
  W2_LIVE.evaluation = { generatedAt, fresh: a.fresh, tickers: a.tickers, reliabilityNote: a.reliabilityNote, priceBasis, augmented: a.augmented, combinedScored: a.combinedScored }
  setLiveTotals({ priceBasis: { source: priceBasis.source, through: priceBasis.through, kind: priceBasis.kind, label: priceBasis.label } })
}

// ---------- Signal validation (the design's "Validated at 1M (n=26)" claims) ----------

/** Each signal's pending-state support lines, captured before the first rewrite (applyForecastValidation clears the index signal's). */
const pendingSupport = new Map<string, string[]>()
const EMPTY_STATUS = (status: ValidationStatus, n: number) => ({ hit: null, n, ic: null, status, baselineHit: null })

/**
 * Rewrite every signal's validation status from the backend evaluation, so no page prints the
 * design's "Validated" where the backend says "insufficient". A signal on a ticker the backend
 * evaluated takes the compute-augmented model's verdict, n and hit rate per horizon (the backend's
 * own rule, VERDICT_STATUS); a signal on a basket or an unevaluated ticker has no backend
 * evaluation at all, so it reads insufficient with n = 0. The forecast means stay the design's
 * illustrative numbers (the backend publishes no equity forecast); only the status changes. The
 * H100 index signal is handled by applyForecastValidation. Pure apart from `signals`.
 */
export function applySignalValidation(p: EvaluationResponse, signals: Signal[] = SIGNALS): number {
  let n = 0
  const closes = evalPriceBasis(p.freshness).short
  for (const s of signals) {
    if (s.id === 'sig-h100') continue
    if (!pendingSupport.has(s.id)) pendingSupport.set(s.id, [...s.support])
    const e = p.evaluations.find((x) => x.ticker === s.asset)
    for (const h of Object.keys(HZ_DAYS) as HorizonId[]) {
      const hz = e?.horizons.find((x) => x.horizonDays === HZ_DAYS[h])
      const m = hz?.models.find((x) => x.model === 'augmented')
      const base = hz?.models.find((x) => x.model === 'baseline')
      const prev = s.horizons[h]
      s.horizons[h] = m
        ? { ...prev, ...EMPTY_STATUS(m.verdict === 'reference' ? 'insufficient' : VERDICT_STATUS[m.verdict], m.n), hit: m.hitRate, baselineHit: base?.hitRate ?? null }
        : { ...prev, ...EMPTY_STATUS('insufficient', 0) }
    }
    s.basis = 'backend'
    s.basisNote = e ? `FLOPX evaluation of ${s.asset} (${closes})` : `FLOPX has no evaluation of ${s.asset}: no scored forecast`
    n++
  }
  return n
}

/**
 * The H100 index signal's status from the backend's own forecast call (W5's timing gate reads the
 * same outcome): refused -> insufficient at every horizon, with n = the daily points the index
 * has; answered -> validated only when the gate is open (measured skill at or above the bar),
 * otherwise exploratory. The design's "Validated at 1W and 1M (n=26)" never survives a real answer.
 */
export function applyForecastValidation(f: ForecastOutcome, signals: Signal[] = SIGNALS): boolean {
  const s = signals.find((x) => x.id === 'sig-h100')
  if (!s || f.kind === 'failed') return false
  if (!pendingSupport.has(s.id)) pendingSupport.set(s.id, [...s.support])
  const gate = timingGateOf(f)
  const status: ValidationStatus = f.kind === 'refused' ? 'insufficient' : gate?.open ? 'validated' : 'exploratory'
  const n = f.kind === 'refused' ? (f.detail.history_points ?? 0) : ((f.data.backtest.find((b) => b.method === f.data.chosen) ?? f.data.backtest[0])?.origins ?? 0)
  for (const h of Object.keys(HZ_DAYS) as HorizonId[]) s.horizons[h] = { ...s.horizons[h], ...EMPTY_STATUS(status, n) }
  s.basis = 'backend'
  s.basisNote = f.kind === 'refused' ? `FLOPX refuses to forecast this index: ${f.detail.history_points ?? 0} of ${f.detail.required_points ?? '?'} daily points of history` : (gate?.note ?? 'FLOPX forecast answered')
  s.support = f.kind === 'refused' ? [] : s.support
  return true
}

/** Count of signals whose 1M status is validated, and the basis the count rests on (for the "N validated @1M" stats). */
export function validatedAt1M(signals: Signal[] = SIGNALS): { validated: number; total: number; basis: 'design' | 'pending' | 'backend' | 'mixed' } {
  const validated = signals.filter((s) => s.horizons['1M'].status === 'validated').length
  const bases = new Set(signals.map((s) => s.basis ?? 'design'))
  return { validated, total: signals.length, basis: bases.size === 1 ? [...bases][0] : 'mixed' }
}

export function applyPortfolio(a: PortfolioAdapted, generatedAt: string): void {
  Object.assign(BACKTEST_STATS, a.stats)
  W2_LIVE.portfolio = { generatedAt, ...a.live }
}

function noteUnavailable(f: W2Freshness | null | undefined) {
  let added = false
  for (const u of unavailableOf(f)) {
    if (W2_LIVE.unavailable.some((x) => x.source === u.source)) continue
    W2_LIVE.unavailable.push(u)
    added = true
  }
  if (added) rebuildFailures()
}

/** Test helper: forget everything that landed; signals return to the module-init pending state (the other design exports are not restored). */
export function __resetW2(): void {
  W2_LIVE.companies = {}
  W2_LIVE.evaluation = null
  W2_LIVE.portfolio = null
  W2_LIVE.unavailable = []
  evalFailures = null
  designPx.clear()
  for (const s of SIGNALS) {
    resetSignalToPending(s)
    const support = pendingSupport.get(s.id)
    if (support) s.support = [...support]
  }
  pendingSupport.clear()
  setLiveTotals({ priceBasis: null })
}

// ---------- The module ----------
export const W2_TICKERS_TIMEOUT_MS = 15_000
/**
 * The evaluation is the slowest read (2 to 6 s warm, longer after a restart) and the one whose
 * absence costs the most honesty, so it gets a longer timeout than the default 12 s (WF4 #6).
 * On a timeout the signals stay in the pending state; nothing from the design table comes back.
 */
export const EVALUATION_TIMEOUT_MS = 20_000

export const financeModule: LiveModule = {
  id: 'finance',
  async load(get: Getter, apply: () => void) {
    let landed = false
    const companies = get<CompaniesResponse>('/flopx/companies').then(async (r) => {
      if (!r.ok) return
      noteUnavailable(r.data.freshness)
      relabelWeeks(new Date(r.data.freshness.generatedAt))
      const rows = adaptCompanies(r.data)
      applyCompanies(rows)
      landed = true
      apply()
      const ours = rows.filter((x) => SECURITIES.some((s) => s.ticker === x.ticker))
      await Promise.all(
        ours.map(async (row) => {
          const [detail, series] = await Promise.all([
            get<CompanyDetailResponse>(`/flopx/companies/${row.ticker}`, { timeoutMs: W2_TICKERS_TIMEOUT_MS }),
            // Only an observed close is worth placing; authored sample closes stay off the chart.
            row.close && row.close.fresh.freshness !== 'illustrative' ? get<SeriesResponseW2>(`/flopx/gpu/series?id=${encodeURIComponent(`equity:${row.ticker}:close`)}`) : Promise.resolve(null),
          ])
          if (detail.ok) {
            const grew = applyFundamentals(row.ticker, revGrowthOf(detail.data.fundamentals))
            const quartered = applyQuarterly(row.ticker, quarterlyOf(detail.data.fundamentals))
            if (grew || quartered) apply()
          }
          // applyCloses also clears a previously placed archive when nothing observed lands
          // (sign-out), so the page re-renders either way.
          if (series && series.ok) {
            applyCloses(row.ticker, series.data.points)
            apply()
          }
        }),
      )
    })
    const evaluation = get<EvaluationResponse>('/flopx/research/evaluation', { timeoutMs: EVALUATION_TIMEOUT_MS }).then((r) => {
      if (!r.ok) return
      noteUnavailable(r.data.freshness)
      applyEvaluation(adaptEvaluation(r.data), r.data.freshness.generatedAt, r.data.freshness)
      applyModelledLinks(r.data)
      applySignalValidation(r.data)
      landed = true
      apply()
    })
    const portfolio = get<PortfolioEvalResponse>('/flopx/research/evaluation/portfolio').then((r) => {
      if (!r.ok) return
      applyPortfolio(adaptPortfolio(r.data), r.data.freshness.generatedAt)
      landed = true
      apply()
    })
    await Promise.all([companies, evaluation, portfolio])
    return landed
  },
}

/**
 * The H100 index signal's validation from the backend's own forecast call (POST, so not through
 * the shared GET getter; the request is cached and shared with the procurement module's timing
 * gate). Today it is a refusal, read as data. A factory so tests can inject `fetchImpl`.
 */
export function makeSignalModule(opts: { fetchImpl?: typeof fetch; base?: string } = {}): LiveModule {
  return {
    id: 'signals',
    async load(_get: Getter, apply: () => void) {
      const f = await fetchForecast(GPU_MODEL.h100, opts)
      if (f.kind === 'failed') throw new Error(f.error)
      if (!applyForecastValidation(f)) return false
      apply()
      return true
    },
  }
}
export const signalModule = makeSignalModule()
