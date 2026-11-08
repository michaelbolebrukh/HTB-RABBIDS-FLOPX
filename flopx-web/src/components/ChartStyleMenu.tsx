import { useEffect, useRef, useState } from 'react'
import { DEFAULT_CHART_STYLE, useChartStyleControls, type ChartStyle } from '@/lib/chartStyle'

type Choice<K extends keyof ChartStyle> = { key: K; label: string; options: { value: ChartStyle[K]; label: string }[] }

const CHOICES = [
  { key: 'curve', label: 'Curve', options: [{ value: 'linear', label: 'Linear' }, { value: 'smooth', label: 'Smooth' }, { value: 'step', label: 'Step' }] },
  { key: 'weight', label: 'Line weight', options: [{ value: 'thin', label: 'Thin' }, { value: 'regular', label: 'Regular' }, { value: 'bold', label: 'Bold' }] },
  { key: 'grid', label: 'Grid', options: [{ value: 'none', label: 'None' }, { value: 'y', label: 'Rows' }, { value: 'xy', label: 'Full' }] },
  { key: 'ticks', label: 'Axis ticks', options: [{ value: 'sparse', label: 'Sparse' }, { value: 'normal', label: 'Normal' }, { value: 'dense', label: 'Dense' }] },
  { key: 'font', label: 'Label font', options: [{ value: 'mono', label: 'Mono' }, { value: 'sans', label: 'Sans' }] },
  { key: 'points', label: 'Data points', options: [{ value: false, label: 'Off' }, { value: true, label: 'On' }] },
  { key: 'area', label: 'Area fill', options: [{ value: false, label: 'Off' }, { value: true, label: 'On' }] },
  { key: 'motion', label: 'Animation', options: [{ value: false, label: 'Off' }, { value: true, label: 'On' }] },
] as Choice<keyof ChartStyle>[]

/** Top-bar "Charts" button with a popover of global chart style options. */
export function ChartStyleMenu() {
  const { style, set, reset } = useChartStyleControls()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const close = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false) }
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', esc)
    return () => { document.removeEventListener('mousedown', close); document.removeEventListener('keydown', esc) }
  }, [open])
  const changed = (Object.keys(DEFAULT_CHART_STYLE) as (keyof ChartStyle)[]).filter((k) => style[k] !== DEFAULT_CHART_STYLE[k]).length

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className={'flex h-7 cursor-pointer items-center gap-1.5 rounded-ctl border px-2.5 text-[11.5px] font-medium transition-colors duration-[120ms] hover:bg-hover hover:text-bone ' + (open ? 'border-line2 bg-raise text-bone' : 'border-line text-mute')}
      >
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.3"><path d="M1 10.5h10M2.5 8l2.5-3 2 2 3-4.5" strokeLinecap="round" strokeLinejoin="round" /></svg>
        Charts{changed > 0 && <span className="num text-[10.5px] text-bone" aria-label={`${changed} changed`}>· {changed}</span>}
      </button>
      {open && (
        <div role="dialog" aria-label="Chart style" className="absolute top-[calc(100%+6px)] right-0 z-[60] w-[280px] rounded-menu border border-line2 bg-panel p-3 shadow-[0_12px_32px_rgba(0,0,0,0.3)]">
          <div className="mb-2.5 flex items-center justify-between">
            <span className="text-[11.5px] font-medium text-dim">Chart style</span>
            <button onClick={reset} disabled={!changed} className="cursor-pointer text-[11px] text-mute hover:text-bone disabled:cursor-default disabled:opacity-40">Reset</button>
          </div>
          <div className="space-y-2">
            {CHOICES.map((c) => (
              <div key={c.key} className="grid grid-cols-[88px_1fr] items-center gap-2">
                <span className="text-[11.5px] text-mute">{c.label}</span>
                <div role="radiogroup" aria-label={c.label} className="flex rounded-ctl border border-line bg-ink p-[2px]">
                  {c.options.map((o) => {
                    const on = style[c.key] === o.value
                    return (
                      <button
                        key={String(o.value)}
                        role="radio"
                        aria-checked={on}
                        onClick={() => set({ [c.key]: o.value } as Partial<ChartStyle>)}
                        className={'flex-1 cursor-pointer rounded-[2px] py-0.5 text-[11px] transition-colors duration-[120ms] ' + (on ? 'bg-raise text-bone' : 'text-mute hover:text-bone')}
                      >
                        {o.label}
                      </button>
                    )
                  })}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
