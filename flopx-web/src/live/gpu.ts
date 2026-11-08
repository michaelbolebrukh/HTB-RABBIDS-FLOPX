// Live module 'gpu': the FLOPX GPU rental index (level and history), offers and providers.
//
// Reads: /flopx/gpu/index, /flopx/gpu/series?id=index:fcpi:<model> (one per mapped GPU),
// /flopx/gpu/series?id=gpu:<model>:getdeploying:weekly-median&range=2y (GetDeploying's archived
// weekly medians, CC BY 4.0, drawn as the chart's real past months), /flopx/gpu/offers,
// /flopx/map/regions (hub names for offer regions), /flopx/overview (index change, when the
// backend has one). Writes, in place: GPUS[*].index + GPUS[*].live, OFFERS, PROVIDERS. Anything
// without backend data keeps the design value, marked illustrative.
import { GPUS, OFFERS, PROVIDERS, regionById, type Gpu, type GpuChange30, type GpuHistory, type Offer, type Provider } from '@/data/core'
import type { Getter, LiveModule } from './boot'
import { AXIS_NOW_IDX, joinToNow, placeOnAxis, realSpan, slotOf, type PlacedSeries } from './dates'
import { fmtDay, fmtObserved, freshOf } from './provenance'
import { setHubNames } from './regions'
import { setLiveTotals, type LiveOffersTotals } from './store'
import type { GpuIndexResponse, GpuIndexWire, GpuSeriesResponse, MapRegionsResponse, OfferWire, OffersResponse, OverviewResponse, PressureResponse, SeriesPoint } from './types'
import { liveRegionName, liveRegionShort } from './regions'

/** Design GPU id -> backend gpuModelId. MI300X has no backend coverage and stays illustrative. */
export const GPU_MODEL: Record<string, string> = {
  b200: 'b200-sxm-192gb',
  h200: 'h200-sxm-141gb',
  h100: 'h100-sxm-80gb',
  a100: 'a100-sxm-80gb',
  l40s: 'l40s-48gb',
}
export const MODEL_GPU: Record<string, string> = Object.fromEntries(Object.entries(GPU_MODEL).map(([k, v]) => [v, k]))
export const seriesIdOf = (model: string) => `index:fcpi:${model}`
/** GetDeploying's weekly median of advertised on-demand asks for the model's family, archived by the backend (CC BY 4.0). */
export const gdhSeriesIdOf = (model: string) => `gpu:${model}:getdeploying:weekly-median`
/** The attribution CC BY 4.0 asks for, shown once wherever the series is drawn. */
export const GDH_ATTRIBUTION = 'GetDeploying (2026). GPU rental price history, CC BY 4.0'
export const GDH_URL = 'https://getdeploying.com/dataset/gpu-prices'
export const GDH_RANGE = '2y'
/** The badge a chart or spark carries when its past months are that series. */
export const GDH_BADGE_LABEL = 'GetDeploying weekly median · CC BY 4.0 · archived'
/** The label of the FLOPX index's own marks drawn beside that history. */
export const FCPI_MARKS_LABEL = 'FLOPX index (own days)'

/**
 * The delta and note of an Overview GPU index card. The delta is a 30-day change where one exists
 * (FLOPX's own once its index spans 30 days, else GetDeploying's, labelled as such), otherwise the
 * FLOPX index's own short window; the design's delta is never shown as data once the index is live.
 */
export function gpuCardFigures(g: Gpu): { delta: number | null; note: string } {
  const l = g.live
  if (!l) return { delta: ((g.index[AXIS_NOW_IDX] - g.index[AXIS_NOW_IDX - 1]) / g.index[AXIS_NOW_IDX - 1]) * 100, note: g.id === 'b200' ? 'Observed · launched Apr 25' : 'Observed · median on-demand' }
  const change = l.chg30 ? `${l.chg30.label} (${l.chg30.from} to ${l.chg30.to})` : l.chgWindow
  const spark = l.history ? ` · spark: ${GDH_BADGE_LABEL}` : l.scaled ? ' · spark: history scaled to today, illustrative' : ''
  return { delta: l.chg30 ? l.chg30.pct : l.chgPct, note: `FLOPX index now · ${change}${spark}` }
}

