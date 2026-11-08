// Live module 'regions' (W1): regional compute pressure, effective supply, anomalies and the
// facility pipeline behind the X-Ray and the Global Intelligence briefing.
//
// Reads: /flopx/pressure (cpx-v1 cells), /flopx/map/regions (hub names, coordinates, energy
// indicator), /flopx/supply (effective-supply-v1), /flopx/anomalies, /flopx/exposure?region= (one
// per covered hub). Writes, in place: REGIONS[*] for the eight design regions the backend covers
// (BACKEND_REGION_IDS in data/core.ts), GLOBAL_CPI[NOW_IDX], ANOMALY_CARDS, PRESSURE_MODEL.
//
// Rules (src/live/README.md): every mapping below is a pure function of a backend payload; values
// the backend does not supply stay design values and read as illustrative (a region without
// `live` is wholly illustrative); backend sample data maps to 'illustrative' via freshOf(); month
// series go through placeOnAxis() then joinToNow() (G14): only the current slot is real today, and
// the design's other months are shifted so the curve joins it.
//
// Not wired: /flopx/scenario/infra (POST) refuses every cell today with "scenario needs at least
// three history points", so the What-if tab keeps the design's illustrative model.
import { ANOMALY_CARDS, BACKEND_REGION_IDS, GLOBAL_CPI, NOW_IDX, PRESSURE_MODEL, REGIONS, type Anomaly, type AnomalyCard, type Evidence, type Freshness, type Region, type RegionLive } from '@/data/core'
import { secByTicker } from '@/data/finance'
import type { Getter, LiveModule } from './boot'
import { joinToNow, placeOnAxis, type JoinedSeries } from './dates'
import { MODEL_GPU } from './gpu'
import { setPressureBands } from '@/lib/model'
import { fmtDay, freshOf } from './provenance'
import type { FreshnessBlock, Provenance, ProvenanceKind } from './types'

// ---------- wire types (only the fields read here; /openapi.json is the source of truth) ----------

export interface FactorWire {
  factor: string
  raw: number | null
  unit: string | null
  normalised: number | null
  weight: number
  contribution: number | null
  direction: 'up' | 'down' | 'none' | string
  evidence: Provenance[]
  observedAt: string | null
  note: string | null
}
export interface UncertaintyWire {
  coverage: number
  factorsWithData: number
  factorsTotal: number
  sampleShare: number
  oldestInput?: string | null
  label: string
}
export interface HistoryPointWire {
  t: string
  score: number | null
  band: string
  inputKinds?: ProvenanceKind[]
}
export interface PressureCellWire {
  regionId: string
  gpuModelId: string
  asOf: string
  score: number | null
  band: string
  factors: FactorWire[]
  contradictions: FactorWire[]
  uncertainty: UncertaintyWire
  method: string
  inputKinds?: ProvenanceKind[]
  provenance: Provenance
  history: HistoryPointWire[]
  historyBasis?: string
  /** N-XRAY: the cell's rental economics; absent on a backend older than the field. */
  rental?: RentalWire | null
}
export interface RentalWire {
  basis: 'regional-asks' | 'global-fallback' | 'none'
  medianUsdPerGpuHour: number | null
  premiumPct: number | null
  premiumLabel: string
  askCount: number
  globalMedianUsdPerGpuHour: number | null
  globalAskCount: number
  weeklyMedianUsdPerGpuHour: number | null
  weeklyWeekEnding: string | null
  source: string
  note: string
  evidence: Provenance[]
  observedAt: string | null
}
export interface PressureW1Response {
  asOf: string
  method: string
  methodText?: string
  bands?: Record<string, string>
  cells: PressureCellWire[]
  freshness: FreshnessBlock
}
export interface EnergyIndicatorWire {
  seriesId: string
  t: string
  value: number
  unit: string
  provenance: Provenance
}
export interface MapRegionW1 {
  region: { id: string; name: string; countryCode?: string; lat?: number; lon?: number; gridZone?: string }
  offers?: number
  medianRawPrice?: number | null
  medianAdjustedPrice?: number | null
  facilitiesByStatus?: Record<string, number>
  energyIndicator?: EnergyIndicatorWire | null
}
export interface MapRegionsW1Response {
  regions: MapRegionW1[]
  freshness: FreshnessBlock
}
export interface SupplyFacilityWire {
  facilityId: string
  name: string
  status: string
  capacityMw: number | null
  expectedOnline: string | null
  monthsLate: number | null
  completionProbability: number | null
  contributesMw: number
}
export interface SupplyRegionWire {
  regionId: string
  asOf: string
  operatingMw: number
  underConstructionMw: number
  announcedMw: number
  reportedMw: number
  effectiveMw: number
  facilities: SupplyFacilityWire[]
  assumptions: { name: string; value: number; range: [number, number]; note?: string }[]
  method: string
  inputKinds?: ProvenanceKind[]
  provenance: Provenance
}
export interface SupplyResponse {
  regions: SupplyRegionWire[]
  freshness: FreshnessBlock
}
export interface ExposurePositionWire {
  quantity: number
  side: 'long' | 'short'
  avgCostUsd: number
  marketValueUsd: number | null
  weight: number | null
}
export interface LinkedCompanyWire {
  ticker: string
  basis: string[]
  documented: boolean
  sensitivity: number
  sensitivityBasis: string
  facilityIds: string[]
  provenance: Provenance
  /** N-XRAY: the tie in words (optional on the wire so older fixtures still type-check). */
  role?: string
  tieKind?: 'documented' | 'authored'
  tieBasis?: string
  tieNote?: string | null
  position?: ExposurePositionWire | null
}
export interface AnomalyWire {
  id: string
  type: string
  regionId: string
  gpuModelId: string | null
  severity: number
  headline: string
  rule: string
  measures: Record<string, number | string>
  supporting: FactorWire[]
  contradicting: FactorWire[]
  uncertainty: UncertaintyWire
  firstSeen: string
  status: string
  history: HistoryPointWire[]
  linkedCompanies: LinkedCompanyWire[]
  provenance: Provenance
}
export interface AnomaliesResponse {
  anomalies: AnomalyWire[]
  freshness: FreshnessBlock
}
export interface ExposureResponse {
  regionId: string
  gpuModelId: string | null
  exposures: LinkedCompanyWire[]
}

