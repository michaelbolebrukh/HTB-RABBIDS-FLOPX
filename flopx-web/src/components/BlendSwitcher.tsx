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
    <div role="radiogroup" aria-label="Theme" className="flex h-7 items-center rounded-ctl border border-line bg-ink p-[2px]">
      {THEMES.map((t) => (
        <button
          key={t.id}
          role="radio"
          aria-checked={t.id === theme}
          onClick={() => setTheme(t.id)}
          className={
            'h-full cursor-pointer rounded-[2px] px-2.5 text-[11.5px] font-medium transition-colors duration-[120ms] ' +
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