const DAY_MS = 86_400_000
type TPoint = { t: string; value: number | null }
const finitePts = (points: TPoint[]) => points.filter((p): p is { t: string; value: number } => p.value != null && Number.isFinite(p.value)).sort((a, b) => Date.parse(a.t) - Date.parse(b.t))

/**
 * Weekly points (each stamped at its week's end) -> one mean per axis slot, for the past and present
 * slots only. A week belongs to the UTC month of its end stamp, the same rule `placeOnAxis` uses
 * for every point; the current month's mean is month to date.
 */
export function monthlyMeans(points: TPoint[], now?: Date): { slot: number; mean: number; weeks: number }[] {
  const acc = new Map<number, { sum: number; weeks: number }>()
  for (const p of finitePts(points)) {
    const i = slotOf(p.t, now)
    if (i < 0 || i > AXIS_NOW_IDX) continue
    const a = acc.get(i) ?? { sum: 0, weeks: 0 }
    a.sum += p.value
    a.weeks++
    acc.set(i, a)
  }
  return [...acc.entries()].sort((a, b) => a[0] - b[0]).map(([slot, a]) => ({ slot, mean: +(a.sum / a.weeks).toFixed(4), weeks: a.weeks }))
}

/**
 * The change over about 30 days: the last point against the point nearest to 30 days before it.
 * Null when the series does not reach back far enough (`minDays`), so a few hours of FLOPX index
 * points never pass as a 30-day change.
 */
export function change30(points: TPoint[], source: GpuChange30['source'], label: string, minDays = 25): GpuChange30 | null {
  const pts = finitePts(points)
  if (pts.length < 2) return null
  const last = pts[pts.length - 1]
  const target = Date.parse(last.t) - 30 * DAY_MS
  let best = pts[0]
  for (const p of pts.slice(0, -1)) if (Math.abs(Date.parse(p.t) - target) < Math.abs(Date.parse(best.t) - target)) best = p
  const spanDays = (Date.parse(last.t) - Date.parse(best.t)) / DAY_MS
  if (spanDays < minDays || !best.value) return null
  return { pct: ((last.value - best.value) / best.value) * 100, source, label, from: fmtDay(best.t), to: fmtDay(last.t), spanDays: Math.round(spanDays) }
}

const designIndex = new Map<string, number[]>()
function designOf(g: Gpu): number[] {
  if (!designIndex.has(g.id)) designIndex.set(g.id, [...g.index])
  return designIndex.get(g.id)!
}

function chgOf(points: SeriesPoint[]): { pct: number | null; window: string } {
  const pts = points.filter((p) => p.value != null && Number.isFinite(p.value)).sort((a, b) => Date.parse(a.t) - Date.parse(b.t))
  if (pts.length < 2 || pts[0].t === pts[pts.length - 1].t || !pts[0].value) return { pct: null, window: 'one FLOPX point so far' }
  const a = pts[0]
  const b = pts[pts.length - 1]
  return { pct: ((b.value - a.value) / a.value) * 100, window: `since ${fmtObserved(a.t)} UTC (first FLOPX point)` }
}

/**
 * Apply one backend index (+ optional fuller history, + optional GetDeploying weekly medians) to a
 * design GPU. Pure apart from `g`.
 *
 * With `gdh`, the chart's past months are GetDeploying's weekly medians as monthly means (real,
 * archived), the FLOPX index's own points are kept as separate marks (`indexMarks`), and the Now
 * slot is GetDeploying's month to date so the drawn line is one source end to end; the FLOPX
 * present fills Now only in a month no GetDeploying week has yet landed in. The design forecast
 * then joins that line (G14). Without `gdh` nothing here changes.
 */