// ---------- vocabulary ----------

/** The design's region pressure is one number; the backend scores (region, GPU) cells. W1 uses the H100 SXM cell. */
export const HEADLINE_GPU = 'h100-sxm-80gb'

const GPU_NAMES: Record<string, string> = {
  'h100-sxm-80gb': 'H100 SXM',
  'h200-sxm-141gb': 'H200',
  'b200-sxm-192gb': 'B200',
  'a100-sxm-80gb': 'A100 80GB',
  'l40s-48gb': 'L40S',
  'rtx-4090-24gb': 'RTX 4090',
}
export const gpuLabel = (id: string | null | undefined) => (id ? (GPU_NAMES[id] ?? id) : 'all GPUs')

const FACTOR_LABELS: Record<string, string> = {
  'price-premium': 'Comparable-ask premium vs global',
  availability: 'Advertised availability',
  'price-momentum': '30-day index momentum',
  'power-stress': 'Power-price stress',
  'capacity-pipeline': 'Facility pipeline relief',
  'demand-signals': 'Demand signals (markets, feed)',
}
export const factorLabel = (f: string) => FACTOR_LABELS[f] ?? f

const ANOMALY_TYPES: Record<string, string> = {
  bottleneck: 'Bottleneck',
  'announcement-mismatch': 'Announcement mismatch',
  'price-dispersion': 'Price dispersion',
  overhang: 'Overhang',
  divergence: 'Divergence',
}
export const anomalyTypeLabel = (t: string) => ANOMALY_TYPES[t] ?? t

/** "US East and Midwest (Ashburn hub)" -> "US East and Midwest" for sentences. */
export const plainRegionName = (name: string) => name.replace(/\s*\([^)]*\)\s*$/, '')

/** Freshness of a set of provenance records: any sample -> illustrative, else the weakest of the rest. */
export function freshOfEvidence(ev: Provenance[]): Freshness {
  if (!ev.length) return 'unavailable'
  const tags = ev.map((p) => freshOf(p).freshness)
  if (tags.includes('illustrative')) return 'illustrative'
  if (tags.includes('historical')) return 'historical'
  return 'live'
}

// ---------- pure adapters ----------

export interface PressureLive {
  designId: string
  backendId: string
  score: number
  band: string
  uncertainty: UncertaintyWire
  freshness: Freshness
  factors: NonNullable<RegionLive['pressure']>['factors']
  contradictions: string[]
  /** Factor shares of |contribution| (the design's "Top drivers of the inference"). */
  contrib: { factor: string; value: number }[]
  rental: RegionLive['rental']
  history: { t: string; value: number }[]
  prevScore: number | null
  asOf: string
}

