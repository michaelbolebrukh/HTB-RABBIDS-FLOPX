// W4 adapter: Ask FLOPX and the Agent Desk answer from the backend.
//
// POST /flopx/ask is a deterministic grammar: it returns an intent, the backend screen route with
// filters, an explanation of which words matched what, and, when the question names one target
// (a region, a ticker or a GPU model) in an assessment shape, the composed brief: templated
// sentences, each with a statement class and the ids of the evidence it rests on, every evidence
// row carrying its own provenance envelope. `composed-from-evidence-v1`, not a language model.
//
// Everything here is pure: payload in, the design's Answer shape out (facts, interpretation,
// confidence, destinations). Nothing invents a number; a fact row is a backend sentence and the
// evidence it cites, badged by that evidence's provenance through the one freshness mapping
// (provenance.ts). A sentence whose evidence is authored sample data never carries the OBS glyph.
// The network call (`askLive`) is the only impure export and resolves to a LiveResult like every
// other client call, so a slow or failed read leaves the design's offline answer in place.
import { ENERGY } from '@/data/infra'
import { designRegionId, type Freshness, type Truth } from '@/data/core'
import type { PageId } from '@/lib/store'
import { fetchJson, type FetchOpts, type LiveResult } from './client'
import { MODEL_GPU } from './gpu'
import { fmtAsOf, freshOf } from './provenance'
import { liveRegionName, liveRegionShort } from './regions'
import type { FreshnessBlock, Provenance, ProvenanceKind } from './types'

// ---------- wire types (only the fields read here; source of truth /openapi.json) ----------

export type StatementClass = 'observation' | 'inference' | 'forecast' | 'decision'

export interface AskActionWire {
  kind: string
  route: string
  requiresConfirmation: boolean
}

export interface SentenceWire {
  text: string
  statementClass: StatementClass | string
  evidenceIds: string[]
}

export interface EvidenceRefWire {
  id: string
  kind: string
  href: string
  statementClass: StatementClass | string
  observedAt?: string | null
  provenanceKind?: string | null
  provenance?: Provenance | null
  inputKinds?: string[]
}

export interface BriefWire {
  targetKind: 'region' | 'ticker' | 'gpu' | string
  targetId: string
  headline: string
  sentences: SentenceWire[]
  evidence: EvidenceRefWire[]
  followThrough: { kind: string; route: string; label: string }[]
  caveats: string[]
  method: string
  statementClass: string
  notLanguageModel: boolean
  engine: string
  languageModelNote: string
  freshness?: FreshnessBlock | null
}

export interface AskResponse {
  query: string
  intent: string
  route: string
  filters: Record<string, unknown>
  explanation: { matched?: Record<string, string[]>; unmatchedTokens?: string[]; numbers?: number[]; periodDays?: number | null }
  actions: AskActionWire[]
  engine: 'grammar' | 'llm' | string
  brief?: BriefWire | null
}

// ---------- the design-shaped result ----------

export interface LiveFact {
  text: string
  source: string
  time: string
  /** OBS/INF/FCT/DEC by the backend's statement class; null when a sample-backed observation (never OBS). */
  truth: Truth | null
  freshness: Freshness
  freshLabel?: string
  /** Backend screen the evidence opens on (informational; destinations below are the design's). */
  href?: string
  evidenceId?: string
}

/** A design screen to open: the hash route's page, its params, and an X-Ray state patch when it is the X-Ray. */
export interface Destination {
  label: string
  page: PageId
  params?: Record<string, string>
  xray?: { region?: string; gpu?: string; tab?: 'brief' | 'evidence' | 'pipeline' | 'exposure' | 'whatif' }
}

export type Confidence = 'High' | 'Moderate' | 'Low' | 'Insufficient'

