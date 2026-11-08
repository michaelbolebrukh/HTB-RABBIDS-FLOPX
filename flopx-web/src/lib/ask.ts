import { AS_OF, NOW_IDX, OFFERS, providerById, regionById, type Freshness, type Truth } from '@/data/core'
import { lastPx } from '@/data/fund'
import { adaptAsk, askLive, briefQueryFor, mergeTargetBrief, type Destination, type LiveAnswer } from '@/live/adapt-w4'
import { BREV, ENERGY_SUMMARY, FUND_TESTS, NEWS_SUMMARY, PM_CALIBRATION, PROCUREMENT, SNAPSHOT, TIMING_GATE, TOKENS, XRAY_CAPACITY, derived } from '@/data/handoff'
import { pressureAt, pressureWord } from '@/lib/model'
import type { Store } from '@/lib/store'

const src = SNAPSHOT.source
const time = SNAPSHOT.short
const f = (text: string, source: string = src) => ({ text, source, time })
const go = (label: string, page: 'intelligence' | 'procurement' | 'markets' | 'xray' | 'energy' | 'news' | 'fund') => ({ label, run: (st: Store) => st.navigate(page) })
const has = (t: string, ...w: string[]) => w.some((x) => t.includes(x))

/** Keyword router over the 7 Nov hand-off snapshot. Order matters: most specific first. */
function handoffAnswer(q: string, t: string, ctx: string): Answer | null {
  const base = { q, context: ctx }
  const anth = TOKENS.labs[0], deep = TOKENS.labs[3]
  if (has(t, 'brev', 'second venue', 'other marketplace', 'another venue')) {
    return { ...base, tool: 'brev_venue', confidence: 'Moderate',
      facts: [f(`Brev lists ${BREV.rows.toLocaleString()} offers across ${BREV.clouds} clouds and ${BREV.locations} locations (e.g. ${BREV.examples.join(', ')}).`),
        f(`H200 SXM: Brev $${BREV.h200sxm.brev.toFixed(2)} vs Vast $${BREV.h200sxm.vast.toFixed(2)} per GPU-hour (${(derived.brevPremium * 100).toFixed(0)}% higher on Brev).`),
        f(`Brev: ${BREV.note} Fair-value r² ${BREV.r2}.`)],
      interpretation: 'Brev is a second venue, not a cheaper one per hour. It matters when the job needs whole 8-GPU nodes that Vast often lacks, so the cheapest feasible ask can sit there.',
      actions: [go('Open procurement', 'procurement')] }
  }
  if (has(t, 'timing', 'wait', 'gate')) {
    return { ...base, tool: 'timing_gate', confidence: 'High',
      facts: [f(`Timing gate is ${TIMING_GATE.open ? 'open' : 'closed'}: ${TIMING_GATE.historyHours} h of out-of-sample history (needs ${TIMING_GATE.historyNeeded}).`),
        f(`Forecast skill +${TIMING_GATE.skillPct}% (needs +${TIMING_GATE.skillNeeded}%).`)],
      interpretation: `The forecast may not time a purchase yet. ${TIMING_GATE.note} Flexible therefore costs the same as run now.`,
      actions: [go('Open procurement', 'procurement')] }
  }
  if (has(t, 'procure', 'plan', 'cheapest feasible', 'job', 'buy')) {
    const rec = PROCUREMENT.plans.find((p) => p.recommended)!
    return { ...base, tool: 'plan_job', confidence: 'Moderate',
      facts: [f(`Job: ${PROCUREMENT.job}. ${PROCUREMENT.jobNote}`),
        f(`Recommended: ${rec.label}, $${rec.cost.toFixed(2)} (${rec.note}).`),
        f(`Median feasible ask $${PROCUREMENT.median.toFixed(2)}, so ${(derived.procurementSaving * 100).toFixed(1)}% saved.`),
        ...PROCUREMENT.plans.filter((p) => !p.recommended).map((p) => f(`${p.label}: $${p.cost.toFixed(2)}${p.feasible ? '' : ' (not feasible)'}. ${p.note}.`))],
      interpretation: 'The saving comes from searching more venues and checking feasibility. It is the cheapest feasible ask, not a claim that the market is mispriced, and not a timing gain. Not backtested.',
      actions: [go('Open procurement', 'procurement')] }
  }
  if (has(t, 'provider', 'same model', 'same-model', 'spread', 'host')) {
    const sp = TOKENS.spread
    return { ...base, tool: 'token_providers', confidence: 'Moderate',
      facts: [f(`${sp.model}: ${sp.providers} providers, $${sp.cheapest.price} (${sp.cheapest.provider}) to $${sp.dearest.price} (${sp.dearest.provider}) per M tokens, ${derived.providerRatio.toFixed(1)}×.`, TOKENS.asOf),
        f(`At the same 8-bit precision the gap is ${sp.ratioSamePrecision}×; typical ratio ${TOKENS.typicalRatio}×, ${TOKENS.modelsAtLeast3x} models at 3× or more.`, TOKENS.asOf)],
      interpretation: `Part of the spread is precision and tier (r² ${TOKENS.r2} from ${TOKENS.r2Features}). About a quarter of endpoints do not state quantization. These are list prices, not executed spend.`,
      actions: [go('Open intelligence', 'intelligence')] }
  }
  if (has(t, 'token', 'llm', ' labs', 'frontier', 'open model', 'open-weight', 'per million')) {
    return { ...base, tool: 'token_prices', confidence: 'Moderate',
      facts: [f(`${anth.lab} median $${anth.median.toFixed(2)} per M tokens vs ${deep.lab} $${deep.median.toFixed(2)}.`, TOKENS.asOf),
        f(`${TOKENS.catalog.models} models from ${TOKENS.catalog.labs} labs; ${TOKENS.catalog.openWeight} open-weight.`, TOKENS.asOf),
        ...TOKENS.labs.slice(1, 3).map((l) => f(`${l.lab} median $${l.median.toFixed(2)} (${l.models} models).`, TOKENS.asOf))],
      interpretation: `Frontier labs list at roughly ${TOKENS.frontierVsOpen}× open models at the median. Prices differ with capability, so this is not a like-for-like saving. ${TOKENS.caveats[0]}`,
      actions: [go('Open intelligence', 'intelligence')] }
  }
  if (has(t, 'scarcity', 'x-ray', 'xray', 'hidden capacity', 'energized', 'energised', 'pressure index', 'cpi')) {
    const er = XRAY_CAPACITY.ranks[0]
    return { ...base, tool: 'scarcity', confidence: 'Moderate',
      facts: [f(`${(derived.energizedShare * 100).toFixed(0)}% of ${XRAY_CAPACITY.trackedGW} GW tracked is energized (${XRAY_CAPACITY.energizedGW} GW, ${XRAY_CAPACITY.sites} sites).`, XRAY_CAPACITY.source),
        f(`Compute Pressure Index: ${er.region} ${er.cpi} (${'lo' in er ? er.lo : ''} to ${'hi' in er ? er.hi : ''}), then ${XRAY_CAPACITY.ranks.slice(1).map((r) => `${r.region} ${r.cpi}`).join(', ')}.`)],
      interpretation: 'Capacity under construction is not capacity you can rent. The index is inferred from indicators, with an interval, and is not an observed utilisation figure.',
      actions: [go('Open X-Ray', 'xray')] }
  }
  if (has(t, 'power', 'electric', 'energy', 'mwh')) {
    return { ...base, tool: 'power_cost', confidence: 'Moderate',
      facts: [f(`Cheapest market ${ENERGY_SUMMARY.cheapest.market} $${ENERGY_SUMMARY.cheapest.price}/MWh; dearest ${ENERGY_SUMMARY.dearest.market} $${ENERGY_SUMMARY.dearest.price}/MWh.`),
        f(`${ENERGY_SUMMARY.real} of ${ENERGY_SUMMARY.markets} markets carry real prices.`),
        f(ENERGY_SUMMARY.simulated)],
      interpretation: 'Power is a cost input, not a rental price. ERCOT numbers are simulated and must not be read as observed.',
      actions: [go('Open energy', 'energy')] }
  }
  if (has(t, 'prediction', 'polymarket', 'kalshi', 'odds')) {
    return { ...base, tool: 'prediction_markets', confidence: 'Low',
      facts: [f(`${PM_CALIBRATION.markets} markets over ${PM_CALIBRATION.events} events, $${(PM_CALIBRATION.tradedUsd / 1e6).toFixed(1)}M traded.`),
        f(`A week out, error is ${PM_CALIBRATION.week.cutPct}% below the base rate (interval ${PM_CALIBRATION.week.from} to ${PM_CALIBRATION.week.to}%).`),
        f(`A month out, ${PM_CALIBRATION.month.cutPct}%, not statistically different from zero.`)],
      interpretation: 'Short-horizon odds look informative; month-ahead odds are not shown to beat the base rate. No trading edge is claimed.',
      actions: [go('Open markets', 'markets')] }
  }
  if (has(t, 'news', 'headline', 'gdelt')) {
    return { ...base, tool: 'news', confidence: 'Low',
      facts: [f(`${NEWS_SUMMARY.headlines} headlines over ${NEWS_SUMMARY.days} days from ${NEWS_SUMMARY.source}.`), f(`Themes: ${NEWS_SUMMARY.themes.join(', ')}.`)],
      interpretation: 'Headlines show what is being discussed, not what is true. They do not by themselves change any price estimate.',
      actions: [go('Open news', 'news')] }
  }
  if (has(t, 'edge', 'backtest', 'pre-registered', 'fund test', 'alpha')) {
    return { ...base, tool: 'fund_tests', confidence: 'High',
      facts: [f(`Edge found in ${FUND_TESTS.edges} of ${FUND_TESTS.tests} pre-registered tests on ${FUND_TESTS.tickers} tickers.`),
        f(`${FUND_TESTS.exploratoryOrders} exploratory orders only. Prices: ${FUND_TESTS.prices}.`)],
      interpretation: 'No signal has passed its test, so the fund shows no proven edge. Exploratory orders are labelled as such.',
      actions: [go('Open fund', 'fund')] }
  }
  return null
}

