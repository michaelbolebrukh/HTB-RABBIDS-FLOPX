import { gpuById, NOW_IDX, rng } from './core'
import { PAPER_START_WK, secByTicker, SECURITIES, WEEKS } from './finance'
import { daysAgo, fmtDMY, lastTradingClose, weekdaysAgo } from '@/lib/designDates'

// The design's signals were "updated 14 Mar 2026 08:00 UTC", its own now. They are illustrative
// worked examples, so the stamp slides onto the last completed trading close before the real
// anchor (Fri 06 Nov 2026 21:00 UTC on a Sunday morning), never a clock time ahead of now (WF4 #12).
const SIGNAL_UPDATED = `${fmtDMY(lastTradingClose())} 21:00 UTC (design example)`

/** The basis note every signal carries until the FLOPX evaluation (or forecast refusal) lands. */
export const AWAITING_EVALUATION = 'awaiting the FLOPX evaluation'

export type HorizonId = '1W' | '1M' | '3M'
export type ValidationStatus = 'validated' | 'exploratory' | 'insufficient' | 'no-edge'

export interface HorizonForecast {
  mean: number
  p10: number
  p90: number
  hit: number | null
  n: number
  ic: number | null
  status: ValidationStatus
  baselineHit: number | null
}

export interface Feature {
  name: string
  value: string
  contribution: number // signed, in pp of expected return
  truth: 'obs' | 'inf'
  region?: string
}

export interface Signal {
  id: string
  name: string
  asset: string
  assetLabel: string
  cluster: string
  direction: 'Long' | 'Short' | 'No position'
  summary: string
  horizons: Record<HorizonId, HorizonForecast>
  features: Feature[]
  fundamentals: { k: string; v: string }[]
  news: { text: string; sources: string[] }
  pm: string
  scenario: { name: string; prob: number; text: string }[]
  support: string[]
  contra: string[]
  regions: string[]
  updated: string
  /**
   * Where `horizons[*].status` comes from. 'pending' is the module-init state (WF4 #6): every
   * horizon reads insufficient with hit, IC and n cleared, basisNote AWAITING_EVALUATION, so a
   * blocked or slow backend leaves the honest state rather than the design's claims. 'backend'
   * means src/live/adapt-w2.ts rewrote the statuses from the FLOPX evaluation verdicts or the
   * forecast refusal. 'design' is the design's own table, kept only in DESIGN_HORIZONS for
   * reference; no page renders it. Pages must not print a validation claim ("Validated at 1M")
   * from anything but a backend-basis signal whose status is validated.
   */
  basis?: 'design' | 'pending' | 'backend'
  /** One line on the backend basis, e.g. "FLOPX refuses to forecast: 4 of 60 daily points". */
  basisNote?: string
}

const hf =(mean: number, p10: number, p90: number, hit: number | null, n: number, ic: number | null, status: ValidationStatus, baselineHit: number | null): HorizonForecast => ({ mean, p10, p90, hit, n, ic, status, baselineHit })