export function applyIndexToGpu(g: Gpu, ix: GpuIndexWire, history?: SeriesPoint[], now?: Date, gdh?: SeriesPoint[]): void {
  const design = designOf(g)
  const points = history && history.length ? history : ix.values
  // G14: real months replace the design's; the design's other months (history and forecast) are
  // multiplied so the curve joins the real present. See src/live/README.md.
  const fcpi = placeOnAxis(design, points, now)
  let base: PlacedSeries = fcpi
  let hist: GpuHistory | undefined
  const weekly = gdh ? finitePts(gdh) : []
  const monthly = monthlyMeans(weekly, now)
  if (monthly.length) {
    const values = [...design]
    const real = design.map(() => false)
    const slots = design.map(() => false)
    for (const m of monthly) {
      values[m.slot] = m.mean
      real[m.slot] = true
      slots[m.slot] = true
    }
    if (!real[AXIS_NOW_IDX] && fcpi.real[AXIS_NOW_IDX]) {
      values[AXIS_NOW_IDX] = fcpi.values[AXIS_NOW_IDX]
      real[AXIS_NOW_IDX] = true
    }
    base = { values, real, used: fcpi.used + weekly.length }
    const p0 = gdh!.find((p) => p.provenance)?.provenance
    hist = {
      source: 'getdeploying',
      seriesId: gdhSeriesIdOf(ix.gpuModelId),
      licence: p0?.licence ?? 'CC BY 4.0',
      attribution: GDH_ATTRIBUTION,
      sourceUrl: p0?.sourceUrl ?? GDH_URL,
      weeks: weekly.length,
      slots,
      monthly,
      weekly: weekly.map((p) => ({ t: p.t, value: p.value })),
      first: fmtDay(weekly[0].t),
      last: fmtDay(weekly[weekly.length - 1].t),
    }
  }
  const placed = joinToNow(base, design, 'multiply')
  const latest = [...points].sort((a, b) => Date.parse(b.t) - Date.parse(a.t))[0]
  const current = ix.current ?? latest?.value ?? null
  if (current == null) return
  const tag = freshOf(ix.provenance)
  const chg = chgOf(points)
  g.index = placed.values
  g.live = {
    modelId: ix.gpuModelId,
    seriesId: ix.id,
    current,
    freshness: tag.freshness,
    freshLabel: tag.label,
    observed: fmtObserved(ix.provenance.observedAt ?? ix.lastUpdated),
    real: placed.real,
    scaled: placed.scaled,
    bandFactor: placed.bandFactor,
    designIndex: design,
    chgPct: chg.pct,
    chgWindow: chg.window,
    coverage: ix.coverage,
    basket: ix.basket,
    limitations: ix.limitations,
    methodology: ix.methodology,
    historyPoints: points.length,
    history: hist,
    indexMarks: hist ? fcpi.values.map((v, i) => (fcpi.real[i] ? v : null)) : undefined,
    // The 30-day change: FLOPX's own once its index reaches back 30 days, else GetDeploying's,
    // never the design's. Labelled so a card can say which.
    chg30: change30(points, 'flopx', '30d · FLOPX index') ?? (hist ? change30(weekly, 'getdeploying', '30d · GetDeploying') : null),
  }
}

const PROVIDER_META: Record<string, { name: string; type: Provider['type']; note: string }> = {
  'vast-ai': { name: 'Vast.ai', type: 'Marketplace', note: 'Peer hosts; a current market reading, not an index' },
  runpod: { name: 'RunPod', type: 'Marketplace', note: 'Community cloud list prices; region not published' },
  akash: { name: 'Akash Network', type: 'Marketplace', note: 'Decentralised market; small population' },
}
const titleCase = (id: string) => id.split(/[-_]/).map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')

export function mapContract(w: Pick<OfferWire, 'contract' | 'interruptible' | 'minDurationHours'>): Offer['contract'] {
  if (w.interruptible || w.contract === 'spot') return 'Spot'
  if (w.contract === 'reserved') return w.minDurationHours != null && w.minDurationHours >= 24 * 365 ? '1-yr reserved' : '1-mo reserved'
  return 'On-demand'
}

export function mapAvailability(a: string): Offer['available'] {
  return a === 'available' ? 'Available' : a === 'limited' ? 'Limited' : 'Sold out'
}

