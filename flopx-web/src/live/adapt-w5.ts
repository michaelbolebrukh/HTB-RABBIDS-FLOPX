// W5 adapter: procurement on real plans.
//
// The design's Procurement page ranked offers client-side and showed a 7 Nov hand-off snapshot
// as its "Scheduling scenarios". This module asks the backend's planner instead:
//   POST /flopx/procure/plan   a costed plan over the offers the backend holds (live asks from
//                              Vast.ai, RunPod and Akash, plus FLOPX's authored sample asks)
//   POST /flopx/forecast       the index forecast for the selected GPU (today it refuses:
//                              insufficient history), which drives the timing gate and the
//                              1M uncertainty panel
// The mapping functions are pure (payload in, design row shapes out) and tested beside this file
// on recorded payloads. The page hook (`useProcurePlan`) debounces the sliders, caches by spec and
// never throws: while a plan is pending or has failed, the page keeps the design's own ranking.
//
// Honesty rules applied here:
// - A plan rests on a live ask only when its offer id is one of the live offers the GPU module put
//   into OFFERS. Everything else is FLOPX sample tier and reads illustrative.
// - When any plan rests on a live ask, only live plans are shown (the rule W0 applied to OFFERS:
//   authored sample providers are fictional). Sample-tier plans show only when no live ask fits
//   the spec, and say so.
// - Scenario rows are separate backend requests, never invented: run now (tolerance none,
//   deadline now + duration), flexible (tolerance none, deadline now + start-by window +
//   duration), interruptible (tolerance high). "Second venue" is the cheapest plan from another
//   provider inside the flexible plan. A row with nothing behind it is omitted.
// - The design's "must start within N days" is a start deadline; the backend's `deadline` is a
//   completion deadline, so requests send now + N days + duration.
import { useEffect, useState } from 'react'
import { GPUS, OFFERS, PROVIDERS, type Freshness, type Offer, type Provider } from '@/data/core'
import type { LiveModule } from './boot'
import { apiBase, fetchJson, type FetchOpts, type LiveResult } from './client'
import { GPU_MODEL, seriesIdOf } from './gpu'
import { fmtObserved } from './provenance'
import { liveRegionName } from './regions'
import type { FreshnessBlock, Provenance } from './types'

// ---------- wire types (only the fields read here; source of truth /openapi.json) ----------

export type InterruptionTolerance = 'none' | 'low' | 'high'

export interface WorkloadWire {
  gpuModelIds: string[]
  durationHours: number
  interruptionTolerance: InterruptionTolerance
  gpuCount?: number
  gpuHours?: number
  deadline?: string
  regionIds?: string[]
  budgetUsd?: number
  performanceNeeds?: string[]
}

export interface PlanOptionWire {
  rank: number
  offerId: string
  providerId: string
  regionId: string
  gpuModelId: string
  contract: 'on-demand' | 'spot' | 'reserved' | string
  gpuHours: number
  unitPriceUsd: number
  expectedCostUsd: number
  costLowUsd: number
  costHighUsd: number
  expectedDurationHours: number
  performanceScore: number
  tradeOffs: string[]
  withinBudget: boolean | null
  meetsDeadline: boolean | null
}

export interface ProcurePlanResponse {
  workload: WorkloadWire
  plans: PlanOptionWire[]
  excluded: Record<string, number>
  caveats: string[]
  provenance: Provenance
  freshness: FreshnessBlock
}

export interface ForecastPointWire { step: number; t: string; mean: number; lower: number; upper: number }

export interface ForecastResponse {
  seriesId: string
  horizon: number
  level: number
  historyPoints: number
  baselines: { method: string; points: ForecastPointWire[]; residualStd: number }[]
  backtest: { method: string; mae: number; mape: number; maeVsNaive: number; origins: number }[]
  chosen: string
  caveats: string[]
  exploratory: boolean
  exploratoryReasons: string[]
  provenance: Provenance
  freshness: FreshnessBlock
}

/** The 422 body of /flopx/forecast when a series is too short (snake_case on the wire). */
export interface ForecastRefusal {
  detail: { code: string; reason?: string; history_points?: number; horizon?: number; required_points?: number }
}

export type ForecastOutcome =
  | { kind: 'ok'; data: ForecastResponse }
  | { kind: 'refused'; detail: ForecastRefusal['detail'] }
  | { kind: 'failed'; error: string }

// ---------- the page's spec -> a backend workload ----------