export interface LiveAnswer {
  kind: 'brief' | 'route' | 'none'
  intent: string
  facts: LiveFact[]
  interpretation: string
  confidence: Confidence
  destinations: Destination[]
  /** One line naming the engine that answered, for the card header. */
  engine: string
  /** The design's engine-tool id when the backend intent maps onto one exactly; undefined otherwise. */
  tool?: string
  headline?: string
  /** `kind:id` of the single target the grammar named when it did not compose a brief (filters r, t or g). */
  target?: { kind: 'region' | 'ticker' | 'gpu'; id: string }
}

// ---------- provenance -> freshness, statement class -> truth ----------

const KINDS: ProvenanceKind[] = ['live', 'archived', 'sample', 'derived']
const asKind = (k: string | null | undefined): ProvenanceKind | null => (k && (KINDS as string[]).includes(k) ? (k as ProvenanceKind) : null)

/**
 * The evidence's own envelope decides; a ref with no envelope but stated input kinds (an evaluation
 * verdict) is derived from those; a ref stating nothing is illustrative, because unknown is not observed.
 */
export function freshOfRef(ref: Pick<EvidenceRefWire, 'provenance' | 'provenanceKind' | 'inputKinds'>): { freshness: Freshness; label?: string } {
  const inputs = (ref.inputKinds ?? []).map(asKind).filter((k): k is ProvenanceKind => k !== null)
  if (ref.provenance) return freshOf({ kind: ref.provenance.kind, inputKinds: ref.provenance.inputKinds?.length ? ref.provenance.inputKinds : inputs })
  const kind = asKind(ref.provenanceKind)
  if (kind) return freshOf({ kind, inputKinds: inputs })
  if (inputs.length) return freshOf({ kind: 'derived', inputKinds: inputs })
  return { freshness: 'illustrative' }
}

/** Observation -> OBS only when the evidence is observed; sample-backed observations carry no glyph. */
export function truthOf(statementClass: string, freshness: Freshness): Truth | null {
  switch (statementClass) {
    case 'observation':
      return freshness === 'illustrative' ? null : 'obs'
    case 'inference':
      return 'inf'
    case 'forecast':
      return 'fct'
    case 'decision':
      return 'dec'
    default:
      return null
  }
}

/**
 * No cited evidence -> Insufficient. Any fact resting on authored sample data -> Low: the sentence is
 * true of the sample set, not of the market. Otherwise Moderate: the backend labels every composed
 * brief an inference and its evaluation verdicts are insufficient-sample today, so High is never
 * claimed from a composed brief.
 */
export function confidenceOf(facts: LiveFact[]): Confidence {
  if (!facts.length) return 'Insufficient'
  if (facts.some((f) => f.freshness === 'illustrative')) return 'Low'
  return 'Moderate'
}

// ---------- names ----------

export type Namer = (id: string) => string

/** Backend region ids in a composed sentence ("in us-tx") become the backend's names when they are known. */
export function nameRegions(text: string, namer: Namer = liveRegionName): string {
  return text.replace(/\b(us|eu|ap)-[a-z]{2}\b/g, (id) => {
    const n = namer(id)
    return n && n !== id ? n : id
  })
}

const GPU_LABEL: Record<string, string> = { b200: 'B200', h200: 'H200', h100: 'H100 SXM', a100: 'A100 80GB', l40s: 'L40S' }
const gpuDesign = (model: string | undefined): string | undefined => (model ? MODEL_GPU[model] : undefined)
const gpuLabel = (model: string | undefined): string => (model ? GPU_LABEL[MODEL_GPU[model] ?? ''] ?? model : '')

// ---------- backend routes -> design destinations ----------

function parseRoute(route: string): { path: string; q: URLSearchParams } {
  const [path, query = ''] = route.split('?')
  return { path, q: new URLSearchParams(query) }
}

/**
 * The backend speaks its own screens (/xray, /infra, /research, /feed, /markets, /companies, /gpu,
 * /procure, /desk, /map, /); the design has hash routes with its own ids. Null for a route with no
 * design counterpart (the /ask echo).
 */