export const SIGNALS: Signal[] = [
  {
    id: 'sig-ercot', name: 'ERCOT power for AI', asset: 'VST', assetLabel: 'Vistra (VST)', cluster: 'Power for AI', direction: 'Long', regions: ['us-tx'], updated: SIGNAL_UPDATED,
    summary: 'Texas inferred pressure jumped +14 pts while capacity energisation slipped. Historically, TX pressure shocks preceded VST outperformance vs the utilities sector over 1M.',
    horizons: {
      '1W': hf(0.4, -4.6, 5.3, 0.52, 98, 0.03, 'no-edge', 0.51),
      '1M': hf(2.1, -6.8, 10.4, 0.58, 26, 0.11, 'validated', 0.52),
      '3M': hf(4.0, -11.2, 19.0, 0.62, 8, 0.14, 'insufficient', 0.55),
    },
    features: [
      { name: 'TX Compute Pressure Index Δ6w', value: '+14.0 pts', contribution: 1.1, truth: 'inf', region: 'us-tx' },
      { name: 'ERCOT scarcity hours (30d)', value: '37 h', contribution: 0.6, truth: 'obs', region: 'us-tx' },
      { name: 'TX pipeline slippage (MW-quarters)', value: '1,800', contribution: 0.5, truth: 'inf', region: 'us-tx' },
      { name: 'US-Central H100 premium', value: '+17%', contribution: 0.3, truth: 'obs', region: 'us-tx' },
      { name: 'VST 3M momentum (market baseline)', value: '+9.2%', contribution: 0.2, truth: 'obs' },
      { name: 'Forward P/E vs sector', value: '1.4×', contribution: -0.6, truth: 'obs' },
    ],
    fundamentals: [{ k: 'Fwd P/E', v: '24.1' }, { k: 'EV/EBITDA', v: '13.8' }, { k: 'FCF yield', v: '4.2%' }, { k: 'ERCOT share of gen.', v: '~60% (reported)' }],
    news: { text: 'ERCOT large-load consultation (confirmed) adds curtailment requirements — mixed for generators: higher scarcity value, slower load growth.', sources: ['ERCOT notice 11 Mar', 'Developer call 24 Feb'] },
    pm: 'Kalshi-style EEA contract at 31% (+6 pts 7d). FLOPX uses it as a weak, thin-liquidity input — not as a fact.',
    scenario: [
      { name: 'Base', prob: 0.55, text: 'Pipeline slips persist through Q3; summer scarcity pricing supports generator margins.' },
      { name: 'Bear', prob: 0.25, text: 'ERCOT curtailment rule passes quickly; new load is interruptible, damping scarcity value.' },
      { name: 'Bull', prob: 0.2, text: 'Hot summer + delayed campuses: repeated EEA events and forward curve repricing.' },
    ],
    support: ['5 independent observation types agree on TX tightening', 'Documented ERCOT fleet concentration'],
    contra: ['Marketplace listings in TX grew 6% (mostly consumer GPUs)', 'VST already trades at premium to sector', 'Curtailment rule could cap upside'],
  },
  {
    id: 'sig-nova', name: 'NoVA powered-shell scarcity', asset: 'DLR', assetLabel: 'Digital Realty (DLR)', cluster: 'Data-centre landlords', direction: 'Long', regions: ['us-va'], updated: SIGNAL_UPDATED,
    summary: 'Northern Virginia pressure remains structurally high; transmission limits cap effective supply additions through 2027.',
    horizons: {
      '1W': hf(0.2, -3.1, 3.4, 0.5, 98, 0.01, 'no-edge', 0.5),
      '1M': hf(1.2, -4.9, 7.0, 0.56, 26, 0.08, 'validated', 0.53),
      '3M': hf(3.1, -8.0, 13.5, 0.6, 8, 0.12, 'insufficient', 0.57),
    },
    features: [
      { name: 'NoVA Compute Pressure Index', value: '78', contribution: 0.7, truth: 'inf', region: 'us-va' },
      { name: 'Dominion zone constraint', value: 'Binding', contribution: 0.4, truth: 'obs', region: 'us-va' },
      { name: 'Reported pre-leasing', value: '92%', contribution: 0.3, truth: 'obs' },
      { name: 'REIT rate sensitivity (10y)', value: '−0.8 β', contribution: -0.2, truth: 'obs' },
    ],
    fundamentals: [{ k: 'P/AFFO', v: '27.4' }, { k: 'Occupancy', v: '84%' }, { k: 'Net debt/EBITDA', v: '6.1×' }],
    news: { text: 'Record pre-leasing in NoVA (confirmed filing).', sources: ['Q4-25 supplemental'] },
    pm: 'Federal moratorium contract at 7% — thin liquidity; not used.',
    scenario: [{ name: 'Base', prob: 0.6, text: 'Rents reprice upward as leases roll.' }, { name: 'Bear', prob: 0.25, text: 'Rate shock dominates REIT valuations.' }, { name: 'Bull', prob: 0.15, text: 'Transmission delays extend scarcity beyond 2028.' }],
    support: ['Long-run structural constraint'], contra: ['Rate sensitivity dominates weekly moves'],
  },
  {
    id: 'sig-neo', name: 'Neocloud rental softening', asset: 'b-neo', assetLabel: 'Neocloud basket', cluster: 'Neoclouds', direction: 'Short', regions: ['no-nordics', 'de-fra', 'us-tx'], updated: SIGNAL_UPDATED,
    summary: 'Outside US-Central, H100/H200 rental indices are falling; neocloud valuations imply flat pricing. Exploratory — fewer than 12 independent windows.',
    horizons: {
      '1W': hf(-0.3, -9.0, 8.2, 0.49, 60, -0.01, 'no-edge', 0.5),
      '1M': hf(-3.4, -18.0, 12.0, 0.6, 10, 0.09, 'exploratory', 0.5),
      '3M': hf(-6.5, -30.0, 20.0, null, 3, null, 'insufficient', null),
    },
    features: [
      { name: 'H100 index Δ ex-US-Central (3M)', value: '−7.9%', contribution: -1.6, truth: 'obs' },
      { name: 'Nordic pressure', value: '41 (falling)', contribution: -0.8, truth: 'inf', region: 'no-nordics' },
      { name: 'TX pressure (offsets)', value: '82', contribution: 0.9, truth: 'inf', region: 'us-tx' },
      { name: 'Short interest (CRWV)', value: '9.8%', contribution: 0.4, truth: 'obs' },
    ],
    fundamentals: [{ k: 'Basket EV/Sales', v: '16.4' }, { k: 'Backlog/Rev', v: '4.1×' }],
    news: { text: 'New $2.1B debt facility (8-K) increases fixed costs; rental softening in EU.', sources: ['8-K 12 Mar', 'Marketplace data 13 Mar'] },
    pm: 'H100 > $2.50 contract at 24% — consistent with softening; thin liquidity.',
    scenario: [{ name: 'Base', prob: 0.5, text: 'Gradual price decline, offset by TX tightness.' }, { name: 'Bear (for short)', prob: 0.35, text: 'Large contract announcement re-rates basket.' }, { name: 'Bull (for short)', prob: 0.15, text: 'B200 oversupply drags all rental prices.' }],
    support: ['Observed rental index declines in 3 regions'], contra: ['High short interest → squeeze risk', 'Only 10 independent 1M windows', 'TX pressure offsets'],
  },
  {
    id: 'sig-h100', name: 'H100 rental index forecast', asset: 'H100-IDX', assetLabel: 'H100 rental index ($/GPU-hr)', cluster: 'Compute prices', direction: 'No position', regions: [], updated: SIGNAL_UPDATED,
    summary: 'Not tradable as equity. Used by Procurement and as a feature in equity signals. Forecasts index level change.',
    horizons: {
      '1W': hf(0.3, -2.0, 2.6, 0.61, 104, 0.21, 'validated', 0.55),
      '1M': hf(-1.2, -6.5, 4.4, 0.64, 26, 0.24, 'validated', 0.54),
      '3M': hf(-4.0, -13.0, 5.5, 0.66, 8, null, 'insufficient', 0.6),
    },
    features: [
      { name: 'Global CPI', value: '66.8', contribution: 0.4, truth: 'inf' },
      { name: 'B200 deliveries (est.)', value: '↑', contribution: -1.2, truth: 'inf' },
      { name: 'Marketplace sold-out rate', value: '24%', contribution: 0.3, truth: 'obs' },
    ],
    fundamentals: [], news: { text: 'EU spot below $2/hr on marketplaces.', sources: ['FLOPX observed 13 Mar'] }, pm: 'Contract (different index definition) at 24% for >$2.50 by June.',
    scenario: [{ name: 'Base', prob: 0.6, text: 'Gentle decline as B200 displaces H100.' }, { name: 'Tight', prob: 0.25, text: 'US-Central scarcity spills over.' }, { name: 'Glut', prob: 0.15, text: 'Rapid decline below $1.80.' }],
    support: [], contra: ['Regime change risk as Blackwell ramps'],
  },
  {
    id: 'sig-nordic', name: 'Nordic loosening → Nebius', asset: 'NBIS', assetLabel: 'Nebius (NBIS)', cluster: 'Neoclouds', direction: 'Short', regions: ['no-nordics'], updated: SIGNAL_UPDATED,
    summary: 'Exploratory: only 7 months of reliable Nordic quote data. Displayed for research; position size capped at 25% of normal.',
    horizons: {
      '1W': hf(-0.2, -10, 9.5, null, 30, null, 'insufficient', null),
      '1M': hf(-1.8, -20, 16, null, 7, null, 'insufficient', null),
      '3M': hf(-3.0, -32, 26, null, 2, null, 'insufficient', null),
    },
    features: [{ name: 'Nordic pressure', value: '41', contribution: -0.9, truth: 'inf', region: 'no-nordics' }, { name: 'Nordic H100 Δ30d', value: '−6.4%', contribution: -0.6, truth: 'obs', region: 'no-nordics' }],
    fundamentals: [{ k: 'Rev growth', v: '310%' }], news: { text: 'Fjordlys Hall 3 on schedule.', sources: ['Company update'] }, pm: 'None relevant.',
    scenario: [{ name: 'Base', prob: 0.5, text: 'Unknown — insufficient history.' }], support: ['Direction consistent with rental data'], contra: ['Insufficient history', 'Company growth dominates'],
  },
  {
    id: 'sig-chips', name: 'Compute pressure → NVIDIA', asset: 'NVDA', assetLabel: 'NVIDIA (NVDA)', cluster: 'Accelerator supply chain', direction: 'No position', regions: [], updated: SIGNAL_UPDATED,
    summary: 'Tested and rejected: adding compute-market features does not improve NVDA return forecasts over the market/fundamental baseline at any horizon.',
    horizons: {
      '1W': hf(0.0, -6, 6, 0.5, 104, 0.0, 'no-edge', 0.51),
      '1M': hf(0.1, -12, 12, 0.49, 26, -0.02, 'no-edge', 0.53),
      '3M': hf(0.0, -20, 21, 0.5, 8, null, 'no-edge', 0.56),
    },
    features: [{ name: 'Global CPI', value: '66.8', contribution: 0.0, truth: 'inf' }], fundamentals: [{ k: 'Fwd P/E', v: '38' }], news: { text: '—', sources: [] }, pm: '—',
    scenario: [], support: [], contra: ['Baseline hit 53% vs 49% with compute features at 1M'],
  },
]
export const signalById = (id: string) => SIGNALS.find((s) => s.id === id)

