import { describe, expect, it } from 'vitest'
import { clampBeat, goTo, keyAction, leadsWith, next, parse, phasesOf, prev, serialise, SPINE, start, togglePlay, togglePresent, type BeatMeta } from './model'

const beats = (phases: BeatMeta['phase'][]): BeatMeta[] => phases.map((phase, i) => ({ id: `b${i}`, phase, title: `t${i}`, target: null, duration: 1000 }))

describe('transitions', () => {
  it('starts on beat 0, not playing', () => {
    expect(start('gr')).toEqual({ id: 'gr', beat: 0, playing: false, present: false })
    expect(start('mv', true).present).toBe(true)
  })
  it('next advances and stops autoplay at the end instead of wrapping', () => {
    const s = { ...start('hrt'), playing: true }
    expect(next(s, 3).beat).toBe(1)
    const last = { ...s, beat: 2 }
    expect(next(last, 3)).toEqual({ ...last, playing: false })
    const stopped = { ...last, playing: false }
    expect(next(stopped, 3)).toBe(stopped) // same object when nothing changes
  })
  it('prev clamps at 0', () => {
    const s = start('gr')
    expect(prev(s)).toBe(s)
    expect(prev({ ...s, beat: 2 }).beat).toBe(1)
  })
  it('goTo and clampBeat clamp into range and truncate', () => {
    expect(clampBeat(7, 5)).toBe(4)
    expect(clampBeat(-3, 5)).toBe(0)
    expect(clampBeat(2.9, 5)).toBe(2)
    expect(clampBeat(NaN, 5)).toBe(0)
    expect(goTo(start('gr'), 99, 4).beat).toBe(3)
  })
  it('toggles are involutions', () => {
    const s = start('gr')
    expect(togglePlay(togglePlay(s))).toEqual(s)
    expect(togglePresent(togglePresent(s))).toEqual(s)
  })
})

describe('spine', () => {
  it('lists the six phases in pitch order plus the closing review', () => {
    expect(SPINE.map((s) => s.n)).toEqual([1, 2, 3, 4, 5, 6, 7])
    expect(SPINE.map((s) => s.id)).toEqual(['invisible', 'retrieval', 'analysis', 'modelling', 'estimation', 'decision', 'review'])
  })
  it('phasesOf collapses consecutive repeats and leadsWith reads spine numbers', () => {
    const b = beats(['invisible', 'analysis', 'modelling', 'modelling', 'estimation', 'decision', 'decision'])
    expect(phasesOf(b)).toEqual(['invisible', 'analysis', 'modelling', 'estimation', 'decision'])
    expect(leadsWith(b, 4)).toEqual([1, 3, 4, 5])
  })
})

describe('persistence', () => {
  const lengths = { gr: 9, hrt: 8, mv: 10 }
  it('round-trips id, beat and present, never autoplay', () => {
    const s = { id: 'mv' as const, beat: 4, playing: true, present: true }
    expect(parse(serialise(s), lengths)).toEqual({ ...s, playing: false })
    expect(serialise(null)).toBe('')
  })
  it('rejects malformed or unknown input and clamps a stale beat', () => {
    expect(parse(null, lengths)).toBeNull()
    expect(parse('', lengths)).toBeNull()
    expect(parse('{not json', lengths)).toBeNull()
    expect(parse(JSON.stringify({ id: 'nope', beat: 1 }), lengths)).toBeNull()
    expect(parse(JSON.stringify({ id: 'gr', beat: 40 }), lengths)!.beat).toBe(8)
    expect(parse(JSON.stringify({ id: 'gr' }), lengths)!.beat).toBe(0)
    expect(parse(JSON.stringify({ id: 'gr' }), { ...lengths, gr: 0 })).toBeNull()
  })
})

describe('keyboard', () => {
  it('maps arrows, space and Escape; ignores typing in a field except Escape', () => {
    expect(keyAction('ArrowRight', false)).toBe('next')
    expect(keyAction('ArrowLeft', false)).toBe('prev')
    expect(keyAction(' ', false)).toBe('play')
    expect(keyAction('Escape', false)).toBe('exit')
    expect(keyAction('Escape', true)).toBe('exit')
    expect(keyAction('ArrowRight', true)).toBeNull()
    expect(keyAction(' ', true)).toBeNull()
    expect(keyAction('p', false)).toBe('present')
    expect(keyAction('x', false)).toBeNull()
  })
})