/** One cpx-v1 cell -> the design region's pressure fields. Returns null when the cell has no score. */
export function adaptPressureCell(cell: PressureCellWire): PressureLive | null {
  if (cell.score == null || !Number.isFinite(cell.score)) return null
  const factors = cell.factors.map((f) => ({ factor: f.factor, note: f.note ?? '', contribution: f.contribution, weight: f.weight, freshness: freshOfEvidence(f.evidence) }))
  const withC = cell.factors.filter((f) => f.contribution != null && Number.isFinite(f.contribution))
  const total = withC.reduce((s, f) => s + Math.abs(f.contribution!), 0)
  const contrib = total > 0 ? withC.map((f) => ({ factor: factorLabel(f.factor), value: +(Math.abs(f.contribution!) / total).toFixed(3) })).sort((a, b) => b.value - a.value) : []
  const pp = cell.factors.find((f) => f.factor === 'price-premium')
  let rental: RegionLive['rental'] = null
  const sourceLabel = (src: string) => (src === 'vast-ai' ? 'Vast.ai asks (market reading, not an index)' : src)
  const rw = cell.rental
  if (rw && rw.basis === 'regional-asks') {
    // N-XRAY: the backend states the cell's median and premium; the same numbers as the price-premium factor.
    rental = { median: rw.medianUsdPerGpuHour, premiumPct: rw.premiumPct, premiumLabel: rw.premiumLabel, fallback: false, freshness: freshOfEvidence(rw.evidence), source: sourceLabel(rw.source), observedAt: rw.observedAt }
  } else if (rw && rw.basis === 'global-fallback') {
    // No regional asks: the model's global median (and GetDeploying's archived week) stand in, labelled; no premium.
    rental = {
      median: rw.medianUsdPerGpuHour, premiumPct: null, premiumLabel: rw.premiumLabel, fallback: true, fallbackNote: rw.note,
      weekly: rw.weeklyMedianUsdPerGpuHour != null && rw.weeklyWeekEnding ? { median: rw.weeklyMedianUsdPerGpuHour, weekEnding: rw.weeklyWeekEnding } : undefined,
      freshness: freshOfEvidence(rw.evidence), source: sourceLabel(rw.source), observedAt: rw.observedAt,
    }
  } else if (!rw && pp && pp.raw != null && Number.isFinite(pp.raw)) {
    // A backend older than `rental`: the cell's comparable-ask median is stated in the factor note ("cell median comparable ask 2.4698 vs ...").
    const m = pp.note?.match(/cell median comparable ask ([0-9.]+)/)
    const src = pp.evidence[0]?.source ?? 'FLOPX offers'
    const premiumPct = +(pp.raw * 100).toFixed(1)
    rental = { median: m ? +m[1] : null, premiumPct, premiumLabel: `${premiumPct >= 0 ? '+' : ''}${premiumPct.toFixed(1)}%`, fallback: false, freshness: freshOfEvidence(pp.evidence), source: sourceLabel(src), observedAt: pp.observedAt }
  }
  const hist = [...cell.history].filter((h) => h.score != null).sort((a, b) => Date.parse(a.t) - Date.parse(b.t))
  const history = hist.map((h) => ({ t: h.t, value: h.score as number }))
  if (!history.some((h) => h.t === cell.asOf)) history.push({ t: cell.asOf, value: cell.score })
  const prior = hist.filter((h) => Date.parse(h.t) < Date.parse(cell.asOf) - 60_000)
  return {
    designId: BACKEND_REGION_IDS[cell.regionId] ?? cell.regionId,
    backendId: cell.regionId,
    score: cell.score,
    band: cell.band,
    uncertainty: cell.uncertainty,
    freshness: freshOf(cell.provenance).freshness,
    factors,
    contradictions: cell.contradictions.map((c) => c.factor),
    contrib,
    rental,
    history,
    prevScore: prior.length ? (prior[prior.length - 1].score as number) : null,
    asOf: cell.asOf,
  }
}

/** /flopx/pressure -> headline-GPU pressure per design region (only regions in BACKEND_REGION_IDS). */
export function adaptPressure(resp: PressureW1Response, gpu = HEADLINE_GPU): Map<string, PressureLive> {
  // The backend's band vocabulary (slack/balanced/tight/acute and its thresholds) replaces the
  // design's words, so every surface speaks one set.
  if (resp.bands) setPressureBands(resp.bands)
  const out = new Map<string, PressureLive>()
  for (const c of resp.cells) {
    if (c.gpuModelId !== gpu || !(c.regionId in BACKEND_REGION_IDS)) continue
    const p = adaptPressureCell(c)
    if (p) out.set(p.designId, p)
  }
  return out
}

