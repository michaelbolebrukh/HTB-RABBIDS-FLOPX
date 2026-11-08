import { useEffect, useRef, useState, type ReactNode } from 'react'
import { CHART_TYPE_GROUPS, CHART_TYPES, type ChartType } from '../lib/chartTypes'

const S = { fill: 'none', stroke: 'currentColor', strokeWidth: 1.2, strokeLinecap: 'round', strokeLinejoin: 'round' } as const

function Svg({ children }: { children: ReactNode }) {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true" className="shrink-0" {...S}>
      {children}
    </svg>
  )
}

const ICONS: Record<ChartType, ReactNode> = {
  bars: (
    <Svg>
      <path d="M5 3v12M3 6h2M5 12h2M13 2v11M11 4h2M13 10h2" />
    </Svg>
  ),
  candles: (
    <Svg>
      <path d="M5 2v3M5 13v3M13 3v2M13 12v3" />
      <rect x="3" y="5" width="4" height="8" fill="currentColor" />
      <rect x="11" y="5" width="4" height="7" />
    </Svg>
  ),
  hollow: (
    <Svg>
      <path d="M5 2v3M5 13v3M13 3v2M13 12v3" />
      <rect x="3" y="5" width="4" height="8" />
      <rect x="11" y="5" width="4" height="7" />
    </Svg>
  ),
  volcandles: (
    <Svg>
      <path d="M4 2v3M4 13v3M13 3v2M13 14v2" />
      <rect x="3" y="5" width="2" height="8" />
      <rect x="10" y="5" width="6" height="9" />
    </Svg>
  ),
  heikin: (
    <Svg>
      <path d="M5 2v2M5 12v3M13 4v2M13 13v2" />
      <rect x="3" y="4" width="4" height="8" />
      <rect x="11" y="6" width="4" height="7" fill="currentColor" />
    </Svg>
  ),
  line: (
    <Svg>
      <path d="M2 13l4-5 3 3 7-8" />
    </Svg>
  ),
  markers: (
    <Svg>
      <path d="M3 13l3-5 3 3 5-7" />
      <circle cx="3" cy="13" r="1.4" fill="currentColor" />
      <circle cx="6" cy="8" r="1.4" fill="currentColor" />
      <circle cx="9" cy="11" r="1.4" fill="currentColor" />
      <circle cx="14" cy="4" r="1.4" fill="currentColor" />
    </Svg>
  ),
  step: (
    <Svg>
      <path d="M2 13h4V9h4v3h3V4h3" />
    </Svg>
  ),
  area: (
    <Svg>
      <path d="M2 14V11l4-4 3 3 7-6v10z" fill="currentColor" fillOpacity={0.25} />
      <path d="M2 11l4-4 3 3 7-6" />
      <path d="M6 14v-3M9 14v-1.5M12 14v-5" />
    </Svg>
  ),
  hlc: (
    <Svg>
      <path d="M2 7l4-3 4 3 6-4" />
      <path d="M2 14l4-3 4 3 6-4" />
      <path d="M6 4v7M10 7v7M16 3v7M2 7v7" strokeOpacity={0.35} />
    </Svg>
  ),
  baseline: (
    <Svg>
      <path d="M2 9h14" strokeDasharray="2 2" />
      <path d="M2 13l4-3 3-1 3-4 4-1" />
      <path d="M6 10v3M12 5v-2" />
    </Svg>
  ),
  columns: (
    <Svg>
      <rect x="2.5" y="9" width="3" height="6" fill="currentColor" />
      <rect x="7.5" y="4" width="3" height="11" fill="currentColor" />
      <rect x="12.5" y="7" width="3" height="8" fill="currentColor" />
    </Svg>
  ),
  highlow: (
    <Svg>
      <rect x="4" y="2.5" width="3" height="13" />
      <rect x="11" y="5" width="3" height="10" />
    </Svg>
  ),
}

const rowCls = 'flex h-8 w-full cursor-pointer items-center gap-2.5 px-3 text-left text-[12px] outline-none'

export function ChartTypeMenu({ value, onChange }: { value: ChartType; onChange: (t: ChartType) => void }) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const current = CHART_TYPES.find((t) => t.id === value)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    const items = menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitemradio"]')
    const sel = menuRef.current?.querySelector<HTMLElement>('[aria-checked="true"]')
    ;(sel ?? items?.[0])?.focus()
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (!open) return
    if (e.key === 'Escape') {
      e.preventDefault()
      setOpen(false)
      triggerRef.current?.focus()
      return
    }
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
    e.preventDefault()
    const items = Array.from(menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitemradio"]') ?? [])
    if (!items.length) return
    const i = items.indexOf(document.activeElement as HTMLElement)
    const next = e.key === 'ArrowDown' ? (i + 1) % items.length : (i <= 0 ? items.length - 1 : i - 1)
    items[next].focus()
  }

  return (
    <div ref={rootRef} className="relative inline-block" onKeyDown={onKeyDown}>
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className="inline-flex h-[26px] cursor-pointer items-center gap-1.5 rounded-[4px] border border-line2 bg-panel px-2 font-mono text-[11.5px] tracking-wide text-bone transition-colors hover:bg-hover max-md:h-11"
      >
        {ICONS[value]}
        <span>{current?.label}</span>
        <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true" className="text-mute" {...S}>
          <path d="M2 4l3 3 3-3" />
        </svg>
      </button>
      {open && (
        <div ref={menuRef} role="menu" className="absolute right-0 top-full z-50 mt-1 max-h-[70vh] w-[220px] overflow-y-auto rounded-[4px] border border-line2 bg-panel py-1 shadow-lg">
          {CHART_TYPE_GROUPS.map((group, gi) => (
            <div key={gi} role="group" className={gi > 0 ? 'mt-1 border-t border-line pt-1' : ''}>
              {group.map((t) => {
                const sel = t.id === value
                return (
                  <button
                    key={t.id}
                    type="button"
                    role="menuitemradio"
                    aria-checked={sel}
                    onClick={() => {
                      onChange(t.id)
                      setOpen(false)
                      triggerRef.current?.focus()
                    }}
                    className={`${rowCls} ${sel ? 'bg-bone text-ink' : 'text-bone hover:bg-hover focus:bg-hover'}`}
                  >
                    {ICONS[t.id]}
                    <span>{t.label}</span>
                  </button>
                )
              })}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
