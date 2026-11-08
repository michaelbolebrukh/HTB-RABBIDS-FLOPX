/**
 * Snapshot of the FLOPX engine build described in the 7 Nov 2026 hand-off (Arsenii's engine, Emre's collectors).
 * These are the numbers the engine reported at 18:16–18:28 UTC; they are shown as a snapshot, never as live data.
 * Anything a page adds on top of these to fill a chart must carry the "Illustrative" label.
 */

import openrouter from './openrouter.json'

export const SNAPSHOT = {
  label: 'FLOPX engine build · 7 Nov 2026 18:16–18:28 UTC',
  short: 'Snapshot 7 Nov 18:16 UTC',
  source: 'FLOPX engine build (hand-off)',
} as const

/** $ per million tokens, blended 3:1 input to output. */
export interface LabRow { lab: string; models: number; median: number; cheapest: { model: string; price: number }; dearest: { model: string; price: number } }

/**
 * One source for the intelligence-market counts: the committed OpenRouter snapshot the Intelligence
 * page renders (text models after alias removal). The hand-off's raw catalogue counts (342 models,
 * 61 labs, 768 endpoints, 63 providers at 18:11 UTC) counted a different basis and are not repeated.
 */
const OR_COUNTS = (() => {
  const s = openrouter as { catalogAt: string; endpointsAt: string; models: { lab: string; open: boolean }[]; endpoints: { provider: string; model: string }[] }
  const stamp = (iso: string) => new Date(iso).toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: 'UTC' })
  return {
    models: s.models.length,
    labs: new Set(s.models.map((m) => m.lab)).size,
    openWeight: s.models.filter((m) => m.open).length,
    endpoints: s.endpoints.length,
    openModels: new Set(s.endpoints.map((e) => e.model)).size,
    providers: new Set(s.endpoints.map((e) => e.provider)).size,
    asOf: `OpenRouter · ${stamp(s.catalogAt)} UTC snapshot (text models after alias removal)`,
  }
})()

export const TOKENS = {
  asOf: OR_COUNTS.asOf,
  catalog: { models: OR_COUNTS.models, labs: OR_COUNTS.labs, openWeight: OR_COUNTS.openWeight },
  endpoints: { endpoints: OR_COUNTS.endpoints, openModels: OR_COUNTS.openModels, providers: OR_COUNTS.providers },
  labs: [
    { lab: 'Anthropic', models: 15, median: 10.0, cheapest: { model: 'Claude Haiku 5.5', price: 0.2 }, dearest: { model: 'Claude Opus 4.1', price: 30.0 } },
    { lab: 'OpenAI', models: 53, median: 4.0, cheapest: { model: 'gpt-oss-120b', price: 0.025 }, dearest: { model: 'GPT-5.5 Pro', price: 67.5 } },
    { lab: 'Google', models: 21, median: 0.85, cheapest: { model: 'Gemma 3 4B', price: 0.06 }, dearest: { model: 'Gemini 3.1 Pro', price: 4.5 } },
    { lab: 'DeepSeek', models: 14, median: 0.44, cheapest: { model: 'V4 Flash', price: 0.11 }, dearest: { model: 'V4 Pro', price: 1.19 } },
    { lab: 'Mistral', models: 20, median: 0.43, cheapest: { model: 'Mistral Nemo', price: 0.03 }, dearest: { model: 'Mistral Medium 3.5', price: 3.0 } },
    { lab: 'NVIDIA', models: 6, median: 0.18, cheapest: { model: 'Nemotron 3.5 Lightning', price: 0.1 }, dearest: { model: 'Nemotron 3 Ultra', price: 1.05 } },
  ] satisfies LabRow[],
  /** Same open model, different host. Only the two ends are real endpoint prices. */
  spread: {
    model: 'DeepSeek V4.1 Flash',
    providers: 39,
    cheapest: { provider: 'Morph', price: 0.1165 },
    dearest: { provider: 'Fireworks', price: 0.7875 },
    ratioSamePrecision: 4.5,
  },
  typicalRatio: 1.9,
  modelsAtLeast3x: 24,
  frontierVsOpen: 20,
  r2: 0.89,
  r2Features: 'precision, tier, context cap, uptime and tool support',
  caveats: [
    'List prices, not executed spend.',
    'About a quarter of endpoints do not state their quantization.',
    'One archived OpenRouter snapshot; FLOPX has no live token-market feed yet.',
  ],
} as const