export interface MapRegionLive {
  designId: string
  backendId: string
  name: string
  lat: number | null
  lon: number | null
  gridZone: string | null
  power: RegionLive['power']
}

/** /flopx/map/regions -> backend names, hub coordinates and the energy indicator per design region. */
export function adaptMapRegions(resp: MapRegionsW1Response): Map<string, MapRegionLive> {
  const out = new Map<string, MapRegionLive>()
  for (const r of resp.regions) {
    const designId = BACKEND_REGION_IDS[r.region.id]
    if (!designId) continue
    const e = r.energyIndicator
    out.set(designId, {
      designId,
      backendId: r.region.id,
      name: r.region.name,
      lat: typeof r.region.lat === 'number' ? r.region.lat : null,
      lon: typeof r.region.lon === 'number' ? r.region.lon : null,
      gridZone: r.region.gridZone ?? null,
      power: e && Number.isFinite(e.value) ? { value: e.value, unit: e.unit, freshness: freshOf(e.provenance).freshness, source: e.provenance.source, observedAt: e.provenance.observedAt } : null,
    })
  }
  return out
}

export interface SupplyLive {
  designId: string
  opMW: number
  conMW: number
  propMW: number
  /** Pipeline: under construction + announced or planned (what the backend's pipeline factor counts). */
  pipelineMW: number
  /** Effective additions from the pipeline, low to high, from the backend's stated completion-ceiling range. */
  effAdd: [number, number]
  effectiveMw: number
  facilities: number
  method: string
  freshness: Freshness
}

/**
 * /flopx/supply -> design capacity fields. The backend's effectiveMw counts operating capacity at
 * the utilisation assumption plus each under-construction facility at its completion probability;
 * the design's "usable by" figure is additions only, so effAdd sums the under-construction
 * contributions and widens them by the backend's own completion-ceiling range (never past a
 * facility's stated capacity). Facility records today are sample data, so freshness is illustrative.
 */
export function adaptSupply(resp: SupplyResponse): Map<string, SupplyLive> {
  const out = new Map<string, SupplyLive>()
  for (const r of resp.regions) {
    const designId = BACKEND_REGION_IDS[r.regionId]
    if (!designId) continue
    const ceil = r.assumptions.find((a) => a.name === 'completion-ceiling')
    const lo = ceil && ceil.value > 0 ? ceil.range[0] / ceil.value : 1
    const hi = ceil && ceil.value > 0 ? ceil.range[1] / ceil.value : 1
    let a = 0
    let b = 0
    for (const f of r.facilities) {
      if (f.status !== 'under-construction' || !(f.contributesMw > 0)) continue
      a += f.contributesMw * lo
      b += Math.min(f.capacityMw ?? Infinity, f.contributesMw * hi)
    }
    out.set(designId, {
      designId,
      opMW: Math.round(r.operatingMw),
      conMW: Math.round(r.underConstructionMw),
      propMW: Math.round(r.announcedMw),
      pipelineMW: Math.round(r.underConstructionMw + r.announcedMw),
      effAdd: [Math.round(a), Math.round(b)],
      effectiveMw: Math.round(r.effectiveMw),
      facilities: r.facilities.length,
      method: r.method,
      freshness: freshOf(r.provenance).freshness,
    })
  }
  return out
}

const supportOf = (f: FactorWire): 'strong' | 'moderate' | 'weak' => {
  const c = Math.abs(f.contribution ?? 0)
  return c >= 0.06 ? 'strong' : c >= 0.02 ? 'moderate' : 'weak'
}

/**
 * The design's Inspector can only open securities it has a record for; linked companies outside
 * that list (today CORZ, SMCI, VRT) are left out of clickable lists rather than crash the panel.
 */
export const knownTicker = (t: string) => !!secByTicker(t)