/** What the Procurement page's controls say. */
export interface ProcureSpec {
  gpu: string
  n: number
  weeks: number
  /** "Must start within" in days. */
  deadlineDays: number
  area: string
  ib: boolean
  minRel: number
  /** Budget in thousands of USD. */
  budgetK: number
}

export type ScenarioId = 'now' | 'flex' | 'interrupt'

/**
 * The design's location areas in the backend's region ids: hub ids from /flopx/map/regions plus
 * the ISO country codes Vast.ai asks carry ('us', 'ca', 'jp'). The design's own ids ('ie-dub')
 * stay in Procurement.tsx for its client-side fallback ranking.
 */
export const AREA_REGIONS: Record<string, string[]> = {
  Any: [],
  US: ['us-tx', 'us-va', 'us-az', 'us-or', 'us'],
  EU: ['eu-ie', 'eu-uk', 'eu-nl', 'eu-de', 'eu-no', 'ie', 'gb', 'nl', 'de', 'no'],
  'North America': ['us-tx', 'us-va', 'us-az', 'us-or', 'ca-qc', 'us', 'ca'],
  APAC: ['ap-sg', 'ap-jp', 'ap-in', 'ap-au', 'sg', 'jp', 'in', 'au'],
}

export const HOURS_PER_WEEK = 168
const HOUR = 3_600_000
const DAY = 24 * HOUR
/** Run now still needs the clock to move between request and plan; one hour of slack. */
const RUN_NOW_SLACK_H = 1

const isoNoMs = (d: Date) => d.toISOString().replace(/\.\d{3}Z$/, 'Z')

/** `now` floored to the hour, so a spec asked twice within an hour is one cached request. */
export const hourOf = (now: Date) => new Date(Math.floor(now.getTime() / HOUR) * HOUR)

/** Null when the design GPU has no backend model (MI300X). */
export function toWorkload(spec: ProcureSpec, scenario: ScenarioId, now: Date): WorkloadWire | null {
  const model = GPU_MODEL[spec.gpu]
  if (!model) return null
  const durationHours = spec.weeks * HOURS_PER_WEEK
  // Run now: an hour of slack on top of the hour floor, so the deadline is still ahead of the clock.
  const startBy = scenario === 'now' ? 2 * RUN_NOW_SLACK_H * HOUR : spec.deadlineDays * DAY
  const w: WorkloadWire = {
    gpuModelIds: [model],
    durationHours,
    interruptionTolerance: scenario === 'interrupt' ? 'high' : 'none',
    gpuCount: spec.n,
    deadline: isoNoMs(new Date(hourOf(now).getTime() + startBy + durationHours * HOUR)),
    budgetUsd: spec.budgetK * 1000,
  }
  const regions = AREA_REGIONS[spec.area]
  if (regions && regions.length) w.regionIds = regions
  if (spec.ib) w.performanceNeeds = ['fast-interconnect']
  return w
}

// ---------- plan -> the design's ranked rows ----------

export interface RankedOption {
  /** The backend offer id. */
  id: string
  providerId: string
  providerName: string
  regionName: string
  contract: string
  interconnect: string
  sla: string
  price: number
  observed: string
  freshness: Freshness
  /** 0 to 100; NaN when the backend reports none. */
  reliability: number
  availRisk: 'Low' | 'Medium' | 'High'
  available: Offer['available']
  risks: string[]
  tradeOffs: string[]
  /** Vast.ai's licence line, present when the plan rests on a Vast ask. */
  marketReading?: string
  total: number
  lo: number
  hi: number
  /** True when the plan rests on a live ask (its offer is one of the live OFFERS). */
  live: boolean
  interruptible: boolean
  meetsDeadline: boolean | null
  sourceUrl?: string
}

export interface RankedPlan {
  options: RankedOption[]
  /** 'live': rows rest on live asks; 'sample': only FLOPX sample asks fit; 'none': nothing fits. */
  tier: 'live' | 'sample' | 'none'
  freshness: Freshness
  /** "08 Nov 02:10" of the payload. */
  asOf: string
  /** Live asks the planner saw for this GPU before the spec filtered them (gpu-count etc.). */
  excluded: Record<string, number>
  caveats: string[]
  /** Plans the backend returned before the minimum-reliability filter. */
  returned: number
}

