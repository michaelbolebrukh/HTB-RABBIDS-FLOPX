/**
 * Intelligence-market analytics over an OpenRouter snapshot (src/data/openrouter.json).
 * Pure functions only; the page and the tests call these.
 *
 * Every price is a blended list price in $ per million tokens: 0.75 × prompt + 0.25 × completion.
 */

export interface Model { id: string; name: string; lab: string; created: number; open: boolean; context: number; prompt: number; completion: number }
export interface EndpointRow {
  model: string; provider: string; tag: string; quant: string; tier: string; context: number; uptime: number | null; up: boolean
  tools: boolean; structured: boolean; caching: boolean; timeOfDay: boolean; discount: number; prompt: number; completion: number
}
export interface Snapshot { catalogAt: string; endpointsAt: string; aliases: number; models: Model[]; endpoints: EndpointRow[] }

export const blended = (p: { prompt: number; completion: number }) => 0.75 * p.prompt + 0.25 * p.completion

export function median(xs: number[]): number {
  if (!xs.length) return NaN
  const s = [...xs].sort((a, b) => a - b)
  const m = s.length >> 1
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2
}

/** Precision bucket from a provider's quantization label. */
export type Bits = 4 | 8 | 16 | 0
export function bits(quant: string): Bits {
  const q = quant.toLowerCase()
  if (/fp4|int4|mxfp4|nvfp4|^4|w4/.test(q)) return 4
  if (/fp8|int8|^8|w8/.test(q)) return 8
  if (/bf16|fp16|fp32|16|32/.test(q)) return 16
  return 0
}
export const bitsLabel = (b: Bits) => (b === 0 ? 'unknown' : b === 16 ? '16 bit+' : `${b} bit`)

// ── Layer 1: labs ────────────────────────────────────────────────────────────

export interface LabRow { lab: string; models: Model[]; open: number; median: number; min: number; max: number }
export function labLadder(models: Model[], minModels = 3): LabRow[] {
  const by = new Map<string, Model[]>()
  for (const m of models) by.set(m.lab, [...(by.get(m.lab) ?? []), m])
  return [...by]
    .filter(([, ms]) => ms.length >= minModels)
    .map(([lab, ms]) => {
      const ps = ms.map(blended)
      return { lab, models: ms, open: ms.filter((m) => m.open).length, median: median(ps), min: Math.min(...ps), max: Math.max(...ps) }
    })
    .sort((a, b) => b.median - a.median)
}

export function closedVsOpen(models: Model[]) {
  const closed = models.filter((m) => !m.open).map(blended)
  const open = models.filter((m) => m.open).map(blended)
  const mc = median(closed), mo = median(open)
  return { closed, open, closedMedian: mc, openMedian: mo, ratio: mc / mo }
}

// ── Layer 2: providers ───────────────────────────────────────────────────────

/** Endpoints that count for spreads, routing and the fit: up, standard tier. */
export const live = (e: EndpointRow) => e.up
export const standardLive = (e: EndpointRow) => e.up && e.tier === 'standard'

export interface Spread { model: string; hosts: number; cheapest: EndpointRow; dearest: EndpointRow; median: number; spread: number; samePrecision: { bits: Bits; spread: number } | null }
/** Dearest ÷ cheapest standard-tier endpoint that is up, plus the spread inside the largest labelled precision group with ≥3 endpoints. */
export function spreadFor(model: string, eps: EndpointRow[]): Spread | null {
  const xs = eps.filter((e) => e.model === model && standardLive(e)).sort((a, b) => blended(a) - blended(b))
  if (xs.length < 2) return null
  const groups = new Map<Bits, EndpointRow[]>()
  for (const e of xs) { const b = bits(e.quant); if (b) groups.set(b, [...(groups.get(b) ?? []), e]) }
  const big = [...groups].filter(([, g]) => g.length >= 3).sort((a, b) => b[1].length - a[1].length)[0]
  return {
    model, hosts: xs.length, cheapest: xs[0], dearest: xs[xs.length - 1], median: median(xs.map(blended)),
    spread: blended(xs[xs.length - 1]) / blended(xs[0]),
    samePrecision: big ? { bits: big[0], spread: blended(big[1][big[1].length - 1]) / blended(big[1][0]) } : null,
  }
}
/** Spreads for every open model with 2+ live hosts, deepest markets (most endpoints) first. */
export function spreads(eps: EndpointRow[]): Spread[] {
  const ids = [...new Set(eps.map((e) => e.model))]
  return ids.map((id) => spreadFor(id, eps)).filter((s): s is Spread => !!s).sort((a, b) => b.hosts - a.hosts || b.spread - a.spread)
}

