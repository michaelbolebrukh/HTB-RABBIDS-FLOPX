// Agents and Markets follow Emre's markup with FLOPX data: a smoke render that the page mounts and
// keeps the honest states (no prepared answer shown as asked, no keyword-router claim).
import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { StoreProvider } from '@/lib/store'
import Agents from './Agents'
import Markets from './Markets'

// The store reads the hash and session storage in its state initialisers; effects do not run on the server.
;(globalThis as { window?: unknown }).window = { location: { hash: '#/markets' }, sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} } }

describe("Emre's layout on Agents and Markets", () => {
  it('Agents opens empty, says what answers, and never claims a keyword router', () => {
    const html = renderToStaticMarkup(<StoreProvider><Agents /></StoreProvider>)
    expect(html).toContain('Ask FLOPX')
    expect(html).toContain('What do you want to know?')
    expect(html).toContain('FLOPX backend')
    expect(html).not.toContain('keyword router')
    expect(html).not.toContain('AI-generated interpretation')
  })
  it('Markets renders the rental index, the combined fair value panel and the notes', () => {
    const html = renderToStaticMarkup(<StoreProvider><Markets /></StoreProvider>)
    expect(html).toContain('GPU rental markets')
    expect(html).toContain('Fair value and venue comparison')
    expect(html).toContain('not transaction prices')
    expect(html).not.toContain('Validated at 1M')
  })
})
