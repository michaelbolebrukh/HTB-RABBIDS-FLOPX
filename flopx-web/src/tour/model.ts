// The step model of a tour: pure data and pure transitions, no DOM and no React, so it can be
// tested on its own. A tour is a sequence of beats; every beat belongs to one phase of the
// six-beat spine that all three pitches share.

export type TourId = 'gr' | 'hrt' | 'mv'

export type Phase = 'invisible' | 'retrieval' | 'analysis' | 'modelling' | 'estimation' | 'decision' | 'review'

/** The spine, in pitch order. 'review' is Metaview's closing card and sits after the six. */
export const SPINE: { id: Phase; n: number; label: string; short: string }[] = [
  { id: 'invisible', n: 1, label: 'The invisible market', short: 'Invisible' },
  { id: 'retrieval', n: 2, label: 'Retrieval', short: 'Retrieve' },
  { id: 'analysis', n: 3, label: 'Analysis', short: 'Analyse' },
  { id: 'modelling', n: 4, label: 'Modelling', short: 'Model' },
  { id: 'estimation', n: 5, label: 'Estimation and honesty', short: 'Estimate' },
  { id: 'decision', n: 6, label: 'Decision', short: 'Decide' },
  { id: 'review', n: 7, label: 'What is real today', short: 'Review' },
]
export const phaseOf = (p: Phase) => SPINE.find((s) => s.id === p)!

/**
 * One element to spotlight: CSS selector, optional case-insensitive text filter, optional index
 * among the matches, then an optional climb (`up` parent hops, or `closest` selector) so a title
 * can select its whole panel.
 */
export interface TargetSpec {
  sel: string
  text?: string
  nth?: number
  up?: number
  closest?: string
  /** Tried only below 768 px (the phone layout), so desktop target resolution is unchanged. */
  phone?: boolean
}

/** A beat's static description. Behaviour (what it does to the app, what its card shows) lives in tours.ts and cards.tsx. */
export interface BeatMeta {
  id: string
  phase: Phase
  title: string
  /** Spotlight alternatives, tried in order. Null: no spotlight, the card is centred. */
  target: TargetSpec[] | null
  /** Autoplay dwell in ms. */
  duration: number
}

export interface TourMeta {
  id: TourId
  name: string
  audience: string
  /** The one-line thesis shown on the title card and the sidebar tooltip. */
  thesis: string
  tone: 'inf' | 'dec' | 'fct'
  beats: BeatMeta[]
}

export interface TourState {
  id: TourId
  beat: number
  playing: boolean
  present: boolean
}

export const DEFAULT_DWELL_MS = 9000

export function start(id: TourId, present = false): TourState {
  return { id, beat: 0, playing: false, present }
}

/** Clamp a beat index into the tour. */
export function clampBeat(i: number, n: number): number {
  if (!Number.isFinite(i) || n <= 0) return 0
  return Math.max(0, Math.min(n - 1, Math.trunc(i)))
}

/** Next beat; at the end, autoplay stops and the state is returned unchanged otherwise. */
export function next(s: TourState, n: number): TourState {
  if (s.beat >= n - 1) return s.playing ? { ...s, playing: false } : s
  return { ...s, beat: s.beat + 1 }
}

export function prev(s: TourState): TourState {
  return s.beat <= 0 ? s : { ...s, beat: s.beat - 1 }
}

export function goTo(s: TourState, i: number, n: number): TourState {
  return { ...s, beat: clampBeat(i, n) }
}

export const togglePlay = (s: TourState): TourState => ({ ...s, playing: !s.playing })
export const togglePresent = (s: TourState): TourState => ({ ...s, present: !s.present })

/** Phases a tour visits, in order and without repeats, for the progress rail's labels. */
export function phasesOf(beats: BeatMeta[]): Phase[] {
  const out: Phase[] = []
  for (const b of beats) if (out[out.length - 1] !== b.phase) out.push(b.phase)
  return out
}

/** Spine numbers a tour leads with: the phases of its first k distinct-phase beats. */
export function leadsWith(beats: BeatMeta[], k: number): number[] {
  return phasesOf(beats).slice(0, k).map((p) => phaseOf(p).n)
}

// ---------- persistence (the tour survives a reload mid-walkthrough) ----------

export const STORAGE_KEY = 'flopx.tour.v1'
const IDS: TourId[] = ['gr', 'hrt', 'mv']

export function serialise(s: TourState | null): string {
  return s ? JSON.stringify({ id: s.id, beat: s.beat, present: s.present }) : ''
}

/** Parse a stored state; anything malformed yields null (the tour simply is not resumed). Autoplay never resumes. */
export function parse(raw: string | null | undefined, lengths: Record<TourId, number>): TourState | null {
  if (!raw) return null
  try {
    const o = JSON.parse(raw) as { id?: unknown; beat?: unknown; present?: unknown }
    if (typeof o !== 'object' || o === null) return null
    if (typeof o.id !== 'string' || !IDS.includes(o.id as TourId)) return null
    const id = o.id as TourId
    const n = lengths[id] ?? 0
    if (n <= 0) return null
    const beat = typeof o.beat === 'number' ? clampBeat(o.beat, n) : 0
    return { id, beat, playing: false, present: o.present === true }
  } catch {
    return null
  }
}

/** Keyboard mapping shared by the stage and its tests. Returns null for keys the tour ignores. */
export type TourKey = 'next' | 'prev' | 'exit' | 'play' | 'first' | 'last' | 'present'
export function keyAction(key: string, inField: boolean): TourKey | null {
  if (key === 'Escape') return 'exit'
  if (inField) return null
  switch (key) {
    case 'ArrowRight':
    case 'PageDown':
    case 'Enter':
      return 'next'
    case 'ArrowLeft':
    case 'PageUp':
      return 'prev'
    case ' ':
      return 'play'
    case 'Home':
      return 'first'
    case 'End':
      return 'last'
    case 'p':
    case 'P':
      return 'present'
    default:
      return null
  }
}