/**
 * The design's own validation table, kept for reference and tests only. At module init every
 * signal is put into the honest pending state (WF4 #6): the forecast distribution (mean, p10, p90)
 * stays as the illustrative worked example, but status is 'insufficient' and hit, IC and n are
 * cleared, because no evaluation has been seen yet. src/live/adapt-w2.ts replaces the statuses
 * when /flopx/research/evaluation or the forecast call answers; a timeout leaves this state.
 */
export const DESIGN_HORIZONS: ReadonlyMap<string, Signal['horizons']> = new Map(SIGNALS.map((s) => [s.id, structuredClone(s.horizons)]))

/** Put one signal into the pending state. Pure apart from `s`. */
export function resetSignalToPending(s: Signal): void {
  for (const h of Object.keys(s.horizons) as HorizonId[]) {
    const f = s.horizons[h]
    s.horizons[h] = { mean: f.mean, p10: f.p10, p90: f.p90, hit: null, n: 0, ic: null, status: 'insufficient', baselineHit: null }
  }
  s.basis = 'pending'
  s.basisNote = AWAITING_EVALUATION
}
SIGNALS.forEach(resetSignalToPending)

/**
 * Supporting-evidence lines for a signal: the design's non-validation evidence, plus one line
 * derived from the 1M status when, and only when, the FLOPX evaluation granted validation. No
 * surface prints "Validated at 1M" from anything else (WF4 #3).
 */