export function mapRoute(route: string, shortNamer: Namer = liveRegionShort): Destination | null {
  const { path, q } = parseRoute(route)
  const r = q.get('r') ?? undefined
  const region = r && !r.includes(',') ? designRegionId(r) : undefined
  const g = q.get('g') ?? undefined
  const gpu = gpuDesign(g)
  const regionShort = (id: string) => shortNamer(id) || id
  if (path === '/xray') {
    const tab = q.get('tab')
    const xray: Destination['xray'] = {}
    if (region) xray.region = region
    if (gpu) xray.gpu = gpu
    if (tab === 'supply') xray.tab = 'pipeline'
    else if (tab === 'exposure') xray.tab = 'exposure'
    const what = tab === 'supply' ? 'supply' : gpu ? `${gpuLabel(g)} pressure` : 'pressure'
    return { label: r ? `Open ${regionShort(r)} ${what} in X-Ray` : 'Open X-Ray', page: 'xray', xray }
  }
  if (path.startsWith('/xray/anomalies/')) {
    // anomaly ids read anomaly:<type>:<region>:<gpu>
    const parts = path.slice('/xray/anomalies/'.length).split(':')
    const rid = parts[2]
    const xray: Destination['xray'] = {}
    if (rid) xray.region = designRegionId(rid)
    const gd = gpuDesign(parts[3])
    if (gd) xray.gpu = gd
    return { label: rid ? `Open ${regionShort(rid)} anomaly in X-Ray` : 'Open anomaly in X-Ray', page: 'xray', xray }
  }
  if (path === '/infra') {
    const market = region ? ENERGY.find((m) => m.regions.includes(region))?.id : undefined
    return { label: market ? `Open ${market.toUpperCase()} power in Energy` : 'Open Energy & Infra', page: 'energy', params: market ? { market } : undefined }
  }
  if (path === '/research') {
    const tab = q.get('tab')
    return { label: tab === 'evaluation' ? `Open ${q.get('target') ?? ''} evaluation in Research`.replace('  ', ' ') : 'Open Research & Eval', page: 'research' }
  }
  if (path === '/feed' || path.startsWith('/feed/')) return { label: 'Open News & Prediction', page: 'news' }
  if (path === '/markets' || path.startsWith('/markets/')) return { label: 'Open prediction markets', page: 'news' }
  if (path.startsWith('/companies/')) {
    const t = path.slice('/companies/'.length).toUpperCase()
    return { label: `Research ${t}`, page: 'securities', params: { t } }
  }
  if (path === '/gpu' || path.startsWith('/gpu/')) {
    const model = path.startsWith('/gpu/') ? path.slice('/gpu/'.length) : g
    const d = gpuDesign(model)
    return { label: q.get('view') === 'offers' || q.get('sort') === 'adjusted' ? 'Compare offers' : 'Open GPU Markets', page: 'markets', params: d ? { gpu: d } : undefined }
  }
  if (path === '/procure') return { label: 'Open Procurement Desk', page: 'procurement', params: gpu ? { gpu } : undefined }
  if (path === '/desk') return { label: 'Open Trading Desk', page: 'trading' }
  if (path === '/map') {
    return { label: r ? `Open ${regionShort(r)} in X-Ray` : 'Open X-Ray', page: 'xray', xray: region ? { region } : {} }
  }
  if (path === '/') return { label: 'Open Global Intelligence', page: 'overview' }
  return null
}

const destKey = (d: Destination) => `${d.page}|${JSON.stringify(d.params ?? {})}|${JSON.stringify(d.xray ?? {})}`

export function mapRoutes(routes: string[], shortNamer?: Namer, max = 4): Destination[] {
  const out: Destination[] = []
  const seen = new Set<string>()
  for (const r of routes) {
    const d = mapRoute(r, shortNamer)
    if (!d) continue
    const k = destKey(d)
    if (seen.has(k)) continue
    seen.add(k)
    out.push(d)
    if (out.length >= max) break
  }
  return out
}

// ---------- the brief -> facts ----------

const cap = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s)
const endStop = (s: string) => (/[.!?]$/.test(s.trim()) ? s.trim() : `${s.trim()}.`)