export const TRADE_OFF_COPY: Record<string, string> = {
  interruptible: 'Interruptible capacity: an assumed restart overhead is included in cost and duration',
  commitment: 'Reserved commitment',
  'minimum-commitment-exceeds-duration': 'Minimum commitment exceeds the workload, so you pay for idle time',
  'limited-availability': 'Capacity limited',
  'lower-reliability': 'Provider reliability below 95/100',
  'pcie-interconnect': 'PCIe interconnect, not NVLink',
  'low-memory': 'Less than 80 GB per GPU',
  'quoted-far-below-comparable': 'Quoted far below comparable asks',
  'misses-deadline': 'Expected duration overruns the start-by window',
}

export const CAVEAT_COPY: Record<string, string> = {
  'advertised-asks-not-quotes': 'advertised asks, not quotes',
  'spot-overhead-assumed': 'spot restart overhead assumed',
  'no-egress-or-storage-costs': 'no egress or storage costs',
  'no-matching-offer': 'no matching offer',
}

export const VAST_MARKET_READING = 'Vast.ai asks: a current market reading, not an index or benchmark'

const titleCase = (id: string) => id.split(/[-_]/).map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')

export function contractLabel(c: string): string {
  if (c === 'spot') return 'Spot'
  if (c === 'reserved') return 'Reserved'
  if (c === 'on-demand') return 'On-demand'
  return titleCase(c)
}

export interface AdaptOpts {
  offers?: Offer[]
  providers?: Provider[]
  /** Drop plans whose provider reports a reliability below this (0 to 100). Unknown passes. */
  minRel?: number
  regionName?: (id: string) => string
}

function toOption(p: PlanOptionWire, offer: Offer | undefined, provider: Provider | undefined, regionName: (id: string) => string): RankedOption {
  const live = !!offer
  const interruptible = p.contract === 'spot' || p.tradeOffs.includes('interruptible')
  const available: Offer['available'] = offer?.available ?? (p.tradeOffs.includes('limited-availability') ? 'Limited' : 'Available')
  const risks = p.tradeOffs.map((t) => TRADE_OFF_COPY[t] ?? t.replace(/-/g, ' '))
  if (!live) risks.unshift('FLOPX sample ask: an authored example, not a live quote')
  if (p.withinBudget === false) risks.push('Over budget')
  const vast = p.providerId === 'vast-ai'
  return {
    id: p.offerId,
    providerId: p.providerId,
    providerName: provider?.name ?? titleCase(p.providerId),
    regionName: regionName(p.regionId),
    contract: offer?.contract ?? contractLabel(p.contract),
    interconnect: offer?.interconnect ?? 'Not reported',
    sla: offer?.sla ?? 'n/a',
    price: p.unitPriceUsd,
    observed: offer?.observed ?? 'n/a',
    freshness: live ? offer.freshness : 'illustrative',
    reliability: provider && Number.isFinite(provider.reliability) ? provider.reliability : NaN,
    availRisk: available === 'Available' ? 'Low' : available === 'Limited' ? 'Medium' : 'High',
    available,
    risks,
    tradeOffs: p.tradeOffs,
    marketReading: vast ? VAST_MARKET_READING : undefined,
    total: p.expectedCostUsd,
    lo: p.costLowUsd,
    hi: p.costHighUsd,
    live,
    interruptible,
    meetsDeadline: p.meetsDeadline,
    sourceUrl: offer?.sourceUrl,
  }
}

/**
 * Backend plan -> the design's ranked rows, in the backend's rank order (expected cost per unit
 * of performance, deadline misses last). Live plans hide sample ones; sample plans show only when
 * no live ask fits, and every one of them reads illustrative.
 */
export function adaptPlan(resp: ProcurePlanResponse, opts: AdaptOpts = {}): RankedPlan {
  const offers = opts.offers ?? OFFERS
  const providers = opts.providers ?? PROVIDERS
  const regionName = opts.regionName ?? liveRegionName
  const liveById = new Map(offers.filter((o) => o.live).map((o) => [o.id, o]))
  const all = (resp.plans ?? []).map((p) => toOption(p, liveById.get(p.offerId), providers.find((x) => x.id === p.providerId), regionName))
  const anyLive = all.some((o) => o.live)
  const shown = (anyLive ? all.filter((o) => o.live) : all).filter((o) => !(opts.minRel != null && Number.isFinite(o.reliability) && o.reliability < opts.minRel))
  const tier: RankedPlan['tier'] = shown.length === 0 ? 'none' : anyLive ? 'live' : 'sample'
  return {
    options: shown,
    tier,
    freshness: tier === 'live' ? (shown.every((o) => o.freshness === 'live') ? 'live' : 'historical') : 'illustrative',
    asOf: fmtObserved(resp.freshness?.generatedAt),
    excluded: resp.excluded ?? {},
    caveats: (resp.caveats ?? []).map((c) => CAVEAT_COPY[c] ?? c.replace(/-/g, ' ')),
    returned: all.length,
  }
}

