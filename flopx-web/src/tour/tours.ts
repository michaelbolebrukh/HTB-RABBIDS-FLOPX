// The three pitches as beat sequences. A beat names its phase on the shared six-beat spine, the
// real screen it stands on (`run` puts the app there), the real element it spotlights (`target`,
// tried in order against the live DOM) and how long autoplay dwells. What the card says is in
// cards.tsx, keyed by beat id, and every figure there is read from the live store at render time.
import { NOW_IDX } from '@/data/core'
import type { Store } from '@/lib/store'
import type { Side } from './geometry'
import { DEFAULT_DWELL_MS, type BeatMeta, type TourId, type TourMeta } from './model'
import { FALLBACK_REGION_ID, topRegion } from './data'
import { prefersReducedMotion, tween } from './motion'
import { AGENT_QUESTION, AGENT_WAIT_CAP_MS, agentAnswerSettled, prefetchAsk } from './prefetch'

export interface Beat extends BeatMeta {
  /** Put the app on the beat's screen. May return a cleanup (an animation to cancel on leave). */
  run?: (s: Store) => void | (() => void)
  prefer?: Side[]
  /** Card width hint; the stage clamps it to the viewport. */
  width?: number
  /** Autoplay arms this beat's dwell only once `ready()` is true, or after `capMs` (the card then says so). */
  await?: { ready: () => boolean; capMs: number }
}

export interface Tour extends TourMeta {
  beats: Beat[]
}

// ---------- shared targets ----------
const TICKER = [{ sel: 'div[data-chrome].h-7' }]
const panel = (title: string) => ({ sel: 'main h3', text: title, closest: 'section' })
const eyebrow = (text: string, up = 1) => ({ sel: 'main .eyebrow', text, up })
const MAIN = { sel: '#main-scroll' }
/** The live quotes table's column header on Markets: lighting the whole table (439 px at 1440 x 900, at the foot of the page) left no band tall enough for the counters card (N-TOUR2 #1), so the beat lights the header row and the card sits over the dimmed rows below it. */
const QUOTES_HEAD = [{ sel: 'main table thead' }, { sel: 'main table', closest: 'section' }]

const toOverview = (s: Store) => { s.setInspector(null); s.setAssistant((a) => ({ ...a, open: false })); s.navigate('overview') }
const toMarkets = (s: Store) => { s.setInspector(null); s.navigate('markets', { gpu: 'h100' }) }
const toHandoff = (s: Store) => { s.setInspector(null); s.navigate('handoff') }
const toResearch = (s: Store) => { s.setInspector(null); s.navigate('research') }
const toProcurement = (s: Store) => { s.setInspector(null); s.navigate('procurement', { gpu: 'h100', n: '8', weeks: '1' }) }
/** The buyer's beat stands on an H200 job, the GPU the procurement script and the page's own hand-off snapshot name (N-TOUR2 #3). */
export const PLAN_GPU = 'h200'
const toProcurementPlan = (s: Store) => { s.setInspector(null); s.navigate('procurement', { gpu: PLAN_GPU, n: '8', weeks: '1' }) }

/** X-Ray with the tour's region selected (the most factors with data; US East and Midwest when the pressure feed is not in). */
const toXrayFactors = (s: Store) => {
  s.setInspector(null)
  const r = topRegion()
  s.setXray({ layers: ['pressure', 'pipeline'], reveal: 100, t: NOW_IDX, region: r?.id ?? FALLBACK_REGION_ID, project: null, scenario: null, compareOpen: false, tab: 'brief' })
  s.navigate('xray')
}

/** How long the raw layers stand alone before the reveal slider moves (0 under reduced motion: the end state shows at once, WF5 #9). */
export const REVEAL_HOLD_MS = 1400

/** X-Ray map, raw layers first, then the reveal slider moved by the tour from 0 to 100. */
export const toXrayReveal = (s: Store) => {
  s.setInspector(null)
  const reduce = prefersReducedMotion()
  s.setXray({ layers: ['rental', 'power', 'pipeline', 'pressure'], reveal: reduce ? 100 : 0, t: NOW_IDX, region: null, project: null, scenario: null, compareOpen: false, tab: 'brief' })
  s.navigate('xray')
  if (reduce) return () => {}
  let cancel = () => {}
  const timer = setTimeout(() => { cancel = tween(0, 100, 1600, (v) => s.setXray({ reveal: Math.round(v) })) }, REVEAL_HOLD_MS)
  return () => { clearTimeout(timer); cancel() }
}

