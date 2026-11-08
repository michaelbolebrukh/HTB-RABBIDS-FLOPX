import { useEffect, useState } from 'react'
import { PHONE_QUERY } from '@/lib/usePhone'

export const THEMES = [
  { id: 'dark', label: 'Dark' },
  { id: 'light', label: 'Light' },
] as const
export type ThemeId = (typeof THEMES)[number]['id']

const KEY = 'flopx.theme'
const isTheme = (v: string | null | undefined): v is ThemeId => THEMES.some((t) => t.id === v)

function initialTheme(): ThemeId {
  return document.documentElement.dataset.blend === 'light' ? 'light' : 'dark'
}

/**
 * Phone only: point every <meta name="theme-color"> at the in-app theme's background (the --color-ink token),
 * so the status bar follows the Dark/Light switch rather than the OS scheme. Off the phone the tags get
 * their original index.html values back, so desktop behaviour is unchanged.
 */
export function syncThemeColor(phone: boolean) {
  const root = document.documentElement
  // The token when the stylesheet has loaded; its index.css value otherwise.
  const bg = getComputedStyle(root).getPropertyValue('--color-ink').trim() || (root.dataset.blend === 'light' ? '#f6f1e8' : '#0c0e10')
  for (const m of document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]')) {
    if (m.dataset.orig == null) m.dataset.orig = m.content
    m.content = phone && bg ? bg : m.dataset.orig
  }
}

// Runs at load, not in the switcher, because on a phone the switcher only mounts inside the More sheet:
// follow the blend attribute (whoever sets it) and the breakpoint.
if (typeof window !== 'undefined' && typeof window.matchMedia === 'function' && typeof MutationObserver !== 'undefined') {
  const mql = window.matchMedia(PHONE_QUERY)
  const run = () => syncThemeColor(mql.matches)
  new MutationObserver(run).observe(document.documentElement, { attributes: true, attributeFilter: ['data-blend'] })
  mql.addEventListener?.('change', run)
  window.addEventListener('load', run)
  run()
}

/** Dark / Light (flopx paper) segmented control in the top bar. Persists to localStorage and ?theme=. */
export function BlendSwitcher() {
  const [theme, setTheme] = useState<ThemeId>(initialTheme)
  useEffect(() => {
    const root = document.documentElement
    if (theme === 'light') root.dataset.blend = 'light'
    else delete root.dataset.blend
    try {
      localStorage.setItem(KEY, theme)
    } catch {
      /* storage unavailable: the theme still applies for this view */
    }
    const url = new URL(location.href)
    url.searchParams.set('theme', theme)
    history.replaceState(history.state, '', url)
  }, [theme])

  return (
    <div role="radiogroup" aria-label="Theme" className="flex h-7 items-center rounded-ctl border border-line bg-ink p-[2px] max-md:h-11">
      {THEMES.map((t) => (
        <button
          key={t.id}
          role="radio"
          aria-checked={t.id === theme}
          onClick={() => setTheme(t.id)}
          className={
            'h-full cursor-pointer rounded-[2px] px-2.5 text-[11.5px] font-medium transition-colors duration-[120ms] max-md:min-w-11 max-md:px-3 ' +
            (t.id === theme ? 'bg-raise text-bone' : 'text-mute hover:text-bone')
          }
        >
          {t.label}
        </button>
      ))}
    </div>
  )
}

export { isTheme }