export function signalSupport(s: Signal): string[] {
  const h = s.horizons['1M']
  if (s.basis === 'backend' && h.status === 'validated') return [...s.support, `Validated at 1M by the FLOPX evaluation (n = ${h.n})`]
  return s.support
}

// ---------- Portfolio ----------
export const NAV0 = 10_000_000
export interface Position {
  ticker: string
  qty: number
  avg: number
  strategy: 'CPX-1' | 'User'
  signal?: string
  realised: number
}

const DRIFT: Record<string, number> = { VST: 0.031, CEG: 0.012, NRG: -0.008, DLR: 0.006, EQIX: -0.011, CRWV: -0.042, ORCL: 0.004, MSFT: 0.009, NBIS: 0.018 }
const W: [string, number, string | undefined][] = [
  ['VST', 0.09, 'sig-ercot'], ['CEG', 0.06, undefined], ['NRG', 0.05, 'sig-ercot'], ['DLR', 0.08, 'sig-nova'], ['EQIX', 0.05, 'sig-nova'],
  ['CRWV', 0.05, 'sig-neo'], ['ORCL', 0.04, undefined], ['MSFT', 0.06, undefined], ['NBIS', -0.03, 'sig-neo'],
]
export const INITIAL_POSITIONS: Position[] = W.map(([t, w, s]) => ({
  ticker: t,
  qty: Math.round((w * NAV0) / secByTicker(t)!.px[secByTicker(t)!.px.length - 1]),
  // Cost basis kept near current prices so the paper period shows modest, non-heroic P&L.
  avg: +(secByTicker(t)!.px[secByTicker(t)!.px.length - 1] * (1 - DRIFT[t])).toFixed(2),
  strategy: t === 'MSFT' ? 'User' : 'CPX-1',
  signal: s,
  realised: 0,
}))
export const INITIAL_CASH = NAV0 - INITIAL_POSITIONS.reduce((s, p) => s + p.qty * p.avg, 0) + 41_250 // + realised from closed trades
export const lastPx = (t: string) => {
  const s = secByTicker(t)
  return s ? s.px[s.px.length - 1] : 0
}
export const prevPx = (t: string) => {
  const s = secByTicker(t)
  return s ? s.px[s.px.length - 2] : 0
}

