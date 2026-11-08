// The phone breakpoint every mobile branch shares, and the shell's phone navigation: the tab bar plus
// More must reach every page the desktop sidebar reaches, each exactly once.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { createElement } from 'react'
import { isPhoneNow, PHONE_QUERY, subscribePhone, useIsPhone } from './usePhone'
import { morePages, NAV, PHONE_TABS, pageTitle } from '@/components/Shell'

type Listener = () => void
function fakeMatchMedia(matches: boolean) {
  const listeners = new Set<Listener>()
  const mql = { matches, media: PHONE_QUERY, addEventListener: (_: string, l: Listener) => listeners.add(l), removeEventListener: (_: string, l: Listener) => listeners.delete(l) }
  const matchMedia = vi.fn((q: string) => { expect(q).toBe(PHONE_QUERY); return mql })
  return { mql, listeners, matchMedia }
}

afterEach(() => vi.unstubAllGlobals())

describe('PHONE_QUERY', () => {
  it("is the complement of Tailwind's md: (min-width: 768px), so a 767 px viewport is a phone and 768 px is not", () => {
    const max = Number(/max-width:\s*([\d.]+)px/.exec(PHONE_QUERY)![1])
    expect(767 <= max).toBe(true)
    expect(768 <= max).toBe(false)
    expect(767.99 <= max).toBe(false)
  })
})

describe('isPhoneNow / subscribePhone', () => {
  it('reads the media query, and is false where matchMedia does not exist (server, tests)', () => {
    expect(isPhoneNow()).toBe(false)
    vi.stubGlobal('window', { matchMedia: fakeMatchMedia(true).matchMedia })
    expect(isPhoneNow()).toBe(true)
    vi.stubGlobal('window', { matchMedia: fakeMatchMedia(false).matchMedia })
    expect(isPhoneNow()).toBe(false)
  })
  it('subscribes to breakpoint changes and unsubscribes cleanly', () => {
    const f = fakeMatchMedia(false)
    vi.stubGlobal('window', { matchMedia: f.matchMedia })
    const cb = vi.fn()
    const off = subscribePhone(cb)
    expect(f.listeners.size).toBe(1)
    for (const l of f.listeners) l()
    expect(cb).toHaveBeenCalledTimes(1)
    off()
    expect(f.listeners.size).toBe(0)
  })
})

describe('useIsPhone', () => {
  it('renders the desktop branch on the server (no viewport to measure)', () => {
    const Probe = () => createElement('span', null, useIsPhone() ? 'phone' : 'desktop')
    expect(renderToStaticMarkup(createElement(Probe))).toBe('<span>desktop</span>')
  })
})

describe('phone navigation', () => {
  const all = NAV.flatMap((s) => s.items.map((it) => it.id))
  it('the tabs are Brief, X-Ray, Markets and Fund, in that order', () => {
    expect(PHONE_TABS.map((t) => [t.id, t.label])).toEqual([['overview', 'Brief'], ['xray', 'X-Ray'], ['markets', 'Markets'], ['fund', 'Fund']])
  })
  it('tabs plus More reach every sidebar page exactly once', () => {
    const more = morePages().flatMap((s) => s.items.map((it) => it.id))
    const reached = [...PHONE_TABS.map((t) => t.id), ...more]
    expect([...reached].sort()).toEqual([...all].sort())
    expect(new Set(reached).size).toBe(reached.length)
  })
  it('More keeps the sidebar sections and drops a section emptied by the tabs', () => {
    const sections = morePages().map((s) => s.section)
    expect(sections).toEqual(['Compute & Intelligence', 'Agents', 'Coverage', 'System'])
  })
  it('the header titles each page with its sidebar label', () => {
    expect(pageTitle('overview')).toBe('Global Intelligence')
    expect(pageTitle('handoff')).toBe('Truth System & States')
  })
})