const toXrayAnomaly = (s: Store) => {
  s.setInspector(null)
  const r = topRegion()
  s.setXray({ reveal: 100, t: NOW_IDX, region: r?.id ?? FALLBACK_REGION_ID, project: null, scenario: null, compareOpen: false, tab: 'brief', layers: ['pressure', 'pipeline'] })
  s.navigate('xray')
}
const toXrayScenario = (s: Store) => {
  const r = topRegion()?.id ?? FALLBACK_REGION_ID
  s.setXray({ region: r, tab: 'whatif', scenario: { region: r, slipMonths: 12, powerShock: 0 }, t: Math.min(38, NOW_IDX + 6), compareOpen: false })
  s.navigate('xray')
  return () => s.setXray({ scenario: null, t: NOW_IDX })
}
const toXrayExposure = (s: Store) => {
  const r = topRegion()?.id ?? FALLBACK_REGION_ID
  s.setXray({ region: r, tab: 'exposure', scenario: null, t: NOW_IDX, compareOpen: false })
  s.navigate('xray')
}

const D = DEFAULT_DWELL_MS

export const TOURS: Record<TourId, Tour> = {
  gr: {
    id: 'gr',
    name: 'G-Research',
    audience: 'Make the invisible visible',
    thesis: 'Nobody publishes GPU utilisation. FLOPX reads what is public, says which class of statement each number is, and shows the inference, its uncertainty and what it refuses to claim.',
    tone: 'inf',
    beats: [
      { id: 'gr-invisible', phase: 'invisible', title: 'The market nobody can see', target: TICKER, duration: D + 3000, run: toOverview, prefer: ['below'], width: 660 },
      { id: 'gr-analysis', phase: 'analysis', title: 'Four classes of statement', target: [{ sel: 'main span.font-serif', text: 'Observed', closest: '.grid' }, { sel: 'main [data-truth-cards]', phone: true }], duration: D + 2000, run: toHandoff, prefer: ['below'], width: 860 },
      { id: 'gr-factors', phase: 'modelling', title: 'Six factors, one score', target: [eyebrow('Top drivers of the inference'), { sel: 'main h2', nth: 0, up: 3 }], duration: D + 3000, run: toXrayFactors, prefer: ['left'], width: 660 },
      { id: 'gr-reveal', phase: 'modelling', title: 'Raw to inferred', target: [{ sel: 'input[aria-label="Raw to inferred"]', up: 1 }, { sel: 'input[aria-label="Observed to inferred"]', up: 1, phone: true }], duration: D + 2000, run: toXrayReveal, prefer: ['right'] },
      { id: 'gr-estimation', phase: 'estimation', title: 'Tested, and honest about it', target: [panel('Does compute data improve predictions?')], duration: D + 3000, run: toResearch, prefer: ['above', 'below'], width: 720 },
      { id: 'gr-anomaly', phase: 'decision', title: 'An anomaly, with its evidence', target: [{ sel: 'main .rounded-md.p-3', text: 'detected', nth: 0 }, { sel: 'main h2', nth: 0, up: 3 }], duration: D, run: toXrayAnomaly, prefer: ['left'] },
      { id: 'gr-scenario', phase: 'decision', title: 'A scenario changes only estimates', target: [eyebrow('Estimated quantity', 1), { sel: 'main h2', nth: 0, up: 3 }], duration: D, run: toXrayScenario, prefer: ['left'] },
      { id: 'gr-exposure', phase: 'decision', title: 'Who is exposed', target: [eyebrow('Documented ties', 1), { sel: 'main h2', nth: 0, up: 3 }], duration: D, run: toXrayExposure, prefer: ['left'] },
      { id: 'gr-plan', phase: 'decision', title: 'And what a buyer would do', target: [panel('Ranked options')], duration: D + 2000, run: toProcurementPlan, prefer: ['left'], width: 560 },
    ],
  },
  hrt: {
    id: 'hrt',
    name: 'HRT',
    audience: 'Best use of data for predictions and decisions',
    thesis: 'Every ask priced, every claim tested against a baseline, and no number that was not observed shown as if it had been. The desk proposes; a person decides; the paper fill is attributed back.',
    tone: 'dec',
    beats: [
      { id: 'hrt-retrieval', phase: 'retrieval', title: 'What FLOPX retrieved tonight', target: QUOTES_HEAD, duration: D + 2000, run: toMarkets, prefer: ['below', 'above'], width: 780 },
      { id: 'hrt-sources', phase: 'retrieval', title: 'Every source, its licence, its last observation', target: [panel('Price distribution')], duration: D + 3000, run: toMarkets, prefer: ['above', 'right'], width: 720 },
      { id: 'hrt-eval', phase: 'estimation', title: 'Edge in 0 of 24 is a result', target: [{ sel: 'main div.rounded-md', text: 'forecast tests', nth: 0 }, { sel: 'main h3', text: 'forecast tests', closest: 'section', phone: true }], duration: D + 2000, run: (s) => { s.setInspector(null); s.setFund({ tab: 'evaluation' }); s.navigate('fund') }, prefer: ['below'], width: 860 },
      { id: 'hrt-forecast', phase: 'estimation', title: 'The forecast refuses when history is thin', target: [{ sel: 'main .rounded-md.border.p-3', text: 'Timing gate' }, panel('Scheduling scenarios')], duration: D + 2000, run: toProcurement, prefer: ['left', 'above'] },
      { id: 'hrt-signal', phase: 'decision', title: 'Signal to proposal', target: [panel('Features driving this forecast')], duration: D, run: (s) => { s.setInspector(null); s.setFund({ tab: 'signals', signal: 'sig-ercot', horizon: '1M' }); s.navigate('fund') }, prefer: ['right', 'left'] },
      { id: 'hrt-send', phase: 'decision', title: 'A person presses Send', target: [{ sel: 'main button', text: 'Send to paper', nth: 0, closest: '.rounded-md' }, { sel: 'main .rounded-md.border.bg-panel', nth: 1 }], duration: D + 3000, run: (s) => { s.setInspector(null); s.setFund({ tab: 'decisions' }); s.navigate('fund') }, prefer: ['right', 'left'] },
      { id: 'hrt-attribution', phase: 'decision', title: 'Paper fill, then attribution', target: [panel('Attribution')], duration: D, run: (s) => { s.setFund({ tab: 'decisions' }); s.navigate('fund') }, prefer: ['above'] },
    ],
  },
  mv: {
    id: 'mv',
    name: 'Metaview',
    audience: 'The whole loop, reviewed',
    thesis: 'Retrieval, analysis, modelling, estimation and decision on one truth system, ending with what is real today, what is still illustrative, and what comes next.',
    tone: 'fct',
    beats: [
      { id: 'mv-invisible', phase: 'invisible', title: 'The market nobody can see', target: TICKER, duration: D + 3000, run: toOverview, prefer: ['below'], width: 660 },
      { id: 'mv-retrieval', phase: 'retrieval', title: 'What was retrieved', target: QUOTES_HEAD, duration: D + 2000, run: toMarkets, prefer: ['below', 'above'], width: 780 },
      { id: 'mv-analysis', phase: 'analysis', title: 'Four classes of statement', target: [{ sel: 'main span.font-serif', text: 'Observed', closest: '.grid' }, { sel: 'main [data-truth-cards]', phone: true }], duration: D + 2000, run: toHandoff, prefer: ['below'], width: 860 },
      { id: 'mv-factors', phase: 'modelling', title: 'Six factors, one score', target: [eyebrow('Top drivers of the inference'), { sel: 'main h2', nth: 0, up: 3 }], duration: D + 3000, run: toXrayFactors, prefer: ['left'], width: 660 },
      { id: 'mv-reveal', phase: 'modelling', title: 'Raw to inferred', target: [{ sel: 'input[aria-label="Raw to inferred"]', up: 1 }, { sel: 'input[aria-label="Observed to inferred"]', up: 1, phone: true }], duration: D + 2000, run: toXrayReveal, prefer: ['right'] },
      { id: 'mv-estimation', phase: 'estimation', title: 'Tested against a baseline', target: [panel('Does compute data improve predictions?')], duration: D + 3000, run: toResearch, prefer: ['above', 'below'], width: 720 },
      { id: 'mv-forecast', phase: 'estimation', title: 'What FLOPX refuses to say', target: [{ sel: 'main .rounded-md.border.p-3', text: 'Timing gate' }, panel('Scheduling scenarios')], duration: D + 2000, run: toProcurement, prefer: ['left', 'above'] },
      // The agent beat's answer takes the backend 6 to 12 s, so its two requests start here, one beat early.
      { id: 'mv-loop', phase: 'decision', title: 'The loop closes on the brief', target: [panel('What changed')], duration: D + 2000, run: (s) => { toOverview(s); void prefetchAsk(AGENT_QUESTION) }, prefer: ['left'], width: 560 },
      { id: 'mv-agent', phase: 'decision', title: 'The agent answers from engine tools', target: [{ sel: 'main h3', text: 'Texas', closest: 'section' }, panel('Engine tools'), { sel: 'main article', nth: 0, phone: true }, { sel: 'main form', phone: true }], duration: D + 4000, run: (s) => { s.setInspector(null); void prefetchAsk(AGENT_QUESTION); s.navigate('agents', { q: AGENT_QUESTION }) }, prefer: ['left'], await: { ready: () => agentAnswerSettled(), capMs: AGENT_WAIT_CAP_MS } },
      { id: 'mv-review', phase: 'review', title: 'What is real today', target: null, duration: D + 6000, run: toOverview, width: 940 },
    ],
  },
}

export const TOUR_IDS: TourId[] = ['gr', 'hrt', 'mv']
export const TOUR_LENGTHS: Record<TourId, number> = { gr: TOURS.gr.beats.length, hrt: TOURS.hrt.beats.length, mv: TOURS.mv.beats.length }

export { MAIN }