export interface AnswerFact {
  text: string
  source: string
  time: string
  /** Backend answers only: OBS/INF/FCT/DEC by the backend's statement class; null for a sample-backed observation. */
  truth?: Truth | null
  /** Backend answers only: the evidence's provenance through the one freshness mapping. */
  freshness?: Freshness
  freshLabel?: string
}

export interface Answer {
  q: string
  context: string
  facts: AnswerFact[]
  interpretation: string
  confidence: 'High' | 'Moderate' | 'Low' | 'Insufficient'
  actions: { label: string; run: (s: Store) => void }[]
  /** Id of the AGENT_TOOLS entry that answered (hand-off branches, or a backend intent that maps onto one exactly). */
  tool?: string
  /** The facts are the design's worked example, not FLOPX data: pages show the illustrative badge. */
  illustrative?: boolean
  /** Set when the backend answered: one line naming the engine (grammar intent, composed brief). */
  engine?: string
  /** Set when the backend could not be reached and the design's offline answer stands in: the reason. */
  offline?: string
}

/** Source line for the design's worked examples, so no surface can read them as retrieved data. */
const ILLUSTRATIVE_SRC = 'Illustrative example, not FLOPX data'
const illus = (text: string) => ({ text, source: ILLUSTRATIVE_SRC, time: 'n/a' })