/** "kalshi · market", "flopx-sample-v1 · feed": the evidence's source and kind, never a design source line. */
function sourceOf(ref: EvidenceRefWire): string {
  const src = ref.provenance?.source
  return src ? `${src} · ${ref.kind}` : ref.kind
}

export function briefFacts(brief: BriefWire, namer: Namer = liveRegionName): LiveFact[] {
  const refs = new Map(brief.evidence.map((e) => [e.id, e]))
  const facts: LiveFact[] = []
  for (const s of brief.sentences) {
    const ref = s.evidenceIds.map((id) => refs.get(id)).find((e): e is EvidenceRefWire => !!e)
    if (!ref) continue // "the product holds no evidence for X yet" cites nothing and is not a fact
    const { freshness, label } = freshOfRef(ref)
    const at = ref.observedAt ?? ref.provenance?.observedAt ?? null
    facts.push({
      text: nameRegions(s.text, namer),
      source: sourceOf(ref),
      time: at ? fmtAsOf(at) || 'n/a' : 'n/a',
      truth: truthOf(s.statementClass, freshness),
      freshness,
      freshLabel: label,
      href: ref.href,
      evidenceId: ref.id,
    })
  }
  return facts
}

export function briefInterpretation(brief: BriefWire, facts: LiveFact[], namer: Namer = liveRegionName): string {
  const parts = [endStop(nameRegions(brief.headline, namer))]
  const uncited = brief.sentences.filter((s) => !s.evidenceIds.some((id) => brief.evidence.some((e) => e.id === id)))
  for (const s of uncited) parts.push(endStop(nameRegions(s.text, namer)))
  const sample = facts.filter((f) => f.freshness === 'illustrative').length
  if (facts.length) {
    parts.push(
      sample === 0
        ? `All ${facts.length} cited rows are observed or archived FLOPX data.`
        : sample === facts.length
          ? `Every cited row rests on the backend's authored sample set, which is observed nowhere, so this reads as illustrative.`
          : `${sample} of ${facts.length} cited rows rest on the backend's authored sample set and are badged illustrative; the rest are observed or archived.`,
    )
  }
  for (const c of brief.caveats) parts.push(endStop(cap(c)))
  parts.push(`Composed by the backend's ${brief.method} method from the evidence above; no language model.`)
  return parts.join(' ')
}

// ---------- intents without a brief ----------

const INTENT_LABEL: Record<string, string> = {
  gpu: 'GPU market', procure: 'procurement', feed: 'news feed', markets: 'prediction market', forecast: 'forecast', scenario: 'scenario',
  xray: 'series comparison', map: 'facilities map', research: 'research', desk: 'paper desk', company: 'company', overview: 'overview',
}
const MATCH_LABEL: Record<string, string> = { tickers: 'company', gpuModels: 'GPU', regions: 'region', providers: 'provider', measures: 'measure', actions: 'action', period: 'period' }

/** The design's engine-tool id for a backend intent, only where the two mean the same thing. */
export function toolOf(resp: AskResponse): string | undefined {
  if (resp.intent === 'procure') return 'plan_job'
  if (resp.intent === 'gpu' && (resp.filters.sort === 'adjusted' || resp.filters.view === 'offers')) return 'cheapest_compute'
  if (resp.intent === 'feed') return 'news'
  if (resp.intent === 'markets') return 'prediction_markets'
  return undefined
}

export function targetOf(resp: AskResponse): LiveAnswer['target'] | undefined {
  const f = resp.filters
  if (typeof f.t === 'string' && f.t) return { kind: 'ticker', id: f.t }
  if (typeof f.r === 'string' && f.r && !f.r.includes(',')) return { kind: 'region', id: f.r }
  if (typeof f.g === 'string' && f.g) return { kind: 'gpu', id: f.g }
  return undefined
}

