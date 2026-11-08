import { useEffect, useState } from 'react'

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

/** Dark (Implement original) / Light (flopx paper) toggle. Persists to localStorage and ?theme=. */
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
    <div role="radiogroup" aria-label="Theme" className="flex items-center gap-1 rounded-full border border-line2 bg-panel/95 p-1 shadow-[0_8px_24px_rgba(0,0,0,0.25)] backdrop-blur">
      {THEMES.map((t) => (
        <button
          key={t.id}
          role="radio"
          aria-checked={t.id === theme}
          onClick={() => setTheme(t.id)}
          className={
            'cursor-pointer rounded-full px-3 py-1 text-[11.5px] font-medium transition-colors ' +
            (t.id === theme ? 'bg-bone text-ink' : 'text-mute hover:bg-hover hover:text-bone')
          }
        >
          {t.label}
        </button>
      ))}
    </div>
  )
}

export { isTheme }