export function navOf(positions: Position[], cash: number) {
  return cash + positions.reduce((s, p) => s + p.qty * lastPx(p.ticker), 0)
}

// Equity curve: weeks 0–103 hypothetical backtest; 104+ paper.
const NAV_NOW = navOf(INITIAL_POSITIONS, INITIAL_CASH)
export const EQUITY: number[] = (() => {
  const r = rng(777)
  const out: number[] = []
  let v = NAV0 * 0.88
  for (let i = 0; i < PAPER_START_WK; i++) {
    const shock = i === 86 ? -0.045 : i === 87 ? -0.03 : i === 88 ? -0.02 : 0
    v *= 1 + 0.0013 + (r() - 0.5) * 0.024 + shock
    out.push(v)
  }
  // rescale backtest to end at NAV0
  const k = NAV0 / out[out.length - 1]
  for (let i = 0; i < out.length; i++) out[i] = Math.round(out[i] * k)
  const n = WEEKS.length - PAPER_START_WK
  for (let j = 0; j < n; j++) {
    const t = (j + 1) / n
    out.push(Math.round(NAV0 + (NAV_NOW - NAV0) * t + (j === n - 1 ? 0 : (r() - 0.5) * 60000)))
  }
  return out
})()
export const BENCH = ['SOX', 'SPX'].map((t) => {
  const px = secByTicker(t)!.px
  return { t, values: px.map((p) => Math.round((p / px[0]) * EQUITY[0])) }
})
export const DRAWDOWN = (() => {
  let peak = -Infinity
  return EQUITY.map((v) => {
    peak = Math.max(peak, v)
    return +(((v - peak) / peak) * 100).toFixed(2)
  })
})()

// ---------- Decision records (annotations) ----------
export interface DecisionRecord {
  id: string
  week: number
  mode: 'backtest' | 'paper'
  title: string
  information: { label: string; value: string; truth: 'obs' | 'inf' }[]
  prediction: { asset: string; horizon: HorizonId; mean: number; p10: number; p90: number; status: ValidationStatus }
  decision: { action: string; size: string; by: string; reasoning: string }
  outcome: { realised: number | null; inBand: boolean | null; text: string; attribution: string }
}