function routeInterpretation(resp: AskResponse, namer: Namer): string {
  const matched = resp.explanation.matched ?? {}
  const bits = Object.entries(matched)
    .filter(([k, v]) => v.length && k !== 'actions')
    .map(([k, v]) => `${MATCH_LABEL[k] ?? k} ${v.join(', ')}`)
  const label = INTENT_LABEL[resp.intent] ?? resp.intent
  if (!bits.length) {
    return 'FLOPX matched none of these words to a region, GPU, company, provider or measure, so it has no evidence to cite and I will not guess. Ask about a named region, GPU or company for a composed brief.'
  }
  return `FLOPX read this as a ${label} question (matched: ${nameRegions(bits.join('; '), namer)}).${ROUTE_NO_FACTS}`
}

/** Dropped from a route-only interpretation once the target's brief has supplied the facts. */
const ROUTE_NO_FACTS = ' It composes evidence only for one named region, company or GPU, so this answer carries no fact rows; the screen it opens holds the data.'

// ---------- the whole answer ----------

export interface AdaptOpts {
  namer?: Namer
  shortNamer?: Namer
}

export function adaptAsk(resp: AskResponse, opts: AdaptOpts = {}): LiveAnswer {
  const namer = opts.namer ?? liveRegionName
  const shortNamer = opts.shortNamer ?? liveRegionShort
  const brief = resp.brief ?? null
  if (brief) {
    const facts = briefFacts(brief, namer)
    const routes = [...brief.followThrough.map((f) => f.route), ...resp.actions.filter((a) => a.kind === 'open').map((a) => a.route)]
    return {
      kind: 'brief',
      intent: resp.intent,
      facts,
      interpretation: briefInterpretation(brief, facts, namer),
      confidence: confidenceOf(facts),
      destinations: mapRoutes(routes, shortNamer),
      engine: `composed brief · ${brief.method}`,
      headline: nameRegions(brief.headline, namer),
      tool: undefined,
    }
  }
  const destinations = mapRoutes(resp.actions.filter((a) => a.kind === 'open' && !a.requiresConfirmation).map((a) => a.route), shortNamer)
  const target = targetOf(resp)
  const matchedAny = Object.values(resp.explanation.matched ?? {}).some((v) => v.length)
  return {
    kind: matchedAny ? 'route' : 'none',
    intent: resp.intent,
    facts: [],
    interpretation: routeInterpretation(resp, namer),
    confidence: 'Insufficient',
    destinations,
    engine: `grammar · intent ${resp.intent}`,
    tool: toolOf(resp),
    target,
  }
}

/**
 * A route-only answer that names one target gets that target's composed brief as its facts (the
 * brief says what it is about; the first response's screen stays the primary action).
 */
export function mergeTargetBrief(first: LiveAnswer, second: AskResponse, opts: AdaptOpts = {}): LiveAnswer {
  if (!second.brief) return first
  const b = adaptAsk(second, opts)
  const seen = new Set(first.destinations.map(destKey))
  return {
    ...first,
    kind: 'brief',
    facts: b.facts,
    interpretation: `${b.interpretation} ${first.interpretation.replace(ROUTE_NO_FACTS, '')}`.trim(),
    confidence: b.confidence,
    destinations: [...first.destinations, ...b.destinations.filter((d) => !seen.has(destKey(d)))].slice(0, 5),
    engine: `${first.engine} + composed brief`,
    headline: b.headline,
  }
}

/** The follow-up query that makes the grammar compose a brief for a target it named: ids are grammar tokens. */
export const briefQueryFor = (t: NonNullable<LiveAnswer['target']>): string => `tell me about ${t.id}`

// ---------- the network call ----------

/** Briefs compose in 7 to 14 s on the Render Starter plan; this is the only call allowed to wait that long. */
export const ASK_TIMEOUT_MS = 60_000

export function askLive(query: string, opts: Pick<FetchOpts, 'fetchImpl' | 'base' | 'timeoutMs'> = {}): Promise<LiveResult<AskResponse>> {
  return fetchJson<AskResponse>('/flopx/ask', { method: 'POST', body: { query }, timeoutMs: opts.timeoutMs ?? ASK_TIMEOUT_MS, fetchImpl: opts.fetchImpl, base: opts.base })
}