export const GPU_FV = {
  asks: 3525,
  asOf: 'Vast snapshot · 7 Nov 17:05 UTC',
  r2: 0.83,
  features: 'GPU type, count, reliability, duration cap, bandwidth, country and bid market',
  example: {
    gpu: '1× H200 NVL',
    low: { place: 'Quebec', price: 3.6 },
    high: { place: 'Czechia', price: 5.46 },
    gapLogPts: 41.6,
    specsLogPts: 19.4,
  },
} as const

export const BREV = {
  clouds: 15,
  locations: 85,
  rows: 2151,
  r2: 0.93,
  examples: ['Nebius', 'Crusoe', 'Lambda', 'GCP', 'AWS'],
  h200sxm: { brev: 5.29, vast: 4.34 },
  note: 'Sells whole 8-GPU nodes that Vast often lacks.',
} as const

export interface ProcurementPlan { id: 'other' | 'now' | 'flex' | 'interrupt'; label: string; cost: number; note: string; feasible: boolean; recommended?: boolean }

export const PROCUREMENT = {
  job: '64 GPU-hours of H200 SXM on ≥4 verified GPUs within 24 hours',
  jobNote: 'No H200 NVL offer can run it right now, so the plan uses H200 SXM.',
  median: 465.36,
  plans: [
    { id: 'other', label: 'Other marketplace', cost: 414.93, feasible: true, recommended: true, note: '8× H200 on Nebius via Brev, us-central1, $6.48 per GPU-hour' },
    { id: 'now', label: 'Run now', cost: 439.82, feasible: true, note: 'Cheapest feasible Vast offer' },
    { id: 'flex', label: 'Flexible', cost: 439.82, feasible: true, note: 'Same as run now, because the timing gate is closed' },
    { id: 'interrupt', label: 'Interruptible', cost: 292.45, feasible: false, note: 'Cheaper, but the job does not allow interruption' },
  ] satisfies ProcurementPlan[],
} as const

export const TIMING_GATE = {
  open: false,
  historyHours: 0,
  historyNeeded: 24,
  skillPct: 1.8,
  skillNeeded: 5,
  note: 'The saving comes from search and feasibility, not from timing the market.',
} as const

export const XRAY_CAPACITY = {
  trackedGW: 50.4,
  sites: 96,
  energizedGW: 19.5,
  regions: 12,
  pillars: 5,
  source: 'Epoch AI frontier data centres (CC BY 4.0)',
  ranks: [
    { region: 'ERCOT (Texas)', cpi: 65, lo: 51, hi: 76 },
    { region: 'Europe', cpi: 58 },
    { region: 'UAE', cpi: 56 },
  ],
} as const

export const ENERGY_SUMMARY = {
  markets: 28,
  real: 27,
  cheapest: { market: 'MISO Arkansas', price: 39 },
  dearest: { market: 'Hungary', price: 236 },
  simulated: 'ERCOT is simulated: its site blocks the venue IP and no free source carries its prices.',
  licences: 'Energy-Charts CC BY 4.0 for AT, BE, CZ, DE_LU, DK_1, FR, HU, NL, PL; private and internal use only for BG, ES, FI, NO_1, RO, SE_3, SK. PJM prices courtesy of EIA and ICE.',
} as const

export const PM_CALIBRATION = {
  markets: 330,
  events: 46,
  tradedUsd: 3.0e6,
  week: { cutPct: 48, from: 31, to: 64 },
  month: { cutPct: 10, significant: false },
} as const

export const NEWS_SUMMARY = {
  headlines: 60,
  days: 7,
  source: 'GDELT (English)',
  themes: ['CoreWeave', 'Nebius', 'Data-centre power', 'Texas data centres', 'Moratoriums'],
} as const

export const FUND_TESTS = {
  tickers: 17,
  tests: 24,
  edges: 0,
  exploratoryOrders: 15,
  prices: 'WRDS CRSP to 2025, Yahoo for 2026',
} as const