/** The cheapest option by expected cost, deadline misses excluded unless nothing else exists. */
export function cheapestOf(plan: RankedPlan | null | undefined, pick: (o: RankedOption) => boolean = () => true): RankedOption | null {
  if (!plan) return null
  const xs = plan.options.filter(pick)
  const ok = xs.filter((o) => o.meetsDeadline !== false)
  const pool = ok.length ? ok : xs
  return pool.length ? pool.reduce((a, b) => (b.total < a.total ? b : a)) : null
}

// ---------- scenarios ----------

export interface ScenarioRow {
  id: 'other' | 'now' | 'flex' | 'interrupt'
  label: string
  cost: number
  note: string
  feasible: boolean
  recommended?: boolean
  live: boolean
}

export interface Scenarios {
  rows: ScenarioRow[]
  /** Median expected cost over the flexible plan's options; null with no options. */
  median: number | null
  /** How many asks the median spans. */
  n: number
  freshness: Freshness
  /** "8 × H100 SXM for 12 weeks, start within 30 days" */
  job: string
  asOf: string
}

const median = (a: number[]) => {
  const s = [...a].sort((x, y) => x - y)
  return s.length ? (s.length % 2 ? s[(s.length - 1) / 2] : (s[s.length / 2 - 1] + s[s.length / 2]) / 2) : NaN
}

const where = (o: RankedOption) => `${o.providerName} · ${o.regionName} · $${o.price.toFixed(2)} per GPU-hour`

export function jobLabel(spec: ProcureSpec): string {
  const g = GPUS.find((x) => x.id === spec.gpu)
  return `${spec.n} × ${(g?.name ?? spec.gpu).replace(' SXM', '').replace(' 80GB', '')} · ${spec.weeks} wk · start within ${spec.deadlineDays} d`
}

/**
 * The design's four scenario rows from three backend plans. Rows whose plan has nothing behind
 * them are omitted rather than invented. `timingOpen` says whether the forecast may time the
 * purchase (today it never does: the forecast refuses on insufficient history), so Flexible is
 * explained as equal to Run now when it is.
 */
export function buildScenarios(spec: ProcureSpec, now: RankedPlan | null, flex: RankedPlan | null, interrupt: RankedPlan | null, timingOpen: boolean): Scenarios | null {
  const nowBest = cheapestOf(now, (o) => !o.interruptible)
  const flexBest = cheapestOf(flex, (o) => !o.interruptible)
  const other = flexBest ? cheapestOf(flex, (o) => !o.interruptible && o.providerId !== flexBest.providerId) : null
  const intBest = cheapestOf(interrupt, (o) => o.interruptible)
  const rows: ScenarioRow[] = []
  if (other) rows.push({ id: 'other', label: 'Second venue', cost: other.total, feasible: true, live: other.live, note: where(other) })
  if (nowBest) rows.push({ id: 'now', label: 'Run now', cost: nowBest.total, feasible: true, live: nowBest.live, note: `Cheapest ask that can start now: ${where(nowBest)}` })
  if (flexBest) {
    const same = nowBest != null && Math.abs(flexBest.total - nowBest.total) < 0.005
    rows.push({ id: 'flex', label: 'Flexible', cost: flexBest.total, feasible: true, live: flexBest.live, note: same ? (timingOpen ? 'Same as run now: no cheaper ask within the window' : 'Same as run now, because the timing gate is closed') : `Cheapest ask within the start-by window: ${where(flexBest)}` })
  }
  if (intBest) rows.push({ id: 'interrupt', label: 'Interruptible', cost: intBest.total, feasible: false, live: intBest.live, note: `${where(intBest)}; this workload does not allow interruption` })
  if (!rows.length) return null
  const feasible = rows.filter((r) => r.feasible)
  if (feasible.length) feasible.reduce((a, b) => (b.cost < a.cost ? b : a)).recommended = true
  const med = flex && flex.options.length ? median(flex.options.filter((o) => !o.interruptible).map((o) => o.total)) : NaN
  const src = flex ?? now ?? interrupt
  return {
    rows,
    median: Number.isFinite(med) ? med : null,
    n: flex ? flex.options.filter((o) => !o.interruptible).length : 0,
    freshness: rows.every((r) => r.live) ? (src?.freshness ?? 'live') : 'illustrative',
    job: jobLabel(spec),
    asOf: src?.asOf ?? '',
  }
}

