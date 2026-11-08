// The Agent Desk beat: the backend answers POST /flopx/ask in 6 to 12 s (two requests: the intent,
// then the named target's composed brief), longer than a beat's dwell. The tour therefore starts
// the same two requests when the previous beat begins, so the backend has the brief composed by the
// time the page asks, and the stage holds the beat's dwell until the answer is on the page (Stage,
// `Beat.await`). The page itself still asks through lib/ask.ts; nothing here is shown as an answer.
import { adaptAsk, askLive, briefQueryFor } from '@/live/adapt-w4'

export const AGENT_QUESTION = 'What is happening in Texas?'
/** Longest the dwell waits for the answer before moving on and saying so. */
export const AGENT_WAIT_CAP_MS = 20_000

interface Warm { startedAt: number; done: Promise<void>; ms: number | null; ok: boolean | null }
const warmed = new Map<string, Warm>()

/** Fire the two /flopx/ask requests for `q` once per session; later calls return the first run. */
export function prefetchAsk(q: string = AGENT_QUESTION, post: typeof askLive = askLive): Warm {
  const ex = warmed.get(q)
  if (ex) return ex
  const startedAt = performance.now()
  const w: Warm = { startedAt, done: Promise.resolve(), ms: null, ok: null }
  w.done = (async () => {
    try {
      const first = await post(q)
      if (first.ok) {
        const live = adaptAsk(first.data)
        if (live.kind === 'route' && live.target) await post(briefQueryFor(live.target))
      }
      w.ok = first.ok
    } catch {
      w.ok = false
    }
    w.ms = Math.round(performance.now() - startedAt)
  })()
  warmed.set(q, w)
  return w
}

export const prefetchState = (q: string = AGENT_QUESTION) => warmed.get(q) ?? null

/**
 * How long the backend took to answer the tour's early request, in ms; null while it is still running
 * and null when it failed. A time to failure is never a "the backend took" figure (WF5 #2).
 */
export function prefetchWarmMs(q: string = AGENT_QUESTION): number | null {
  const w = warmed.get(q)
  return w && w.ok === true ? w.ms : null
}

/** How long the tour's early request ran before it failed, in ms; null unless it failed. */
export function prefetchFailedMs(q: string = AGENT_QUESTION): number | null {
  const w = warmed.get(q)
  return w && w.ok === false ? w.ms : null
}

export type AgentAnswerState = 'none' | 'asking' | 'landed' | 'offline'

const ASKING_TEXT = 'Asking the FLOPX backend'
const OFFLINE_TEXT = 'Offline · prepared card'

/**
 * The page's own answer card for the Texas question, read from the Agent Desk: 'none' before the card
 * exists, 'asking' while its header says so, 'offline' when the page fell back to the design's prepared
 * card (the backend did not answer), 'landed' when a live answer is up. The offline card is never
 * "landed" (WF5 #2).
 */
export function agentAnswerState(root: ParentNode = document): AgentAnswerState {
  const main = root.querySelector('main')
  if (!main) return 'none'
  const h3 = Array.from(main.querySelectorAll('h3')).find((h) => (h.textContent ?? '').toLowerCase().includes('texas'))
  if (!h3) return 'none'
  // The card's header spans sit in the same section as its title; fall back to the whole main.
  const scope: ParentNode = (typeof h3.closest === 'function' && h3.closest('section')) || main
  const spans = Array.from(scope.querySelectorAll('span')).map((s) => (s.textContent ?? '').trim())
  if (spans.includes(ASKING_TEXT)) return 'asking'
  if (spans.includes(OFFLINE_TEXT)) return 'offline'
  return 'landed'
}

/** Is a live answer on the page? */
export function agentAnswerLanded(root: ParentNode = document): boolean {
  return agentAnswerState(root) === 'landed'
}

/** Has the page finished, one way or the other: a live answer, or its prepared card after a failure? Autoplay waits for this. */
export function agentAnswerSettled(root: ParentNode = document): boolean {
  const st = agentAnswerState(root)
  return st === 'landed' || st === 'offline'
}

/** Test helper. */
export function __resetPrefetch(): void {
  warmed.clear()
}