/** Backend offer -> design Offer; null for GPU models the design has no tab for. */
export function mapOffer(w: OfferWire): Offer | null {
  const gpu = MODEL_GPU[w.gpuModelId]
  if (!gpu || !Number.isFinite(w.pricePerGpuHourUsd)) return null
  const vast = w.providerId === 'vast-ai'
  return {
    id: w.id,
    provider: w.providerId,
    gpu,
    region: w.regionId,
    price: w.pricePerGpuHourUsd,
    contract: mapContract(w),
    interconnect: 'Not reported',
    nodeSize: w.gpuCount,
    available: mapAvailability(w.availability),
    observed: fmtObserved(w.provenance.observedAt),
    freshness: freshOf(w.provenance).freshness,
    egress: 'n/a',
    sla: 'n/a',
    note: vast ? 'market reading, not an index' : undefined,
    detail: w.provenance.note ?? undefined,
    sourceUrl: w.provenance.sourceUrl ?? undefined,
    live: true,
  }
}

const median = (a: number[]) => {
  const s = [...a].sort((x, y) => x - y)
  return s.length ? (s.length % 2 ? s[(s.length - 1) / 2] : (s[s.length / 2 - 1] + s[s.length / 2]) / 2) : NaN
}

/**
 * Replace OFFERS in place. Backend offers whose provenance is 'sample' (authored FLOPX samples)
 * are left out: they are fictional providers, and the design's own illustrative offers already
 * fill a GPU that has no real coverage. A design GPU with zero real offers (MI300X today) keeps
 * the design's illustrative offers so its tab is never empty; they carry the illustrative badge.
 */
/**
 * Payload totals over every non-sample offer, whatever its GPU model (the design has tabs for five
 * models; the backend lists more). Read by the tour's retrieval counters (WF4 #4).
 */
export function offerTotals(resp: OffersResponse): LiveOffersTotals {
  const live = resp.offers.filter((w) => w.provenance.kind !== 'sample')
  const kinds = new Set(live.map((w) => w.provenance.kind))
  return {
    count: live.length,
    providers: new Set(live.map((w) => w.providerId)).size,
    regions: new Set(live.map((w) => w.regionId)).size,
    models: new Set(live.map((w) => w.gpuModelId)).size,
    total: resp.offers.length,
    freshness: !live.length ? 'unavailable' : kinds.has('live') ? 'live' : 'historical',
  }
}

/** Live binding of the offers totals (the same object as LIVE_TOTALS.liveOffers), null until the payload lands. */
export let OFFER_TOTALS: LiveOffersTotals | null = null

export function applyOffers(resp: OffersResponse, offers: Offer[] = OFFERS, providers: Provider[] = PROVIDERS): number {
  OFFER_TOTALS = offerTotals(resp)
  setLiveTotals({ liveOffers: OFFER_TOTALS })
  const real = resp.offers.filter((w) => w.provenance.kind !== 'sample').map(mapOffer).filter((o): o is Offer => o != null)
  if (!real.length) return 0
  const byProv = new Map<string, OfferWire[]>()
  for (const w of resp.offers) if (w.provenance.kind !== 'sample' && MODEL_GPU[w.gpuModelId]) byProv.set(w.providerId, [...(byProv.get(w.providerId) ?? []), w])
  for (const [id, ws] of byProv) {
    const meta = PROVIDER_META[id] ?? { name: titleCase(id), type: 'Marketplace' as const, note: 'Provider type not reported by FLOPX' }
    const scores = ws.map((w) => w.reliabilityScore).filter((v): v is number => v != null && Number.isFinite(v))
    const rel = scores.length ? Math.round(median(scores) * 100) : NaN
    const tag = freshOf(ws[0].provenance)
    const next: Provider = { id, name: meta.name, type: meta.type, reliability: rel, note: meta.note, freshness: tag.freshness, sourceUrl: ws[0].provenance.sourceUrl ?? undefined }
    const i = providers.findIndex((p) => p.id === id)
    if (i >= 0) providers[i] = { ...providers[i], ...next, ticker: providers[i].ticker }
    else providers.push(next)
  }
  const covered = new Set(real.map((o) => o.gpu))
  const keep = offers.filter((o) => !o.live && !covered.has(o.gpu))
  offers.length = 0
  offers.push(...real, ...keep)
  return real.length
}

