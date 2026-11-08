import { describe, expect, it } from 'vitest'
import { collapse, isCollapsed, panelKey, RAIL, rowTemplate, sideKey, toggleIn, withoutPage } from './layout'

describe('layout helpers', () => {
  it('builds page-scoped panel keys', () => {
    expect(panelKey('fund', 'Equity curve')).toBe('fund:Equity curve')
  })

  it('toggles a key in and out without mutating the input', () => {
    const a: string[] = []
    const b = toggleIn(a, 'fund:A')
    expect(b).toEqual(['fund:A'])
    expect(a).toEqual([])
    expect(toggleIn(b, 'fund:A')).toEqual([])
  })

  it('clears only the given page on reset', () => {
    const s = ['fund:A', 'fund:B', 'markets:A', 'fundx:A']
    expect(withoutPage(s, 'fund')).toEqual(['markets:A', 'fundx:A'])
    expect(withoutPage([], 'fund')).toEqual([])
  })
})

describe('collapse store', () => {
  it('toggles, reports and resets per page (works without localStorage)', () => {
    collapse.resetPage('t')
    expect(isCollapsed('t', 'P')).toBe(false)
    collapse.toggle('t', 'P')
    expect(isCollapsed('t', 'P')).toBe(true)
    expect(collapse.anyOnPage('t')).toBe(true)
    collapse.resetPage('t')
    expect(isCollapsed('t', 'P')).toBe(false)
    expect(collapse.anyOnPage('t')).toBe(false)
  })
})

describe('rowTemplate', () => {
  it('keeps weights and fixed tracks when nothing is folded', () => {
    expect(rowTemplate([1.35, 1], [false, false])).toBe('minmax(0,1.35fr) minmax(0,1fr)')
    expect(rowTemplate([1, '380px'], [])).toBe('minmax(0,1fr) 380px')
  })
  it('turns a folded track into a rail, fixed or flexible', () => {
    expect(rowTemplate([1.35, 1], [true, false])).toBe(`${RAIL}px minmax(0,1fr)`)
    expect(rowTemplate([1, '380px'], [false, true])).toBe(`minmax(0,1fr) ${RAIL}px`)
  })
  it('lets a fixed track take the width once every flexible track is folded', () => {
    expect(rowTemplate(['320px', 1], [false, true])).toBe(`minmax(0,1fr) ${RAIL}px`)
    expect(rowTemplate(['320px', 1, '200px'], [false, false, true])).toBe(`320px minmax(0,1fr) ${RAIL}px`)
    expect(rowTemplate(['300px', '200px'], [false, false])).toBe('300px 200px')
  })
  it('never folds every track', () => {
    expect(rowTemplate([1, 1, 1], [true, true, true])).toBe(`${RAIL}px ${RAIL}px minmax(0,1fr)`)
  })
  it('returns an empty template for no tracks', () => expect(rowTemplate([], [])).toBe(''))
})

describe('side folds', () => {
  it('use their own key, separate from the up fold, and reset with the page', () => {
    expect(sideKey('Offers')).toBe('Offers:side')
    collapse.resetPage('s')
    collapse.toggle('s', sideKey('P'))
    expect(isCollapsed('s', sideKey('P'))).toBe(true)
    expect(isCollapsed('s', 'P')).toBe(false)
    collapse.resetPage('s')
    expect(isCollapsed('s', sideKey('P'))).toBe(false)
  })
})