export interface RouteNeeds { promptM: number; completionM: number; minUptime: number; standardOnly: boolean; tools: boolean }
export interface RoutePlan { e: EndpointRow; cost: number }
/** Cheapest endpoints that meet the job: cost = list price × volume. */
export function route(model: string, eps: EndpointRow[], n: RouteNeeds) {
  const all = eps.filter((e) => e.model === model)
  const feasible = all
    .filter((e) => e.up && (e.uptime ?? 0) >= n.minUptime && (!n.standardOnly || e.tier === 'standard') && (!n.tools || e.tools))
    .map((e) => ({ e, cost: e.prompt * n.promptM + e.completion * n.completionM }))
    .sort((a, b) => a.cost - b.cost)
  const best = feasible[0] ?? null
  const med = median(feasible.map((f) => f.cost))
  const worst = feasible[feasible.length - 1]
  return {
    listed: all.length, plans: feasible, best, median: med,
    saving: best ? med - best.cost : 0, savingPct: best ? (1 - best.cost / med) * 100 : 0,
    vsDearestPct: best && worst ? (1 - best.cost / worst.cost) * 100 : 0,
  }
}

// ── Hedonic fit: log blended price on attributes, with model fixed effects ───

export const FEATURES = [
  { key: 'priority', label: 'Priority tier, vs standard', group: 'tier' },
  { key: 'flex', label: 'Flex tier, vs standard', group: 'tier' },
  { key: 'b4', label: '4-bit weights, vs 8-bit', group: 'precision' },
  { key: 'b16', label: '16-bit or more, vs 8-bit', group: 'precision' },
  { key: 'bNone', label: 'No precision label, vs 8-bit', group: 'precision' },
  { key: 'ctxCap', label: 'Context capped at half the model maximum', group: 'context' },
  { key: 'down', label: 'Each point of downtime, last day', group: 'uptime' },
  { key: 'tools', label: 'Supports tool calling', group: 'features' },
  { key: 'structured', label: 'Supports structured outputs', group: 'features' },
  { key: 'caching', label: 'Supports prompt caching', group: 'features' },
  { key: 'tod', label: 'Has time-of-day pricing', group: 'features' },
] as const
export type FeatureKey = (typeof FEATURES)[number]['key']

export function featuresOf(e: EndpointRow, modelMaxCtx: number): number[] {
  const b = bits(e.quant)
  return [
    e.tier === 'priority' || e.tier === 'ultrafast' ? 1 : 0, e.tier === 'flex' ? 1 : 0,
    b === 4 ? 1 : 0, b === 16 ? 1 : 0, b === 0 ? 1 : 0,
    e.context < modelMaxCtx / 2 ? 1 : 0,
    Math.max(0, 100 - (e.uptime ?? 100)),
    e.tools ? 1 : 0, e.structured ? 1 : 0, e.caching ? 1 : 0, e.timeOfDay ? 1 : 0,
  ]
}

/** Solve A x = b (A small, symmetric positive definite after ridge) by Gaussian elimination with partial pivoting. */
export function solve(A: number[][], b: number[]): number[] {
  const n = b.length
  const M = A.map((r, i) => [...r, b[i]])
  for (let c = 0; c < n; c++) {
    let p = c
    for (let r = c + 1; r < n; r++) if (Math.abs(M[r][c]) > Math.abs(M[p][c])) p = r
    ;[M[c], M[p]] = [M[p], M[c]]
    const d = M[c][c] || 1e-12
    for (let r = 0; r < n; r++) {
      if (r === c) continue
      const f = M[r][c] / d
      for (let k = c; k <= n; k++) M[r][k] -= f * M[c][k]
    }
  }
  return M.map((r, i) => r[n] / (r[i] || 1e-12))
}

export interface Fit {
  coef: number[]; r2: number; r2Model: number; withinShare: number; n: number; models: number
  /** Per endpoint (same order as `rows`): fitted log price, fair $/M and residual (log). */
  rows: { e: EndpointRow; x: number[]; y: number; fitted: number; fair: number; resid: number; fe: number }[]
}
/**
 * Ridge on log(blended) ~ model fixed effects + attributes, fitted by the within
 * transformation: demean y and x inside each model, ridge-solve the attributes,
 * then recover each model's intercept. Uses up endpoints of models with 2+ of them.
 */