/** One backend anomaly -> the design's Anomaly (the X-Ray brief tab's four questions). */
export function adaptAnomaly(a: AnomalyWire, regionName: string, known: (t: string) => boolean = knownTicker): Anomaly {
  const u = a.uncertainty
  const score = u.factorsTotal > 0 ? +(u.coverage / u.factorsTotal).toFixed(2) : 0
  const designId = BACKEND_REGION_IDS[a.regionId] ?? a.regionId
  const companies = [...a.linkedCompanies].filter((c) => known(c.ticker)).sort((x, y) => y.sensitivity - x.sensitivity).slice(0, 3)
  const designGpu = a.gpuModelId ? MODEL_GPU[a.gpuModelId] : undefined
  const last = a.history.length ? a.history[a.history.length - 1] : null
  return {
    title: `${anomalyTypeLabel(a.type)}: ${gpuLabel(a.gpuModelId)}`,
    detected: fmtDay(a.firstSeen),
    changed: a.headline.replace(a.gpuModelId ?? '\u0000', gpuLabel(a.gpuModelId)),
    changedPoints: a.supporting.map((f) => `${factorLabel(f.factor)}: ${f.note ?? 'no note'}`),
    why: [
      { text: `Detection rule: ${a.rule}.`, support: a.severity >= 2 ? 'strong' : 'moderate' },
      ...a.supporting.filter((f) => f.contribution != null).map((f) => ({ text: `${factorLabel(f.factor)} points ${f.direction} (contribution ${f.contribution! >= 0 ? '+' : ''}${f.contribution!.toFixed(3)}).`, support: supportOf(f) })),
    ],
    confidence: {
      level: u.label === 'indicative' ? 'Moderate' : 'Low',
      score,
      notes: [
        `Score is the share of the ${u.factorsTotal} cpx-v1 factors backed by live or archived inputs (${u.coverage} of ${u.factorsTotal}).`,
        `${u.factorsWithData} of ${u.factorsTotal} factors have any data; ${Math.round(u.sampleShare * 100)}% of inputs are FLOPX sample data.`,
        `Uncertainty label: ${u.label}. Utilisation is not measured.`,
      ],
    },
    affects: [
      ...(designGpu ? [{ kind: 'gpu' as const, id: designGpu, label: `${gpuLabel(a.gpuModelId)} rental`, note: last ? `Cell scored ${last.score} (${last.band})` : 'Cell in the anomaly', link: 'modelled' as const }] : []),
      { kind: 'region' as const, id: designId, label: regionName, note: `Anomaly rule: ${anomalyTypeLabel(a.type).toLowerCase()}`, link: 'modelled' as const },
      ...companies.map((c) => ({ kind: 'security' as const, id: c.ticker, label: c.ticker, note: `Sensitivity ${c.sensitivity} (${c.documented ? 'documented' : 'authored estimate, not a measured beta'})`, link: c.documented ? ('documented' as const) : ('modelled' as const) })),
    ],
    conflicting: a.contradicting.length ? a.contradicting.map((f) => `${factorLabel(f.factor)}: ${f.note ?? 'no note'}`).join(' ') : undefined,
  }
}

export interface AnomaliesLive {
  /** The most severe open anomaly per design region. */
  byRegion: Map<string, Anomaly>
  cards: AnomalyCard[]
}

/** /flopx/anomalies -> per-region Anomaly plus the global panel's cards (open only, most severe first). */
export function adaptAnomalies(resp: AnomaliesResponse, nameOf: (designId: string) => string): AnomaliesLive {
  const open = resp.anomalies.filter((a) => a.status === 'open' && a.regionId in BACKEND_REGION_IDS)
  // Most severe first; among equals, the one backed by more real inputs; then the newest.
  open.sort((x, y) => y.severity - x.severity || y.uncertainty.coverage - x.uncertainty.coverage || Date.parse(y.firstSeen) - Date.parse(x.firstSeen))
  const byRegion = new Map<string, Anomaly>()
  const cards: AnomalyCard[] = []
  for (const a of open) {
    const designId = BACKEND_REGION_IDS[a.regionId]
    const name = nameOf(designId)
    if (!byRegion.has(designId)) byRegion.set(designId, adaptAnomaly(a, name))
    const u = a.uncertainty
    cards.push({
      id: a.id,
      region: designId,
      title: `${plainRegionName(name)}: ${anomalyTypeLabel(a.type).toLowerCase()}, ${gpuLabel(a.gpuModelId)}`,
      sub: `First seen ${fmtDay(a.firstSeen)} · coverage ${u.factorsWithData} of ${u.factorsTotal} factors · ${u.label}`,
      severity: a.severity,
    })
  }
  return { byRegion, cards }
}