export function answer(q: string, s: Store, ctx = ''): Answer {
  const t = q.toLowerCase()
  const regionCtx = s.inspector?.kind === 'region' ? s.inspector.id : s.xray.region
  if ((t.includes('h100') || t.includes('quote') || t.includes('premium')) && (t.includes('high') || t.includes('why') || t.includes('here'))) {
    return {
      q, context: ctx, confidence: 'Moderate', illustrative: true,
      facts: [
        illus('Texas median H100 SXM on-demand quote is $2.71/GPU-hr, +11.8% over 30 days.'),
        illus('Global median is $2.31/GPU-hr, so Texas trades at a +17% premium.'),
        illus('Sold-out rate for 8×H100 nodes rose from 18% to 41%.'),
        illus('Red Plains Ph.2 first energisation moved from Q2 to Q4 2026.'),
      ],
      interpretation: 'Illustrative worked example from the design, not a FLOPX reading. The most plausible explanation is that supply is arriving later than announced while training demand concentrates in DFW. FLOPX infers Texas compute pressure at 82 ± 6 (acute). Utilisation is not directly observed — this is inferred from price and availability, and one input (lab demand) rests on a single unconfirmed report.',
      actions: [
        { label: 'Open Texas in X-Ray', run: (st) => { st.setXray({ region: 'us-tx', tab: 'brief' }); st.navigate('xray') } },
        { label: 'Inspect Red Plains Ph.2', run: (st) => st.inspect({ kind: 'project', id: 'red-plains', label: 'Red Plains Campus Ph.2' }) },
        { label: 'Compare TX vs Nordics', run: (st) => { st.setXray({ compare: ['us-tx', 'no-nordics'], compareOpen: true }); st.navigate('xray') } },
      ],
    }
  }
  if ((t.includes('texas') || t.includes('ercot')) && (t.includes('stock') || t.includes('expos') || t.includes('compan'))) {
    return {
      q, context: ctx, confidence: 'Moderate', illustrative: true,
      facts: [
        illus('VST: generation fleet concentrated in ERCOT.'),
        illus('CRWV: leases 2 campuses in DFW metro.'),
        illus('APLD: developer of Red Plains campus (slipped).'),
        illus('NRG: Texas retail + generation.'),
      ],
      interpretation: 'Illustrative worked example from the design, not a FLOPX reading. Documented ties are facts about where companies operate. Separately, FLOPX estimates VST (β 0.42, R² 0.18) and NRG (β 0.27) returns have historically co-moved with Texas pressure — an association, not proof that pressure causes returns. Generators may benefit from scarcity; developers like APLD bear delivery risk from the same constraint.',
      actions: [
        { label: 'Research VST', run: (st) => st.navigate('securities', { t: 'VST' }) },
        { label: 'See fund exposure to TX', run: (st) => { st.setXray({ region: 'us-tx', tab: 'exposure' }); st.navigate('xray') } },
      ],
    }
  }
  if (t.includes('fund') && (t.includes('fall') || t.includes('drop') || t.includes('rental'))) {
    const betas: Record<string, number> = { CRWV: 0.55, NBIS: 0.4, IREN: 0.6, VST: 0.08, DLR: 0.1, EQIX: 0.06, NRG: 0.05 }
    const rows = s.positions.map((p) => ({ t: p.ticker, mv: p.qty * lastPx(p.ticker), b: betas[p.ticker] ?? 0 })).filter((r) => r.b)
    const impact = rows.reduce((a, r) => a + r.mv * r.b * -0.1, 0)
    return {
      q, context: ctx, confidence: 'Low', illustrative: true,
      facts: rows.map((r) => illus(`${r.t}: paper MV ${r.mv >= 0 ? '' : '−'}$${Math.abs(Math.round(r.mv)).toLocaleString()} · modelled β to H100 index ${r.b}`)),
      interpretation: `Illustrative: the betas are the design's example sensitivities, not fitted by FLOPX. A 10% fall in the H100 rental index maps, via modelled sensitivities, to an estimated ${impact >= 0 ? '+' : '−'}$${Math.abs(Math.round(impact)).toLocaleString()} paper P&L (${((impact / s.nav) * 100).toFixed(2)}% NAV). The NBIS short partly offsets CRWV. Sensitivities have wide error bars (R² ≤ 0.12), so treat this as a rough scenario, not a forecast.`,
      actions: [
        { label: 'Open fund positions', run: (st) => { st.setFund({ tab: 'portfolio' }); st.navigate('fund') } },
        { label: 'Review CRWV proposal', run: (st) => { st.setFund({ tab: 'decisions' }); st.navigate('fund') } },
      ],
    }
  }
  const hand = handoffAnswer(q, t, ctx)
  if (hand) return hand
  if (t.includes('rent') || t.includes('workload') || t.includes('train') || t.includes('which compute')) {
    const best = OFFERS.filter((o) => o.gpu === 'h100' && o.interconnect === 'InfiniBand 3.2T' && o.available !== 'Sold out').sort((a, b) => a.price - b.price).slice(0, 3)
    return {
      q, context: ctx, confidence: 'Moderate',
      facts: best.map((o) => ({ text: `${providerById(o.provider)!.name} · ${regionById(o.region)!.name} · ${o.contract}: $${o.price.toFixed(2)}/GPU-hr (${o.available})`, source: 'Provider offer', time: `${o.observed} UTC` })),
      interpretation: 'For multi-node training, InfiniBand clusters matter more than headline price. These are the cheapest matching offers on screen now. FLOPX does not time purchases yet (the forecast needs more history), so waiting is not a modelled saving. Tell me GPU count, duration and deadline for a costed plan.',
      actions: [{ label: 'Open Procurement Desk', run: (st) => st.navigate('procurement') }, { label: 'Compare offers', run: (st) => st.navigate('markets') }],
    }
  }
  if (regionCtx && (t.includes('why') || t.includes('pressure') || t.includes('region') || t.includes('here'))) {
    const r = regionById(regionCtx)!
    const p = pressureAt(r, NOW_IDX)
    if (p == null) return { q, context: ctx, confidence: 'Insufficient', facts: [{ text: `${r.opMW} MW of facilities reported in ${r.name}.`, source: 'FLOPX region view', time: AS_OF }], interpretation: `There are no usable rental quotes or market data for ${r.name}, so FLOPX does not estimate pressure there. I won't guess from neighbouring regions.`, actions: [] }
    return {
      q, context: ctx, confidence: r.quality === 'good' ? 'Moderate' : 'Low',
      facts: [
        { text: `Median H100 quote $${r.h100.toFixed(2)}/GPU-hr (${r.premium > 0 ? '+' : ''}${r.premium}% vs global).`, source: 'FLOPX region view', time: r.quality === 'stale' ? `${AS_OF} (stale)` : AS_OF },
        { text: `${r.opMW.toLocaleString()} MW operational, ${r.conMW.toLocaleString()} MW under construction.`, source: 'FLOPX region view', time: AS_OF },
        ...(r.power ? [{ text: `Day-ahead power $${r.power}/MWh (30d avg).`, source: `${r.grid.toUpperCase()} market data`, time: AS_OF }] : []),
      ],
      interpretation: `FLOPX infers pressure of ${p.toFixed(0)} (${pressureWord(p)}). Largest contributor: ${r.contrib[0]?.factor ?? '—'}. ${r.note ?? ''}`,
      actions: [{ label: `Open ${r.short} in X-Ray`, run: (st) => { st.setXray({ region: r.id }); st.navigate('xray') } }],
    }
  }
  return {
    q, context: ctx, confidence: 'Insufficient',
    facts: [],
    interpretation: 'I could not find enough evidence in FLOPX sources to answer that confidently, so I won’t speculate. Try asking about a region, GPU, company or the fund — or open an entity first so I can use it as context.',
    actions: [{ label: 'Search entities', run: (st) => st.setPalette(true) }],
  }
}

