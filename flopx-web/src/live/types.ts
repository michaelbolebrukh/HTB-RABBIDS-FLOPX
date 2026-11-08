// Wire types for the FLOPX backend payloads this frontend reads. Only the fields the adapters use
// are typed; everything else on the wire is ignored. Source of truth: /openapi.json.

export type ProvenanceKind = 'live' | 'archived' | 'sample' | 'derived'

export interface Provenance {
  kind: ProvenanceKind
  source: string
  observedAt: string | null
  sourceUrl?: string | null
  fetchedAt?: string | null
  licence?: string | null
  note?: string | null
  inputKinds?: ProvenanceKind[]
}

export interface FreshnessBlock {
  generatedAt: string
  oldestObservedAt?: string | null
  sources?: string[]
  sourceDetails?: { source: string; kind: ProvenanceKind; observedAt: string | null; licence?: string | null }[]
  unavailable?: string[]
}

export interface SeriesPoint {
  seriesId: string
  t: string
  value: number
  unit: string
  provenance: Provenance
}

export interface GpuIndexWire {
  id: string
  gpuModelId: string
  methodology: string
  coverage: { providers: number; regions: number; offerCount: number }
  normalisation: string[]
  lastUpdated: string
  values: SeriesPoint[]
  limitations: string[]
  provenance: Provenance
  current: number | null
  basket: string[]
  breaks: unknown[]
}

export interface GpuIndexResponse {
  indices: GpuIndexWire[]
  freshness: FreshnessBlock
}

export interface GpuSeriesResponse {
  seriesId: string
  unit: string
  points: SeriesPoint[]
  freshness: FreshnessBlock
  breaks: unknown[]
}

export interface OfferWire {
  id: string
  providerId: string
  regionId: string
  gpuModelId: string
  gpuCount: number
  pricePerGpuHourUsd: number
  availability: 'available' | 'limited' | 'unavailable' | string
  interruptible: boolean
  contract: 'on-demand' | 'spot' | 'reserved' | string
  provenance: Provenance
  reliabilityScore: number | null
  bandwidthGbps: number | null
  minDurationHours: number | null
}

export interface OffersResponse {
  offers: OfferWire[]
  freshness: FreshnessBlock
}

export interface MapRegionWire {
  region: { id: string; name: string; countryCode?: string; lat?: number; lon?: number; gridZone?: string }
  offers?: number
  medianRawPrice?: number | null
  medianAdjustedPrice?: number | null
}

export interface MapRegionsResponse {
  regions: MapRegionWire[]
  freshness: FreshnessBlock
}

export interface OverviewIndicator {
  id: string
  labelKey?: string
  value: number | null
  unit: string
  changePct: number | null
  changeWindowDays?: number | null
  provenance: Provenance
}

export interface OverviewResponse {
  indicators: OverviewIndicator[]
  freshness: FreshnessBlock
  [k: string]: unknown
}

export interface PressureCell {
  regionId: string
  gpuModelId: string
  asOf: string
  score: number | null
  band: string
}

export interface PressureResponse {
  asOf: string
  method: string
  methodText?: string
  cells: PressureCell[]
  freshness: FreshnessBlock
}