export const DECISIONS: DecisionRecord[] = [
  {
    id: 'd1', week: 30, mode: 'backtest', title: 'Added DLR on NoVA tightening',
    information: [{ label: 'NoVA CPI', value: '73 → 76', truth: 'inf' }, { label: 'Dominion constraint', value: 'Binding', truth: 'obs' }, { label: 'DLR 1M momentum', value: '+2.1%', truth: 'obs' }],
    prediction: { asset: 'DLR', horizon: '1M', mean: 1.4, p10: -4.2, p90: 7.1, status: 'validated' },
    decision: { action: 'Buy DLR', size: '+3.0% NAV', by: 'CPX-1 (backtest replay)', reasoning: 'Pressure rising with binding transmission constraint.' },
    outcome: { realised: 3.8, inBand: true, text: 'Realised +3.8% over 1M — within forecast band, above mean.', attribution: 'Compute features contributed est. +0.5 pp of the forecast; realised excess largely market beta.' },
  },
  {
    id: 'd2', week: 66, mode: 'backtest', title: 'Cut CRWV after rental index break',
    information: [{ label: 'H100 index 3M Δ', value: '−9.4%', truth: 'obs' }, { label: 'Neocloud basket IV', value: '92%', truth: 'obs' }],
    prediction: { asset: 'CRWV', horizon: '1M', mean: -2.9, p10: -18, p90: 13, status: 'exploratory' },
    decision: { action: 'Sell CRWV', size: '−2.0% NAV', by: 'CPX-1 (backtest replay)', reasoning: 'Rental softening; exploratory signal → half size.' },
    outcome: { realised: 21.0, inBand: false, text: 'Realised +21% — outside band. Large contract announcement not anticipated.', attribution: 'Failed prediction. News shock dominated; logged to Research → Failures.' },
  },
  {
    id: 'd3', week: 87, mode: 'backtest', title: 'Drawdown: ERCOT signal wrong',
    information: [{ label: 'TX CPI', value: '61 (flat)', truth: 'inf' }, { label: 'ERCOT forwards', value: 'Mild summer priced', truth: 'obs' }],
    prediction: { asset: 'VST', horizon: '1M', mean: 1.8, p10: -6, p90: 9.5, status: 'validated' },
    decision: { action: 'Hold VST 8%', size: 'No change', by: 'CPX-1 (backtest replay)', reasoning: 'Signal within normal range.' },
    outcome: { realised: -11.4, inBand: false, text: 'Realised −11.4% — below p10. Sector-wide sell-off on rate repricing.', attribution: 'Model had no rate-shock feature. Max drawdown −9.1% for strategy.' },
  },
  {
    id: 'd4', week: 104, mode: 'paper', title: 'Paper trading start: initial allocation',
    information: [{ label: 'Global CPI', value: '64.9', truth: 'inf' }, { label: 'Signals active', value: '4 (design example)', truth: 'obs' }],
    prediction: { asset: 'Portfolio', horizon: '1M', mean: 0.6, p10: -3.9, p90: 4.8, status: 'validated' },
    decision: { action: 'Allocate 9 positions', size: '56% gross', by: 'CPX-1 + user (MSFT)', reasoning: 'Initial targets from signal weights with 4% risk budget.' },
    outcome: { realised: null, inBand: null, text: 'Ongoing paper period.', attribution: '—' },
  },
  {
    id: 'd5', week: 112, mode: 'paper', title: 'Raised VST on Texas pressure jump',
    information: [{ label: 'TX CPI', value: '68 → 82', truth: 'inf' }, { label: 'Red Plains Ph.2', value: 'Slipped to Q4', truth: 'obs' }, { label: 'ERCOT scarcity hrs', value: '37', truth: 'obs' }],
    prediction: { asset: 'VST', horizon: '1M', mean: 2.1, p10: -6.8, p90: 10.4, status: 'validated' },
    decision: { action: 'Buy VST (paper fill)', size: '+1.0% NAV', by: 'CPX-1, approved by user', reasoning: '1M signal strengthened by anomaly (design example; the signal status shown is the live evaluation).' },
    outcome: { realised: null, inBand: null, text: `Horizon not yet elapsed (resolves ${fmtDMY(daysAgo(-30, weekdaysAgo(1)))}).`, attribution: 'Pending' },
  },
]

// ---------- Proposals ----------
export interface Proposal {
  id: string
  ticker: string
  side: 'Buy' | 'Sell' | 'Short' | 'Cover'
  qty: number
  currentW: number
  targetW: number
  riskUse: number // % of risk budget consumed after trade
  signal: string
  reasoning: string
  risks: string[]
  exploratory?: boolean
}
// ---------- Price-chart series (illustrative, derived from EQUITY) ----------
export interface Candle { o: number; h: number; l: number; c: number }
/** Weekly NAV candles: open = previous close; high/low widened by a seeded intraweek range. */
export const EQUITY_OHLC: Candle[] = (() => {
  const r = rng(913)
  return EQUITY.map((c, i) => {
    const o = i ? EQUITY[i - 1] : Math.round(c * 0.996)
    const span = Math.abs(c - o) * 0.35 + c * (0.002 + r() * 0.005)
    return { o, c, h: Math.round(Math.max(o, c) + span * r()), l: Math.round(Math.min(o, c) - span * r()) }
  })
})()
/** Weekly traded notional ($): ~340%/yr turnover, with rebalances spiking on decision weeks. */
export const VOLUME: number[] = (() => {
  const r = rng(4242)
  const spike = new Set(DECISIONS.map((d) => d.week))
  return EQUITY.map((nav, i) => Math.round(nav * (0.035 + r() * 0.06) * (spike.has(i) ? 3 + r() * 1.5 : 1)))
})()

