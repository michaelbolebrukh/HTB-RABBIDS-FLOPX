import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { INDICATORS, indicatorById, type IndicatorCategory, type IndicatorDef, type IndicatorId } from '../lib/indicators'

const S = { fill: 'none', stroke: 'currentColor', strokeWidth: 1.2, strokeLinecap: 'round', strokeLinejoin: 'round' } as const

const CATEGORIES: IndicatorCategory[] = []
for (const d of INDICATORS as readonly IndicatorDef[]) if (!CATEGORIES.includes(d.category)) CATEGORIES.push(d.category)

interface Props {
  value: IndicatorId[]
  onChange: (ids: IndicatorId[]) => void
}

export function IndicatorMenu({ value, onChange }: Props) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [category, setCategory] = useState<IndicatorCategory | 'All'>('All')
  const [active, setActive] = useState(0)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const listRef = useRef<HTMLDivElement>(null)

  const all = INDICATORS as readonly IndicatorDef[]
  const q = query.trim().toLowerCase()
  const matchesQuery = (d: IndicatorDef) =>
    !q || d.label.toLowerCase().includes(q) || d.short.toLowerCase().includes(q) || d.description.toLowerCase().includes(q)
  const counts = useMemo(() => {
    const c: Record<string, number> = { All: 0 }
    for (const d of all) {
      if (!matchesQuery(d)) continue
      c.All++
      c[d.category] = (c[d.category] ?? 0) + 1
    }
    return c
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q])
  const rows = all.filter((d) => (category === 'All' || d.category === category) && matchesQuery(d))

  const close = () => {
    setOpen(false)
    triggerRef.current?.focus()
  }
  const toggle = (id: IndicatorId) => onChange(value.includes(id) ? value.filter((v) => v !== id) : [...value, id])

  useEffect(() => {
    if (open) {
      setQuery('')
      setCategory('All')
      setActive(0)
    }
  }, [open])
  useEffect(() => setActive(0), [q, category])
  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>(`[data-row="${active}"]`)?.scrollIntoView({ block: 'nearest' })
  }, [active])

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      e.preventDefault()
      e.stopPropagation()
      close()
    } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      if (!rows.length) return
      setActive((a) => (e.key === 'ArrowDown' ? (a + 1) % rows.length : (a <= 0 ? rows.length - 1 : a - 1)))
    } else if (e.key === 'Enter' || (e.key === ' ' && (e.target as HTMLElement).tagName !== 'INPUT')) {
      const t = e.target as HTMLElement
      if (t.tagName === 'BUTTON' && t.dataset.row === undefined) return
      e.preventDefault()
      if (rows[active]) toggle(rows[active].id)
    }
  }

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen(true)}
        className="inline-flex h-[26px] cursor-pointer items-center gap-1.5 rounded-[4px] border border-line2 bg-panel px-2 font-mono text-[11.5px] tracking-wide text-bone transition-colors hover:bg-hover max-md:h-11"
      >
        <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true" className="shrink-0" {...S}>
          <path d="M2 15h14M4 15v-4M8 15V8M12 15v-3M2 7l4-3 4 2 5-4" />
        </svg>
        <span>Indicators</span>
        {value.length > 0 && (
          <span className="rounded-full bg-bone px-1.5 text-[10px] leading-[14px] text-ink">{value.length}</span>
        )}
      </button>
      {open &&
        createPortal(
          <div
            className="fixed inset-0 z-[80] flex items-center justify-center bg-ink/40 p-4 backdrop-blur-[2px]"
            onMouseDown={(e) => {
              if (e.target === e.currentTarget) close()
            }}
          >
            <div
              role="dialog"
              aria-modal="true"
              aria-label="Indicators"
              onKeyDown={onKeyDown}
              className="flex h-[560px] max-h-full w-[760px] max-w-full flex-col overflow-hidden rounded-[8px] border border-line2 bg-panel text-bone shadow-2xl"
            >
              <div className="flex items-center justify-between border-b border-line px-4 py-3">
                <h2 className="text-[14px] font-medium">Indicators</h2>
                <button
                  type="button"
                  aria-label="Close"
                  onClick={close}
                  className="inline-flex h-6 w-6 cursor-pointer items-center justify-center rounded-[4px] text-mute hover:bg-hover hover:text-bone"
                >
                  <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true" {...S}>
                    <path d="M2 2l8 8M10 2l-8 8" />
                  </svg>
                </button>
              </div>
              <div className="border-b border-line px-4 py-2.5">
                <input
                  autoFocus
                  type="text"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Search indicators"
                  aria-label="Search indicators"
                  className="h-8 w-full rounded-[4px] border border-line2 bg-panel px-2.5 text-[12px] text-bone outline-none placeholder:text-dim focus:border-mute"
                />
              </div>
              <div className="flex min-h-0 flex-1">
                <div className="w-[160px] shrink-0 overflow-y-auto border-r border-line py-1.5 max-sm:w-[120px]">
                  {(['All', ...CATEGORIES] as const).map((c) => (
                    <button
                      key={c}
                      type="button"
                      onClick={() => setCategory(c)}
                      aria-pressed={category === c}
                      className={`flex w-full cursor-pointer items-center justify-between gap-2 px-3 py-1.5 text-left text-[12px] hover:bg-hover ${category === c ? 'bg-hover text-bone' : 'text-mute'}`}
                    >
                      <span className="truncate">{c}</span>
                      <span className="font-mono text-[10.5px] text-dim">{counts[c] ?? 0}</span>
                    </button>
                  ))}
                </div>
                <div ref={listRef} role="listbox" aria-multiselectable="true" aria-label="Indicator list" className="min-w-0 flex-1 overflow-y-auto py-1">
                  {rows.length === 0 && <div className="px-4 py-6 text-[12px] text-mute">No indicators match.</div>}
                  {rows.map((d, i) => {
                    const sel = value.includes(d.id)
                    return (
                      <div
                        key={d.id}
                        role="option"
                        aria-selected={sel}
                        data-row={i}
                        onClick={() => toggle(d.id)}
                        onMouseMove={() => active !== i && setActive(i)}
                        className={`flex cursor-pointer items-start gap-2.5 px-4 py-2 ${sel ? 'bg-hover' : ''} ${i === active ? 'outline outline-1 -outline-offset-1 outline-line2' : ''}`}
                      >
                        <span className="mt-0.5 inline-flex h-4 w-4 shrink-0 items-center justify-center text-inf">
                          {sel && (
                            <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true" {...S} strokeWidth={1.8}>
                              <path d="M3 7.5l2.5 2.5L11 4" />
                            </svg>
                          )}
                        </span>
                        <span className="min-w-0 flex-1">
                          <span className="flex flex-wrap items-baseline gap-x-2">
                            <span className="text-[12.5px] text-bone">{d.label}</span>
                            <span className="font-mono text-[10.5px] text-dim">{d.short}</span>
                          </span>
                          <span className="block truncate text-[11.5px] text-mute">{d.description}</span>
                        </span>
                        <span className="mt-0.5 shrink-0 rounded-[3px] border border-line px-1.5 font-mono text-[10px] leading-[16px] text-dim">
                          {d.placement === 'overlay' ? 'Overlay' : 'Pane'}
                        </span>
                      </div>
                    )
                  })}
                </div>
              </div>
              <div className="flex items-center justify-between border-t border-line px-4 py-2.5">
                <span className="font-mono text-[11px] text-mute">{value.length} active</span>
                <div className="flex gap-2">
                  <button
                    type="button"
                    disabled={value.length === 0}
                    onClick={() => onChange([])}
                    className="h-7 cursor-pointer rounded-[4px] border border-line2 px-3 text-[12px] text-bone hover:bg-hover disabled:cursor-default disabled:opacity-40 disabled:hover:bg-transparent"
                  >
                    Clear all
                  </button>
                  <button type="button" onClick={close} className="h-7 cursor-pointer rounded-[4px] bg-bone px-3 text-[12px] text-ink hover:opacity-90">
                    Done
                  </button>
                </div>
              </div>
            </div>
          </div>,
          document.body,
        )}
    </>
  )
}

export function IndicatorChips({ value, onChange }: Props) {
  if (value.length === 0) return null
  return (
    <div className="flex flex-wrap items-center gap-1">
      {value.map((id) => (
        <span key={id} className="inline-flex items-center gap-1 rounded-[3px] border border-line2 px-1.5 py-px font-mono text-[10.5px] text-bone">
          {indicatorById(id).short}
          <button
            type="button"
            aria-label={`Remove ${indicatorById(id).label}`}
            onClick={() => onChange(value.filter((v) => v !== id))}
            className="cursor-pointer leading-none text-mute hover:text-bone"
          >
            ×
          </button>
        </span>
      ))}
    </div>
  )
}
