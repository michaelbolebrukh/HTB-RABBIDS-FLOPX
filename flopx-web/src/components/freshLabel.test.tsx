// One word per provenance kind (WF8 #1): the `historical` kind read "Historical" on the X-Ray Evidence
// row and "Archived" on the ticker strip, the Securities list and the Markets eyebrow. The shared map
// in ui.tsx is the one place the word lives; the strip's own word table is held to it here.
import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { Fresh, freshLabel } from './ui'
import { closeBadgeOf } from '@/live/adapt-w2'
import type { Freshness } from '@/data/core'

const KINDS: Freshness[] = ['live', 'delayed', 'historical', 'stale', 'unavailable', 'experimental', 'simulated', 'illustrative']

describe('freshLabel: one word per data-status kind', () => {
  it('the archived kind reads "Archived", never "Historical", on the chip and the quiet note alike', () => {
    expect(freshLabel('historical')).toBe('Archived')
    const chip = renderToStaticMarkup(<Fresh f="historical" />)
    const quiet = renderToStaticMarkup(<Fresh quiet f="historical" />)
    expect(chip).toContain('>Archived<')
    expect(quiet).toContain('>Archived<')
    expect(chip).not.toContain('Historical')
    expect(quiet).not.toContain('Historical')
    expect(chip).toContain('data-fresh="chip"')
    expect(quiet).toContain('data-fresh="quiet"')
  })
  it('every kind has a distinct one-word label', () => {
    const words = KINDS.map(freshLabel)
    expect(new Set(words).size).toBe(KINDS.length)
    for (const w of words) expect(w).toMatch(/^[A-Z][a-z]+$/)
  })
  it('the ticker-strip word for an archived close is the shared label', () => {
    const b = closeBadgeOf({ placed: { t: '2026-11-06T21:00:00Z', value: 186.7, fresh: { freshness: 'historical' }, source: 'alpaca-data', latestWeek: true } })
    expect(b.label.split(' · ')[0]).toBe(freshLabel('historical'))
  })
})