const qtyFor = (t: string, w: number) => Math.round((w * NAV0) / lastPx(t))
export const INITIAL_PROPOSALS: Proposal[] = [
  { id: 'p1', ticker: 'VST', side: 'Buy', qty: qtyFor('VST', 0.015), currentW: 9.3, targetW: 10.8, riskUse: 71, signal: 'sig-ercot', reasoning: 'TX pressure anomaly (+14 pts) and ERCOT scarcity strengthen the 1M signal, whose validation status below is the FLOPX evaluation\'s. Target raised 1.5 pp; capped by single-name limit of 12%.', risks: ['ERCOT curtailment rule (consultation open)', 'Already premium valuation', 'Concentration: 3 TX power names = 15.8% NAV'] },
  { id: 'p2', ticker: 'CRWV', side: 'Sell', qty: qtyFor('CRWV', 0.02), currentW: 5.2, targetW: 3.2, riskUse: 64, signal: 'sig-neo', reasoning: 'Rental softening outside US-Central; delivery-risk on DFW expansion. Exploratory signal → half-size reduction.', risks: ['High short interest → squeeze', 'Contract announcements can gap stock +20%'], exploratory: true },
  { id: 'p3', ticker: 'DLR', side: 'Buy', qty: qtyFor('DLR', 0.005), currentW: 8.1, targetW: 8.6, riskUse: 66, signal: 'sig-nova', reasoning: 'NoVA scarcity persists; small top-up to target.', risks: ['Rate sensitivity'] },
  { id: 'p4', ticker: 'NBIS', side: 'Short', qty: qtyFor('NBIS', 0.005), currentW: -3.1, targetW: -3.6, riskUse: 67, signal: 'sig-nordic', reasoning: 'Nordic loosening. Insufficient history — capped at 25% of normal size, requires manual approval.', risks: ['Insufficient validation history', 'Growth-driven stock'], exploratory: true },
]

// ---------- Evaluation ----------
export const H100_ACTUAL = gpuById('h100')!.index.slice(0, NOW_IDX + 1)
function fc(h: number, seed: number, bias = 0) {
  const r = rng(seed)
  return H100_ACTUAL.map((v, i) => {
    if (i < h + 2) return null
    const shock = i === 9 || i === 10 ? 0.16 : i === 24 ? -0.07 : 0
    return +(v * (1 + (r() - 0.5) * 0.06 * Math.sqrt(h) + shock * (h > 1 ? 1 : 0.6) + bias)).toFixed(3)
  })
}
export const H100_FC: Record<'1M' | '3M', { base: (number | null)[]; compute: (number | null)[] }> = {
  '1M': { base: fc(1, 5, 0.03), compute: fc(1, 6) },
  '3M': { base: fc(3, 7, 0.05), compute: fc(3, 8) },
}
export const LOW_INFO: [number, number, string][] = [
  [0, 5, 'Sparse coverage: 3 price sources'],
  [17, 18, 'Scraper outage (2 venues)'],
]

export interface ModelRow {
  model: string
  short: string
  mae: number | null
  hit: number | null
  ic: number | null
  sharpe: number | null
  n: number
  ci: string
  /** 'reference' is the backend's verdict for the baseline row: it is the yardstick, not a result. */
  status: ValidationStatus | 'reference'
  /** RMSE skill against the baseline over the same origins (backend rows only). */
  skill?: number | null
}
/**
 * What MODEL_COMPARE currently describes. The design ships H100-index rows in $/hr; once the
 * backend evaluation lands (src/live/adapt-w2.ts) the rows are equity log-return forecasts pooled
 * across the backend's tickers, and this says so.
 */