/** /flopx/exposure?region= -> the exposure tab's documented ties and estimated sensitivities. */
export function adaptExposure(resp: ExposureResponse, known: (t: string) => boolean = knownTicker): { documented: Region['documented']; authored: NonNullable<Region['authored']>; positions: Map<string, ExposurePositionWire>; sensitivities: Region['sensitivities']; freshness: Freshness } {
  const tags = resp.exposures.map((e) => freshOf(e.provenance).freshness)
  const all = resp.exposures
  resp = { ...resp, exposures: resp.exposures.filter((e) => known(e.ticker)) }
  return {
    documented: all.filter((e) => e.documented).map((e) => ({ t: e.ticker, tie: e.tieBasis || e.basis.join(', '), source: e.provenance.source })),
    // N-XRAY: ties the backend rests on a profile or an authored sample facility, listed whether or not the price feed knows the ticker.
    authored: all.filter((e) => !e.documented).map((e) => ({ t: e.ticker, tie: e.tieBasis || e.basis.join(', '), note: e.tieNote ?? 'authored tie from company profile, not a filing', role: e.role ?? '', source: e.provenance.source })),
    positions: new Map(all.filter((e) => e.position).map((e) => [e.ticker, e.position as ExposurePositionWire])),
    // The backend's sensitivity is an authored estimate per profile role, not a regression: no R² exists.
    sensitivities: [...resp.exposures].sort((a, b) => b.sensitivity - a.sensitivity).map((e) => ({ t: e.ticker, beta: e.sensitivity, r2: NaN })),
    freshness: tags.includes('illustrative') ? 'illustrative' : tags.includes('historical') ? 'historical' : tags.length ? 'live' : 'unavailable',
  }
}

/** Factor rows for the evidence tab (factors with data that the base rows do not already show). */
export function factorEvidence(designId: string, p: PressureLive): Evidence[] {
  return p.factors
    .filter((f) => f.contribution != null && f.factor !== 'price-premium')
    .map((f) => ({
      id: `${designId}-f-${f.factor}`,
      label: `cpx-v1 factor: ${factorLabel(f.factor)}`,
      value: `${f.contribution! >= 0 ? '+' : ''}${f.contribution!.toFixed(3)}`,
      truth: 'inf' as const,
      source: 'FLOPX cpx-v1',
      time: fmtDay(p.asOf),
      freshness: f.freshness,
      weight: f.weight,
      direction: f.contribution! > 0.001 ? ('up' as const) : f.contribution! < -0.001 ? ('down' as const) : ('flat' as const),
      conflict: p.contradictions.includes(f.factor) || undefined,
      note: f.note,
    }))
}

// ---------- composition and apply ----------

export interface W1Parts {
  pressure?: Map<string, PressureLive>
  map?: Map<string, MapRegionLive>
  supply?: Map<string, SupplyLive>
  anomalies?: AnomaliesLive
  exposure?: Map<string, ReturnType<typeof adaptExposure>>
  method?: string
  asOf?: string
}

/** The design value of every region field W1 may overwrite, captured once so re-applies start clean. */
const designRegions = new Map<string, Region>()
const designGlobal: number[] = []
function designOf(r: Region): Region {
  if (!designRegions.has(r.id)) designRegions.set(r.id, { ...r, pressure: [...r.pressure], eff27: [...r.eff27] as [number, number], documented: [...r.documented], sensitivities: [...r.sensitivities], contrib: [...r.contrib] })
  return designRegions.get(r.id)!
}

/**
 * The region the design should show, given what has landed. Pure: returns a new object built from
 * the design region `d` and the backend parts; `undefined` parts leave the design fields.
 */
