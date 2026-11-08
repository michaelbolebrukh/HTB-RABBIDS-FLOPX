// WF5 #2: the agent beat's card must never report a landed answer when the page fell back to its
// prepared card, and a time to failure is never shown as "the backend took". No DOM here (vitest runs
// in node), so the Agent Desk is a small fake tree with the parts agentAnswerState reads.
import { afterEach, describe, expect, it } from 'vitest'
import type { LiveResult } from '@/live/client'
import type { AskResponse } from '@/live/adapt-w4'
import { __resetPrefetch, AGENT_WAIT_CAP_MS, agentAnswerLanded, agentAnswerSettled, agentAnswerState, prefetchAsk, prefetchFailedMs, prefetchWarmMs } from './prefetch'
import { agentCardState } from './cards'
import askTexas from '@/live/__fixtures__/w4/ask-texas.json'

type FakeEl = { textContent: string; closest?: (sel: string) => FakeEl | null; querySelectorAll: (sel: string) => FakeEl[] }
const el = (textContent: string, children: Record<string, FakeEl[]> = {}, section: FakeEl | null = null): FakeEl => ({
  textContent,
  closest: () => section,
  querySelectorAll: (sel) => children[sel] ?? [],
})

/** A fake `main` holding one answer card for the Texas question in the given header state. */
function desk(state: 'none' | 'asking' | 'offline' | 'landed' | 'other-question'): ParentNode {
  const spans = state === 'asking' ? [el('Asking the FLOPX backend')] : state === 'offline' ? [el('Offline · prepared card'), el('Illustrative')] : state === 'landed' ? [el('Tool: Region brief'), el('Live')] : []
  const section: FakeEl = el('', { span: spans })
  const h3 = el(state === 'other-question' ? 'Why are H100 quotes higher?' : 'What is happening in Texas?', {}, section)
  const main: FakeEl = el('', { h3: state === 'none' ? [] : [h3], span: spans })
  return { querySelector: () => (main as unknown as Element) } as unknown as ParentNode
}

afterEach(() => __resetPrefetch())

describe('agentAnswerState reads the Agent Desk', () => {
  it('is none before the card exists, asking while the header says so, and landed only for a live answer', () => {
    expect(agentAnswerState(desk('none'))).toBe('none')
    expect(agentAnswerState(desk('other-question'))).toBe('none')
    expect(agentAnswerState(desk('asking'))).toBe('asking')
    expect(agentAnswerState(desk('landed'))).toBe('landed')
    expect(agentAnswerLanded(desk('landed'))).toBe(true)
  })
  it('never counts the offline prepared card as landed, but autoplay may move on from it', () => {
    expect(agentAnswerState(desk('offline'))).toBe('offline')
    expect(agentAnswerLanded(desk('offline'))).toBe(false)
    expect(agentAnswerSettled(desk('offline'))).toBe(true)
    expect(agentAnswerSettled(desk('asking'))).toBe(false)
    expect(agentAnswerSettled(desk('landed'))).toBe(true)
  })
  it('returns none without a main', () => {
    expect(agentAnswerState({ querySelector: () => null } as unknown as ParentNode)).toBe('none')
  })
})

const ok = (data: unknown): LiveResult<AskResponse> => ({ ok: true, data: data as AskResponse, ms: 1 })
const fail = (): LiveResult<AskResponse> => ({ ok: false, error: 'TypeError: Failed to fetch', ms: 1 })

describe('prefetch timing', () => {
  it('exposes warmMs only for a request that succeeded', async () => {
    const w = prefetchAsk('q-ok', async () => ok(askTexas))
    await w.done
    expect(w.ok).toBe(true)
    expect(prefetchWarmMs('q-ok')).toBe(w.ms)
    expect(prefetchFailedMs('q-ok')).toBeNull()
  })
  it('exposes a failed request as failedMs and never as warmMs', async () => {
    const w = prefetchAsk('q-fail', async () => fail())
    await w.done
    expect(w.ok).toBe(false)
    expect(prefetchWarmMs('q-fail')).toBeNull()
    expect(prefetchFailedMs('q-fail')).toBe(w.ms)
  })
  it('treats a thrown post as a failure too, and both are null while the request runs', async () => {
    let release: () => void = () => {}
    const w = prefetchAsk('q-slow', () => new Promise((_, rej) => { release = () => rej(new Error('boom')) }))
    expect(prefetchWarmMs('q-slow')).toBeNull()
    expect(prefetchFailedMs('q-slow')).toBeNull()
    release()
    await w.done
    expect(w.ok).toBe(false)
    expect(prefetchFailedMs('q-slow')).not.toBeNull()
  })
})

describe('agentCardState', () => {
  it('goes offline when the page shows its prepared card and stays there', () => {
    expect(agentCardState('offline', 'pending', 1)).toBe('offline')
    expect(agentCardState('landed', 'offline', 5)).toBe('offline')
  })
  it('lands only on a live answer, and holds landed', () => {
    expect(agentCardState('landed', 'pending', 7)).toBe('landed')
    expect(agentCardState('asking', 'landed', 9)).toBe('landed')
  })
  it('keeps waiting while the page asks, then times out at the cap', () => {
    expect(agentCardState('asking', 'pending', 3)).toBe('pending')
    expect(agentCardState('none', 'pending', 3)).toBe('pending')
    expect(agentCardState('asking', 'pending', AGENT_WAIT_CAP_MS / 1000)).toBe('timeout')
    // A late answer after the cap still lands.
    expect(agentCardState('landed', 'timeout', 25)).toBe('landed')
  })
})