export const MODEL_COMPARE_META: { basis: 'design' | 'backend'; maeUnit: string; scope: string; horizonDays?: Record<HorizonId, number> } = {
  basis: 'design',
  maeUnit: '$/hr',
  scope: 'H100 index + equity signals',
}
export const MODEL_COMPARE: Record<HorizonId, ModelRow[]> = {
  '1W': [
    { model: 'Market + fundamentals (baseline)', short: 'Baseline', mae: 0.061, hit: 0.55, ic: 0.12, sharpe: 0.31, n: 104, ci: '±0.04', status: 'validated' },
    { model: '+ Compute-market data', short: '+Compute', mae: 0.049, hit: 0.61, ic: 0.21, sharpe: 0.52, n: 104, ci: '±0.04', status: 'validated' },
    { model: '+ AI news/PM assessment', short: '+AI', mae: 0.048, hit: 0.61, ic: 0.22, sharpe: 0.5, n: 52, ci: '±0.06', status: 'exploratory' },
  ],
  '1M': [
    { model: 'Market + fundamentals (baseline)', short: 'Baseline', mae: 0.118, hit: 0.54, ic: 0.09, sharpe: 0.42, n: 26, ci: '±0.08', status: 'validated' },
    { model: '+ Compute-market data', short: '+Compute', mae: 0.087, hit: 0.64, ic: 0.24, sharpe: 0.88, n: 26, ci: '±0.09', status: 'validated' },
    { model: '+ AI news/PM assessment', short: '+AI', mae: 0.083, hit: 0.65, ic: 0.26, sharpe: 0.91, n: 12, ci: '±0.13', status: 'exploratory' },
  ],
  '3M': [
    { model: 'Market + fundamentals (baseline)', short: 'Baseline', mae: 0.21, hit: 0.6, ic: null, sharpe: null, n: 8, ci: '±0.17', status: 'insufficient' },
    { model: '+ Compute-market data', short: '+Compute', mae: 0.16, hit: 0.66, ic: null, sharpe: null, n: 8, ci: '±0.17', status: 'insufficient' },
    { model: '+ AI news/PM assessment', short: '+AI', mae: null, hit: null, ic: null, sharpe: null, n: 4, ci: '—', status: 'insufficient' },
  ],
}
export interface BacktestStats {
  /** 'design' = the founder's illustrative figures; 'backend' = /flopx/research/evaluation/portfolio. */
  basis: 'design' | 'backend'
  grossSharpe: number | null
  netSharpe: number | null
  turnover: number | null
  costBps: number
  costDrag: number | null
  maxDD: number | null
  period: string
  vol: number | null
  annRet: number | null
  hitRate: number | null
  costsUsd?: number | null
  trades?: number | null
}
// maxDD is read off the same DRAWDOWN series the equity chart draws, so the KPI, the chart readout and this panel carry one number.
export const BACKTEST_STATS: BacktestStats = { basis: 'design', grossSharpe: 1.21, netSharpe: 0.88, turnover: 340, costBps: 12, costDrag: 1.4, maxDD: +Math.min(0, ...DRAWDOWN.slice(0, PAPER_START_WK)).toFixed(1), period: 'Jan 2024 – Dec 2025', vol: 9.8, annRet: 6.9, hitRate: 0.56 }

export const FAILURES: { date: string; what: string; why: string; lesson: string }[] = [
  { date: 'Apr 2025', what: 'CRWV 1M forecast −2.9% · realised +21%', why: 'Unanticipated contract announcement (news shock).', lesson: 'Added contract-announcement risk flag; exploratory signals half-size.' },
  { date: 'Sep 2025', what: 'VST 1M forecast +1.8% · realised −11.4%', why: 'Rate repricing across utilities; no macro feature.', lesson: 'Added sector-neutral variant (under evaluation).' },
  { date: 'Oct–Nov 2024', what: 'H100 index 1M forecasts too low (−16%)', why: 'H200 launch shifted demand mix; model treated as supply shock.', lesson: 'GPU-generation substitution feature added.' },
  { date: 'Jun 2025', what: 'No forecast issued for 2 weeks', why: 'Scraper outage on 2 marketplaces — low-information period.', lesson: 'Forecasts withheld rather than issued on stale data.' },
]

export const ALL_TICKERS = SECURITIES.filter((s) => s.group !== 'Index').map((s) => s.ticker)