// ---------- timing gate and uncertainty from the forecast ----------

export interface TimingGate {
  open: boolean
  historyPoints: number
  historyNeeded: number
  /** Skill over the naive forecast in %, null when the backtest never ran. */
  skillPct: number | null
  skillNeeded: number
  note: string
}

/** The design's bar: a forecast may time a purchase only with a measured skill of at least this. */
export const SKILL_NEEDED_PCT = 5
export const FORECAST_HORIZON_DAYS = 30

/** Null when the forecast call failed outright (the page then keeps the design's snapshot). */
export function timingGateOf(f: ForecastOutcome | null | undefined, horizon = FORECAST_HORIZON_DAYS): TimingGate | null {
  if (!f || f.kind === 'failed') return null
  if (f.kind === 'refused') {
    const pts = f.detail.history_points ?? 0
    const need = f.detail.required_points ?? 2 * (f.detail.horizon ?? horizon)
    return { open: false, historyPoints: pts, historyNeeded: need, skillPct: null, skillNeeded: SKILL_NEEDED_PCT, note: `FLOPX refuses to forecast this index: ${pts} of ${need} daily points of history (${f.detail.reason ?? f.detail.code}). The saving comes from search and feasibility, not from timing the market.` }
  }
  const d = f.data
  const bt = d.backtest.find((b) => b.method === d.chosen) ?? d.backtest[0]
  const skill = bt && Number.isFinite(bt.maeVsNaive) ? (1 - bt.maeVsNaive) * 100 : null
  const open = !d.exploratory && skill != null && skill >= SKILL_NEEDED_PCT
  return {
    open,
    historyPoints: d.historyPoints,
    historyNeeded: 2 * d.horizon,
    skillPct: skill,
    skillNeeded: SKILL_NEEDED_PCT,
    note: open ? `The ${d.chosen} baseline beat the naive forecast by ${skill!.toFixed(1)}% over ${bt?.origins ?? 0} rolling origins.` : `The forecast is exploratory (${d.exploratoryReasons.join(', ') || 'no measured skill'}). The saving comes from search and feasibility, not from timing the market.`,
  }
}

/** The chosen baseline's end-of-horizon change versus the current index, in %, as p10/mean/p90. */
export function uncertaintyOf(f: ForecastResponse, current: number): { p10: number; mean: number; p90: number } | null {
  if (!Number.isFinite(current) || current <= 0) return null
  const b = f.baselines.find((x) => x.method === f.chosen) ?? f.baselines[0]
  const last = b?.points[b.points.length - 1]
  if (!last) return null
  const pct = (v: number) => (v / current - 1) * 100
  return { p10: pct(last.lower), mean: pct(last.mean), p90: pct(last.upper) }
}

// ---------- fetching: cached by workload, de-duplicated in flight ----------

export const PLAN_TIMEOUT_MS = 15_000

const planCache = new Map<string, Promise<LiveResult<ProcurePlanResponse>>>()
const forecastCache = new Map<string, Promise<ForecastOutcome>>()

export function planKey(w: WorkloadWire): string {
  return JSON.stringify(w)
}

export function fetchPlan(w: WorkloadWire, opts: Pick<FetchOpts, 'fetchImpl' | 'base'> = {}): Promise<LiveResult<ProcurePlanResponse>> {
  const k = planKey(w)
  let p = planCache.get(k)
  if (!p) {
    p = fetchJson<ProcurePlanResponse>('/flopx/procure/plan', { method: 'POST', body: { workload: w }, timeoutMs: PLAN_TIMEOUT_MS, ...opts }).then((r) => {
      // A failed call is not cached, so the next slider move retries.
      if (!r.ok) planCache.delete(k)
      return r
    })
    planCache.set(k, p)
  }
  return p
}