export const AGENT_TOOLS: { id: string; label: string; desc: string }[] = [
  { id: 'cheapest_compute', label: 'Cheapest compute', desc: 'Cheapest feasible GPU ask for a spec' },
  { id: 'plan_job', label: 'Plan a job', desc: 'Run now, flexible, interruptible or another venue' },
  { id: 'fair_value', label: 'Fair value', desc: 'What an ask is worth from its specs' },
  { id: 'brev_venue', label: 'Second venue', desc: 'Brev offers across 15 clouds' },
  { id: 'timing_gate', label: 'Timing gate', desc: 'Whether the forecast may time a purchase' },
  { id: 'token_prices', label: 'Token prices', desc: 'Lab list prices per million tokens' },
  { id: 'token_providers', label: 'Token providers', desc: 'Cheapest host for an open model' },
  { id: 'scarcity', label: 'Scarcity', desc: 'Compute Pressure Index by region' },
  { id: 'grid_slip', label: 'Grid slip', desc: 'What a one-year delay does to pressure' },
  { id: 'power_cost', label: 'Power cost', desc: 'Electricity cost per GPU-hour' },
  { id: 'prediction_markets', label: 'Prediction markets', desc: 'Implied odds and calibration' },
  { id: 'news', label: 'News', desc: 'Recent headlines on five themes' },
  { id: 'fund_tests', label: 'Fund tests', desc: 'Pre-registered edge tests' },
]

export interface Claim { claim: string; number: string; file: string; recheck: boolean }

export const CLAIMS: Claim[] = [
  { claim: 'Same open model, different provider', number: '6.8×: DeepSeek V4.1 Flash $0.1165 (Morph) vs $0.7875 (Fireworks); 4.5× at the same 8-bit precision', file: 'tokens', recheck: true },
  { claim: 'Frontier lab vs open model', number: 'Anthropic median $10.00 per M tokens vs DeepSeek $0.44', file: 'tokens', recheck: true },
  { claim: 'Coverage of the intelligence market', number: `${OR_COUNTS.models} models from ${OR_COUNTS.labs} labs; ${OR_COUNTS.endpoints} endpoints from ${OR_COUNTS.providers} providers (OpenRouter snapshot, same basis as the Intelligence page)`, file: 'tokens', recheck: true },
  { claim: 'GPU asks priced', number: '3,525 Vast asks at 17:05 UTC, fair-value r² 0.83', file: 'gpu_markets', recheck: true },
  { claim: 'Same GPU, different place', number: 'H200 NVL $3.60 Quebec vs $5.46 Czechia; specs explain 19.4 of 41.6 log points', file: 'fair_value', recheck: true },
  { claim: 'Procurement saving', number: '10.8% below the median feasible ask ($414.93 vs $465.36) via Brev on Nebius', file: 'procurement', recheck: true },
  { claim: 'Timing', number: 'Gate closed: 0 h out-of-sample history (needs 24), +1.8% skill (needs +5%)', file: 'forecast', recheck: true },
  { claim: 'Hidden capacity', number: '39% of 50.4 GW energized; ERCOT pressure 65 (51 to 76)', file: 'xray', recheck: false },
  { claim: 'Prediction markets', number: '48% less error than the base rate a week out; 10% a month out, not different from zero', file: 'pm', recheck: false },
  { claim: 'Fund', number: 'Edge in 0 of 24 pre-registered tests', file: 'fund', recheck: false },
  { claim: 'Scale', number: '68,480 rows ingested in the last build', file: 'meta', recheck: true },
]

export const NEVER_CLAIM: string[] = [
  'Timing alpha: the gate is closed.',
  'That routing exploits mispricing: it buys the cheapest feasible ask.',
  'Real ERCOT power or AWS spot history: both are simulated.',
  'A backtested saving: the backtest needs more Vast snapshots and says "not answerable yet".',
]

/** Derived figures the pages quote; computed so the constants above cannot drift from the text. */
export const derived = {
  providerRatio: TOKENS.spread.dearest.price / TOKENS.spread.cheapest.price,
  energizedShare: XRAY_CAPACITY.energizedGW / XRAY_CAPACITY.trackedGW,
  procurementSaving: 1 - PROCUREMENT.plans.find((p) => p.recommended)!.cost / PROCUREMENT.median,
  sameGpuPremium: GPU_FV.example.high.price / GPU_FV.example.low.price - 1,
  regionLogPts: GPU_FV.example.gapLogPts - GPU_FV.example.specsLogPts,
  brevPremium: BREV.h200sxm.brev / BREV.h200sxm.vast - 1,
}