export function composeRegion(d: Region, parts: W1Parts, now?: Date): Region {
  const id = d.id
  const p = parts.pressure?.get(id)
  const m = parts.map?.get(id)
  const s = parts.supply?.get(id)
  const x = parts.exposure?.get(id)
  if (!p && !m && !s) {
    // Once the backend's pressure grid has landed, a design region it does not cover is a region
    // FLOPX has no data for: it takes the design's own no-data state instead of showing synthetic
    // numbers next to real ones (on the map they would otherwise lead their continent unbadged).
    if (parts.pressure && !(Object.values(BACKEND_REGION_IDS) as string[]).includes(id)) return noDataRegion(d)
    return { ...d, live: undefined }
  }
  const backendId = p?.backendId ?? m?.backendId ?? Object.keys(BACKEND_REGION_IDS).find((k) => BACKEND_REGION_IDS[k] === id)!
  const next: Region = { ...d, pressure: [...d.pressure], eff27: [...d.eff27] as [number, number] }
  if (m) {
    next.name = m.name
    if (m.lat != null) next.lat = m.lat
    if (m.lon != null) next.lon = m.lon
  }
  // The design's dollar power field (the map's power layer, "$ bars") holds only a backend USD/MWh
  // indicator. Once the backend's region list has landed, a covered region without a USD series
  // takes the design's no-data state (0, no bar) rather than the design's unbadged price; a
  // non-USD series (DE-LU, EUR) stays in `live.power` with its unit for the evidence tab.
  if (parts.map) next.power = m?.power && /^USD\/MWh$/i.test(m.power.unit) ? +m.power.value.toFixed(1) : 0
  let realSlots: boolean[] = d.pressure.map(() => false)
  let scaled = false
  if (p) {
    // G14: real months replace the design's in their slots, and the illustrative months are
    // shifted (0..100 score) so the curve joins the real present; see src/live/README.md.
    const placed = joinToNow(placeOnAxis(d.pressure, p.history, now), d.pressure, 'shift', { clamp: [0, 100] })
    next.pressure = placed.values
    realSlots = placed.real
    scaled = placed.scaled
    next.quality = p.uncertainty.label === 'indicative' ? 'good' : 'experimental'
    next.contrib = p.contrib
    next.h100 = p.rental?.median ?? 0
    // A fallback median has no regional premium: `premium` stays 0 for the map and the inspector, and the
    // Brief tab reads `live.rental.premiumLabel` ("n/a (no regional asks)") instead.
    next.premium = p.rental?.premiumPct != null ? Math.round(p.rental.premiumPct) : 0
    next.h100Chg = NaN // the backend has no 30-day H100 change per region yet (price-momentum: fewer than 30 days of history)
    next.queueGW = 0 // not in FLOPX
    next.extraEvidence = factorEvidence(id, p)
    next.note = `cpx-v1 reads ${p.band} (${p.uncertainty.label}): ${p.uncertainty.factorsWithData} of ${p.uncertainty.factorsTotal} factors have data and ${Math.round(p.uncertainty.sampleShare * 100)}% of inputs are FLOPX sample data.`
  }
  if (s) {
    next.opMW = s.opMW
    next.conMW = s.conMW
    next.propMW = s.propMW
    next.announced27 = s.pipelineMW
    next.eff27 = s.effAdd
  }
  // A covered region shows only the backend's anomaly (none until /flopx/anomalies lands), never the design's.
  if (p || parts.anomalies) next.anomaly = parts.anomalies?.byRegion.get(id)
  if (x) {
    next.documented = x.documented
    next.authored = x.authored
    next.sensitivities = x.sensitivities
  }
  next.live = {
    backendId,
    asOf: p?.asOf ?? parts.asOf ?? new Date().toISOString(),
    pressure: p ? { gpu: HEADLINE_GPU, score: p.score, band: p.band, label: p.uncertainty.label, coverage: p.uncertainty.coverage, factorsWithData: p.uncertainty.factorsWithData, factorsTotal: p.uncertainty.factorsTotal, sampleShare: p.uncertainty.sampleShare, freshness: p.freshness, realSlots, scaled, factors: p.factors } : null,
    rental: p?.rental ?? null,
    power: m?.power ?? null,
    supply: s ? { freshness: s.freshness, facilities: s.facilities, effectiveMw: s.effectiveMw, method: s.method } : null,
    exposure: x ? { freshness: x.freshness } : null,
  }
  return next
}

/** The design's no-data state for a region outside the backend's hubs: no estimate, no numbers. */
export function noDataRegion(d: Region): Region {
  return {
    ...d, quality: 'none', pressure: d.pressure.map(() => 0), unc: 0, h100: 0, h100Chg: 0, premium: 0, power: 0, queueGW: 0,
    opMW: 0, conMW: 0, propMW: 0, announced27: 0, eff27: [0, 0], slipSensitivity: 0, documented: [], sensitivities: [], contrib: [],
    anomaly: undefined, extraEvidence: undefined, note: 'Outside the eight FLOPX hubs: no rental, power or facility data, so no estimate.', live: undefined,
  }
}

/** Equal-weight mean of the covered regions' headline scores (the design's global CPI, made from real cells). */
export function globalFromPressure(pressure: Map<string, PressureLive>): { now: number; prev: number | null; n: number } | null {
  const ps = [...pressure.values()]
  if (!ps.length) return null
  const now = +(ps.reduce((s, p) => s + p.score, 0) / ps.length).toFixed(1)
  const prevs = ps.map((p) => p.prevScore)
  const prev = prevs.every((v) => v != null) ? +((prevs as number[]).reduce((s, v) => s + v, 0) / prevs.length).toFixed(1) : null
  return { now, prev, n: ps.length }
}