export const gpuModule: LiveModule = {
  id: 'gpu',
  async load(get: Getter, apply: () => void) {
    let any = false

    const indexBranch = (async () => {
      const [ix, ov] = await Promise.all([get<GpuIndexResponse>('/flopx/gpu/index'), get<OverviewResponse>('/flopx/overview')])
      if (!ix.ok) return
      // The FLOPX index history and GetDeploying's weekly medians, one pair per mapped model, in
      // parallel; a GetDeploying series that is missing or times out leaves that GPU as before.
      const [series, gdh] = await Promise.all([
        Promise.all(GPUS.map((g) => (GPU_MODEL[g.id] ? get<GpuSeriesResponse>(`/flopx/gpu/series?id=${encodeURIComponent(seriesIdOf(GPU_MODEL[g.id]))}`) : Promise.resolve(null)))),
        Promise.all(GPUS.map((g) => (GPU_MODEL[g.id] ? get<GpuSeriesResponse>(`/flopx/gpu/series?id=${encodeURIComponent(gdhSeriesIdOf(GPU_MODEL[g.id]))}&range=${GDH_RANGE}`) : Promise.resolve(null)))),
      ])
      GPUS.forEach((g, k) => {
        const w = ix.data.indices.find((x) => x.gpuModelId === GPU_MODEL[g.id])
        if (!w) return
        const s = series[k]
        const h = gdh[k]
        applyIndexToGpu(g, w, s && s.ok ? s.data.points : undefined, undefined, h && h.ok ? h.data.points : undefined)
        // Prefer the backend's own windowed change when it publishes one.
        const ind = ov.ok ? ov.data.indicators.find((i) => i.id === w.id) : undefined
        if (g.live && ind && ind.changePct != null) {
          g.live.chgPct = ind.changePct
          g.live.chgWindow = `${ind.changeWindowDays ?? 7}-day change`
        }
        any = true
      })
      apply()
    })()

    const offersBranch = (async () => {
      const [regions, offers] = await Promise.all([get<MapRegionsResponse>('/flopx/map/regions'), get<OffersResponse>('/flopx/gpu/offers')])
      if (regions.ok) setHubNames(regions.data.regions.map((r) => r.region))
      if (offers.ok && applyOffers(offers.data) > 0) {
        any = true
        apply()
      }
    })()

    const pressureBranch = (async () => {
      const pr = await get<PressureResponse>('/flopx/pressure')
      if (pr.ok && applyPressure(pr.data) > 0) {
        any = true
        apply()
      }
    })()

    await Promise.all([indexBranch, offersBranch, pressureBranch])
    return any
  },
}

/**
 * The design shows one "inferred category pressure" per GPU. The backend publishes cpx-v1 scores
 * per region and GPU; the category figure is their median across regions (stated in the UI as
 * such). GPUs with no cells keep the design value, illustrative.
 */
export function applyPressure(resp: PressureResponse, gpus: Gpu[] = GPUS): number {
  let n = 0
  for (const g of gpus) {
    const model = GPU_MODEL[g.id]
    if (!model) continue
    const scores = resp.cells.filter((c) => c.gpuModelId === model && c.score != null && Number.isFinite(c.score)).map((c) => c.score as number)
    if (!scores.length) continue
    g.pressure = Math.round(median(scores))
    g.pressureLive = { cells: scores.length, method: resp.method, asOf: fmtObserved(resp.asOf) }
    n++
  }
  return n
}

/** Slots on the GPU's month axis that carry real data (for badges). */
export function realSlots(g: Gpu): { count: number; first: number } {
  return g.live ? realSpan(g.live.real) : { count: 0, first: -1 }
}

/** Region label for an offer: backend names for live offers, the design's for illustrative ones. */
export function offerRegionName(o: Pick<Offer, 'region' | 'live'>): string {
  return o.live ? liveRegionName(o.region) : regionById(o.region)?.name ?? o.region
}
export function offerRegionShort(o: Pick<Offer, 'region' | 'live'>): string {
  return o.live ? liveRegionShort(o.region) : regionById(o.region)?.short ?? o.region
}