// ---------------- The backend path ----------------

/** A design destination as a store action: the X-Ray takes a state patch first, every other page its params. */
export function runDestination(d: Destination): (st: Store) => void {
  return (st) => {
    if (d.page === 'xray' && d.xray) st.setXray(d.xray)
    st.navigate(d.page, d.params ?? {})
  }
}

export function toAnswer(live: LiveAnswer, q: string, ctx: string): Answer {
  return {
    q,
    context: ctx,
    facts: live.facts.map((f) => ({ text: f.text, source: f.source, time: f.time, truth: f.truth, freshness: f.freshness, freshLabel: f.freshLabel })),
    interpretation: live.interpretation,
    confidence: live.confidence,
    actions: live.destinations.map((d) => ({ label: d.label, run: runDestination(d) })),
    tool: live.tool,
    engine: live.engine,
  }
}

/** The design's canned answer, standing in while the backend is unreachable: always badged illustrative. */
export function offlineAnswer(q: string, s: Store, ctx: string, reason: string): Answer {
  const a = answer(q, s, ctx)
  return {
    ...a,
    illustrative: true,
    offline: reason,
    interpretation: a.facts.length ? `Offline: the FLOPX backend did not answer (${reason}), so this is the design's worked example, not a FLOPX reading. ${a.interpretation}` : a.interpretation,
  }
}