export function hedonic(eps: EndpointRow[], lambda = 1): Fit {
  const by = new Map<string, EndpointRow[]>()
  for (const e of eps) if (live(e) && blended(e) > 0) by.set(e.model, [...(by.get(e.model) ?? []), e])
  const groups = [...by].filter(([, g]) => g.length >= 2)
  const k = FEATURES.length
  const data = groups.map(([m, g]) => {
    const maxCtx = Math.max(...g.map((e) => e.context))
    const xs = g.map((e) => featuresOf(e, maxCtx)), ys = g.map((e) => Math.log(blended(e)))
    const xm = Array.from({ length: k }, (_, j) => xs.reduce((s, x) => s + x[j], 0) / g.length)
    const ym = ys.reduce((s, y) => s + y, 0) / g.length
    return { m, g, xs, ys, xm, ym }
  })
  const A = Array.from({ length: k }, () => new Array(k).fill(0)), b = new Array(k).fill(0)
  for (const d of data) d.xs.forEach((x, i) => {
    const xd = x.map((v, j) => v - d.xm[j]), yd = d.ys[i] - d.ym
    for (let p = 0; p < k; p++) { b[p] += xd[p] * yd; for (let q = 0; q < k; q++) A[p][q] += xd[p] * xd[q] }
  })
  for (let p = 0; p < k; p++) A[p][p] += lambda
  const coef = solve(A, b)
  const rows: Fit['rows'] = []
  let ssTot = 0, ssRes = 0, ssModel = 0, ssWithin = 0
  const all = data.flatMap((d) => d.ys)
  const grand = all.reduce((s, y) => s + y, 0) / (all.length || 1)
  for (const d of data) {
    const fe = d.ym - coef.reduce((s, c, j) => s + c * d.xm[j], 0)
    d.g.forEach((e, i) => {
      const fitted = fe + coef.reduce((s, c, j) => s + c * d.xs[i][j], 0)
      const y = d.ys[i]
      rows.push({ e, x: d.xs[i], y, fitted, fair: Math.exp(fitted), resid: y - fitted, fe })
      ssTot += (y - grand) ** 2; ssRes += (y - fitted) ** 2; ssModel += (y - d.ym) ** 2; ssWithin += (y - d.ym) ** 2
    })
  }
  return {
    coef, n: rows.length, models: data.length, rows,
    r2: 1 - ssRes / ssTot, r2Model: 1 - ssModel / ssTot,
    withinShare: ssWithin > 0 ? 1 - ssRes / ssWithin : 0,
  }
}

/** Listed-price effect of a log coefficient (dummy): exp(c) − 1. */
export const pctEffect = (c: number) => (Math.exp(c) - 1) * 100

/**
 * Decompose the log-point gap between two endpoints of the same model:
 * gap = Σ attribute terms (explained) + residual difference (unexplained). Exact by construction.
 */
export function decompose(fit: Fit, a: EndpointRow, b: EndpointRow) {
  const ra = fit.rows.find((r) => r.e === a), rb = fit.rows.find((r) => r.e === b)
  if (!ra || !rb) return null
  const groups = new Map<string, number>()
  FEATURES.forEach((f, j) => {
    const v = 100 * fit.coef[j] * (ra.x[j] - rb.x[j])
    groups.set(f.group, (groups.get(f.group) ?? 0) + v)
  })
  const gap = 100 * (ra.y - rb.y)
  const explained = [...groups.values()].reduce((s, v) => s + v, 0)
  return { gap, groups, explained, unexplained: 100 * (ra.resid - rb.resid), a: ra, b: rb }
}

/** Median listed-vs-fair by host: exp(median log residual) − 1, hosts with ≥ minN endpoints. */
export function leaderboard(fit: Fit, minN = 5) {
  const by = new Map<string, number[]>()
  for (const r of fit.rows) by.set(r.e.provider, [...(by.get(r.e.provider) ?? []), r.resid])
  return [...by]
    .filter(([, rs]) => rs.length >= minN)
    .map(([provider, rs]) => ({ provider, n: rs.length, vsFair: (Math.exp(median(rs)) - 1) * 100 }))
    .sort((x, y) => x.vsFair - y.vsFair)
}

// ── Tiers: one product line per lab, latest release ──────────────────────────

export const TIERS = {
  large: [/claude opus/i, /^gemini [\d.]+ pro( preview)?$/i, /gpt-[\d.]+ (astra|sol)$/i, /^grok [\d.]+$/i, /deepseek v[\d.]+ pro/i, /kimi k[\d.]+ pro/i, /^glm [\d.]+$/i, /nemotron.*ultra/i],
  small: [/claude haiku/i, /gemini [\d.]+ flash$/i, /gpt-[\d.]+ luna$/i, /grok.*fast/i, /deepseek v[\d.]+ flash$/i, /^glm [\d.]+ flash$/i, /minimax.*flash/i, /qwen[\d.]+ \d+b$/i],
} as const
/** Latest model per line pattern (distinct product lines; GPT Astra and Sol are kept apart). */
export function tierModels(models: Model[], patterns: readonly RegExp[]): Model[] {
  const out = new Map<string, Model>()
  for (const re of patterns) {
    for (const m of models.filter((x) => re.test(x.name))) {
      const line = `${re.source}|${m.name.replace(/[\d.]+/g, '#').toLowerCase()}`
      const cur = out.get(line)
      if (!cur || m.created > cur.created) out.set(line, m)
    }
  }
  // Keep one entry per line pattern and family word (the newest), so GPT Astra and Sol both appear.
  const byFamily = new Map<string, Model>()
  for (const [line, m] of out) {
    const fam = line.split('|')[0] + '|' + m.name.replace(/[\d.]+.*$/, '').trim().toLowerCase() + (/(astra|sol)/i.exec(m.name)?.[1]?.toLowerCase() ?? '')
    const cur = byFamily.get(fam)
    if (!cur || m.created > cur.created) byFamily.set(fam, m)
  }
  return [...byFamily.values()]
}