/** The design's global CPI with the real present in slot NOW_IDX and every other slot shifted to join it (G14). */
export function joinGlobal(design: number[], now: number): JoinedSeries {
  const real = design.map((_, i) => i === NOW_IDX)
  const values = design.map((v, i) => (i === NOW_IDX ? now : v))
  return joinToNow({ values, real, used: 1 }, design, 'shift', { clamp: [0, 100], digits: 1 })
}

/** Write the composed regions and globals into the design exports, in place. */
export function applyW1(parts: W1Parts, now?: Date): void {
  if (!designGlobal.length) designGlobal.push(...GLOBAL_CPI)
  for (let i = 0; i < REGIONS.length; i++) {
    const d = designOf(REGIONS[i])
    Object.assign(REGIONS[i], composeRegion(d, parts, now))
  }
  if (parts.pressure) {
    // The backend's band vocabulary (slack/balanced/tight/acute with its thresholds) replaces the
    // design's words, so every surface uses one set.
    const g = globalFromPressure(parts.pressure)
    designGlobal.forEach((v, i) => (GLOBAL_CPI[i] = v))
    if (g) {
      // G14: the one real month replaces the design's Now slot, and the design's history and
      // forecast are shifted so they join it (the uncertainty band keeps its width under a shift).
      const joined = joinGlobal(designGlobal, g.now)
      joined.values.forEach((v, i) => (GLOBAL_CPI[i] = v))
      PRESSURE_MODEL.regions = g.n
      PRESSURE_MODEL.globalReal = true
      PRESSURE_MODEL.globalScaled = joined.scaled
      PRESSURE_MODEL.globalPrev = g.prev
    }
  }
  if (parts.method) PRESSURE_MODEL.method = parts.method
  if (parts.asOf) PRESSURE_MODEL.asOf = parts.asOf
  if (parts.anomalies) {
    ANOMALY_CARDS.length = 0
    ANOMALY_CARDS.push(...parts.anomalies.cards)
    PRESSURE_MODEL.anomaliesLive = true
  }
}

// ---------- the module ----------

const nameFrom = (parts: W1Parts) => (designId: string) => parts.map?.get(designId)?.name ?? REGIONS.find((r) => r.id === designId)?.name ?? designId

export const regionsModule: LiveModule = {
  id: 'regions',
  async load(get: Getter, apply) {
    const parts: W1Parts = {}
    let landed = 0
    const commit = () => {
      applyW1(parts)
      apply()
    }
    const pressure = (async () => {
      const r = await get<PressureW1Response>('/flopx/pressure')
      if (!r.ok) return
      parts.pressure = adaptPressure(r.data)
      parts.method = r.data.method
      parts.asOf = r.data.asOf
      landed++
      commit()
    })()
    const map = (async () => {
      const r = await get<MapRegionsW1Response>('/flopx/map/regions')
      if (!r.ok) return
      parts.map = adaptMapRegions(r.data)
      landed++
      commit()
    })()
    const supply = (async () => {
      const r = await get<SupplyResponse>('/flopx/supply')
      if (!r.ok) return
      parts.supply = adaptSupply(r.data)
      landed++
      commit()
    })()
    const anomalies = (async () => {
      const r = await get<AnomaliesResponse>('/flopx/anomalies', { timeoutMs: 25_000 })
      if (!r.ok) return
      await map // names first, so cards read the backend's region names
      parts.anomalies = adaptAnomalies(r.data, nameFrom(parts))
      landed++
      commit()
    })()
    await Promise.all([pressure, map, supply, anomalies])
    // Exposure is per hub; fetched one at a time after the main reads, because a burst of parallel
    // reads has been seen to tip the Render Starter instance into 502s.
    if (landed) {
      const exp = new Map<string, ReturnType<typeof adaptExposure>>()
      for (const [backendId, designId] of Object.entries(BACKEND_REGION_IDS)) {
        const r = await get<ExposureResponse>(`/flopx/exposure?region=${encodeURIComponent(backendId)}`)
        if (r.ok) exp.set(designId, adaptExposure(r.data))
        else break
      }
      if (exp.size) {
        parts.exposure = exp
        commit()
      }
    }
    return landed > 0
  },
}