export function fetchForecast(model: string, opts: Pick<FetchOpts, 'fetchImpl' | 'base'> = {}, horizon = FORECAST_HORIZON_DAYS): Promise<ForecastOutcome> {
  const k = `${model}:${horizon}`
  let p = forecastCache.get(k)
  if (!p) {
    p = (async (): Promise<ForecastOutcome> => {
      const body = { seriesId: seriesIdOf(model), horizon, level: 0.8 }
      const f = opts.fetchImpl ?? fetch
      // fetchJson drops the body of a non-2xx response; the refusal's body is the point here.
      try {
        const ctrl = new AbortController()
        const timer = setTimeout(() => ctrl.abort(), PLAN_TIMEOUT_MS)
        const res = await f(`${opts.base ?? apiBase()}/flopx/forecast`, { method: 'POST', headers: { 'content-type': 'application/json', accept: 'application/json' }, body: JSON.stringify(body), signal: ctrl.signal })
        clearTimeout(timer)
        if (res.ok) return { kind: 'ok', data: (await res.json()) as ForecastResponse }
        if (res.status === 422) {
          const j = (await res.json()) as ForecastRefusal
          if (j?.detail?.code) return { kind: 'refused', detail: j.detail }
        }
        return { kind: 'failed', error: `HTTP ${res.status}` }
      } catch (e) {
        return { kind: 'failed', error: e instanceof Error ? e.message : String(e) }
      }
    })().then((r) => {
      if (r.kind === 'failed') forecastCache.delete(k)
      return r
    })
    forecastCache.set(k, p)
  }
  return p
}

/** Test helper. */
export function __resetProcureCaches(): void {
  planCache.clear()
  forecastCache.clear()
}

// ---------- the page hook ----------

export interface ProcureState {
  status: 'idle' | 'pending' | 'live' | 'failed'
  /** The ranked options for the page's spec (the flexible scenario). */
  plan: RankedPlan | null
  scenarios: Scenarios | null
  forecast: ForecastOutcome | null
  gate: TimingGate | null
  error?: string
}

export const EMPTY_STATE: ProcureState = { status: 'idle', plan: null, scenarios: null, forecast: null, gate: null }

/** Debounce between a slider move and the request; the design's sliders fire on every pixel. */
export const DEBOUNCE_MS = 350

/** Fetch and adapt the three plans and the forecast for a spec. Pure apart from the network. */
export async function loadProcure(spec: ProcureSpec, now: Date = new Date(), opts: Pick<FetchOpts, 'fetchImpl' | 'base'> = {}): Promise<ProcureState> {
  const flexW = toWorkload(spec, 'flex', now)
  if (!flexW) return { ...EMPTY_STATE, status: 'failed', error: `no backend coverage for ${spec.gpu}` }
  const model = flexW.gpuModelIds[0]
  const [flexR, nowR, intR, fc] = await Promise.all([
    fetchPlan(flexW, opts),
    fetchPlan(toWorkload(spec, 'now', now)!, opts),
    fetchPlan(toWorkload(spec, 'interrupt', now)!, opts),
    fetchForecast(model, opts),
  ])
  if (!flexR.ok) return { ...EMPTY_STATE, status: 'failed', forecast: fc, gate: timingGateOf(fc), error: flexR.error }
  const adapt = (r: LiveResult<ProcurePlanResponse>) => (r.ok ? adaptPlan(r.data, { minRel: spec.minRel }) : null)
  const plan = adaptPlan(flexR.data, { minRel: spec.minRel })
  const gate = timingGateOf(fc)
  return { status: 'live', plan, scenarios: buildScenarios(spec, adapt(nowR), plan, adapt(intR), gate?.open ?? false), forecast: fc, gate }
}

export function specKey(spec: ProcureSpec): string {
  return JSON.stringify(spec)
}

/** The page hook: the state for the current spec, debounced; the previous spec's state stays on screen until the new one lands. */
export function useProcurePlan(spec: ProcureSpec): ProcureState {
  const key = specKey(spec)
  const [state, setState] = useState<ProcureState>(EMPTY_STATE)
  useEffect(() => {
    let alive = true
    setState((s) => ({ ...s, status: 'pending' }))
    const t = setTimeout(() => {
      void loadProcure(JSON.parse(key) as ProcureSpec).then((next) => {
        if (alive) setState(next)
      })
    }, DEBOUNCE_MS)
    return () => {
      alive = false
      clearTimeout(t)
    }
  }, [key])
  return state
}

// ---------- boot module: warm the default spec so the page lands quickly ----------

/** The page's defaults (Procurement.tsx). */
export const DEFAULT_SPEC: ProcureSpec = { gpu: 'h100', n: 64, weeks: 12, deadlineDays: 30, area: 'Any', ib: false, minRel: 80, budgetK: 1500 }

export const procureModule: LiveModule = {
  id: 'procure',
  load: async () => {
    const s = await loadProcure(DEFAULT_SPEC)
    if (s.status === 'failed') throw new Error(s.error ?? 'plan unavailable')
    return true
  },
}
