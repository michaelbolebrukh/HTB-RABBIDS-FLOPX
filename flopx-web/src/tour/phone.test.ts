// M-PAGES-3: a target spec marked `phone` is tried only below 768 px, so the desktop tour resolves
// exactly the targets it did before, while a phone reaches the element its own layout renders.
import { afterEach, describe, expect, it } from 'vitest'
import { resolveTarget, SWIPE_MIN_PX } from './Stage'
import { TOURS } from './tours'

const el = (id: string) => ({
  id,
  textContent: id,
  parentElement: null,
  getBoundingClientRect: () => ({ left: 0, top: 0, width: 100, height: 40, right: 100, bottom: 40, x: 0, y: 0 }) as DOMRect,
  closest: () => null,
})
const root = { querySelectorAll: (sel: string) => (sel === 'main [data-truth-cards]' ? [el('cards')] : []) } as unknown as ParentNode
const specs = [{ sel: 'main span.font-serif', text: 'Observed' }, { sel: 'main [data-truth-cards]', phone: true }]

const g = globalThis as unknown as { window?: { innerWidth: number } }
const had = 'window' in g
afterEach(() => { if (!had) delete g.window })

describe('phone-only target specs', () => {
  it('are skipped at desktop widths', () => {
    g.window = { innerWidth: 1440 }
    expect(resolveTarget(specs, root)).toBeNull()
  })
  it('are skipped at exactly 768 px, the first desktop width', () => {
    g.window = { innerWidth: 768 }
    expect(resolveTarget(specs, root)).toBeNull()
  })
  it('resolve below 768 px', () => {
    g.window = { innerWidth: 390 }
    expect((resolveTarget(specs, root) as unknown as { id: string }).id).toBe('cards')
  })
  it('are never a beat\'s first spec, so a desktop target always wins when it exists', () => {
    for (const t of Object.values(TOURS)) for (const b of t.beats) if (b.target) expect(b.target[0].phone).toBeFalsy()
  })
  it('a swipe needs a deliberate distance', () => {
    expect(SWIPE_MIN_PX).toBeGreaterThanOrEqual(40)
  })
})