export interface AskOpts {
  /** Injected in tests: the POST /flopx/ask call. */
  post?: typeof askLive
}

/**
 * Ask FLOPX: POST /flopx/ask for the interpretation and routes; when the grammar composed a brief its
 * sentences are the facts. A route-only answer that still names one target (a region, ticker or GPU)
 * fetches that target's brief with a second call, so a question like "why are H100 quotes higher"
 * carries the H100's evidence rows rather than only a screen. Falls back to the design's offline
 * answer, badged illustrative, when the backend does not answer.
 */
export async function askFlopx(q: string, s: Store, ctx = '', opts: AskOpts = {}): Promise<Answer> {
  const post = opts.post ?? askLive
  const first = await post(q)
  if (!first.ok) return offlineAnswer(q, s, ctx, first.error)
  let live = adaptAsk(first.data)
  if (live.kind === 'route' && live.target) {
    const second = await post(briefQueryFor(live.target))
    if (second.ok) live = mergeTargetBrief(live, second.data)
  }
  return toAnswer(live, q, ctx)
}

/**
 * Prepared questions for the Agent Desk. Each names one target in the shape the backend grammar
 * answers with a composed brief (region, company or GPU); src/live/adapt-w4.test.ts holds the recorded
 * payload for every one. The hand-off questions (tokens, Brev, timing gate, scarcity, power, prediction
 * calibration, news themes, fund edge) were dropped: the backend has no composed answer for them.
 */
export const AGENT_SUGGESTED = [
  'What is happening in Texas?',
  'Tell me about the H100',
  'How is CoreWeave doing?',
  'How is Nvidia doing?',
  'What is the state of Virginia?',
  'Tell me about the H200',
  'How is Nebius doing?',
  'What is happening in Frankfurt?',
]

export const SUGGESTED = [
  'Why are H100 quotes higher here?',
  'Which compute-related stocks are exposed to Texas power constraints?',
  'What happens to the fund if rental prices fall?',
  'Which compute should I rent for this workload?',
]

