import { describe, expect, it } from 'vitest'
import { collapse, isCollapsed, panelKey, toggleIn, withoutPage } from './layout'

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
