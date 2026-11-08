import { useEffect, useRef, useState } from 'react'
import { useIsPhone } from '@/lib/usePhone'
import { cx, SHEET_MENU, SheetBackdrop, SheetHandle } from './ui'
import { BASES, DEFAULT_MAP_SCHEME, RAMPS, setMapScheme, useApplyMapScheme, useMapScheme, type Theme } from '@/lib/mapScheme'

/** Top-bar "Map" button: test alternative basemap and pressure-glow colourings in the current theme. */
export function MapSchemeMenu() {
  useApplyMapScheme()
  const scheme = useMapScheme()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const phone = useIsPhone()
  const sheetRef = useRef<HTMLDivElement>(null)
  const theme = useTheme()
  useEffect(() => {
    if (!open) return
    const close = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false) }
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', esc)
    return () => { document.removeEventListener('mousedown', close); document.removeEventListener('keydown', esc) }
  }, [open])
  const changed = Number(scheme.base !== DEFAULT_MAP_SCHEME.base) + Number(scheme.ramp !== DEFAULT_MAP_SCHEME.ramp)

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className={'flex h-7 cursor-pointer items-center gap-1.5 rounded-ctl border px-2.5 text-[11.5px] font-medium transition-colors duration-[120ms] hover:bg-hover hover:text-bone max-md:h-11 max-md:px-3 ' + (open ? 'border-line2 bg-raise text-bone' : 'border-line text-mute')}
      >
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.3"><circle cx="6" cy="6" r="4.8" /><path d="M1.2 6h9.6M6 1.2c1.5 1.4 2.2 3 2.2 4.8S7.5 9.4 6 10.8M6 1.2C4.5 2.6 3.8 4.2 3.8 6S4.5 9.4 6 10.8" /></svg>
        Map{changed > 0 && <span className="num text-[10.5px] text-bone" aria-label={`${changed} changed`}>· {changed}</span>}
      </button>
      {open && phone && <SheetBackdrop onClose={() => setOpen(false)} z="z-[96]" />}
      {open && (
        <div ref={sheetRef} role="dialog" aria-label="Map colours" className={cx('absolute top-[calc(100%+6px)] right-0 z-[60] w-[300px] rounded-menu border border-line2 bg-panel p-3 shadow-[0_12px_32px_rgba(0,0,0,0.3)]', SHEET_MENU, phone && 'm-sheet')}>
          {phone && <SheetHandle sheet={sheetRef} onClose={() => setOpen(false)} />}
          <div className="mb-2.5 flex items-center justify-between">
            <span className="text-[11.5px] font-medium text-dim">Map colours · {theme} mode</span>
            <button onClick={() => setMapScheme(DEFAULT_MAP_SCHEME)} disabled={!changed} className="cursor-pointer text-[11px] text-mute hover:text-bone disabled:cursor-default disabled:opacity-40 max-md:min-h-11 max-md:px-2">Reset</button>
          </div>
          <Group label="Basemap" value={scheme.base} onPick={(base) => setMapScheme({ base })}
            options={BASES.map((b) => ({ id: b.id, label: b.label, swatch: b[theme]['--color-land'] ? [b[theme]['--color-land'], b[theme]['--color-ocean'], b[theme]['--color-map-border']] : ['var(--color-land)', 'var(--color-ocean)', 'var(--color-line2)'] }))} />
          <Group label="Pressure glow" value={scheme.ramp} onPick={(ramp) => setMapScheme({ ramp })}
            options={RAMPS.map((r) => ({ id: r.id, label: r.label, swatch: r[theme]['--color-g0'] ? [0, 1, 2, 3, 4].map((i) => r[theme][`--color-g${i}`]) : [0, 1, 2, 3, 4].map((i) => `var(--color-g${i})`) }))} />
          <p className="mt-2.5 text-[11px] leading-snug text-dim">For testing legibility. Each scheme has a dark and a light variant; switch the theme to compare. Glows encode pressure, so the ramp stays ordered low to high.</p>
        </div>
      )}
    </div>
  )
}

function Group({ label, value, options, onPick }: { label: string; value: string; options: { id: string; label: string; swatch: string[] }[]; onPick: (id: string) => void }) {
  return (
    <div className="mb-2.5">
      <div className="mb-1 text-[11.5px] text-mute">{label}</div>
      <div role="radiogroup" aria-label={label} className="grid grid-cols-3 gap-1">
        {options.map((o) => {
          const on = o.id === value
          return (
            <button key={o.id} role="radio" aria-checked={on} onClick={() => onPick(o.id)}
              className={'flex cursor-pointer flex-col gap-1 rounded-ctl border p-1.5 text-left text-[11px] transition-colors duration-[120ms] max-md:min-h-11 ' + (on ? 'border-line2 bg-raise text-bone' : 'border-line text-mute hover:text-bone')}>
              <span className="flex h-2.5 overflow-hidden rounded-[2px]">{o.swatch.map((c, i) => <span key={i} className="flex-1" style={{ background: c }} />)}</span>
              {o.label}
            </button>
          )
        })}
      </div>
    </div>
  )
}

/** Current theme from <html data-blend>, kept in sync with the theme switch. */
function useTheme(): Theme {
  const read = (): Theme => (document.documentElement.dataset.blend === 'light' ? 'light' : 'dark')
  const [t, setT] = useState(read)
  useEffect(() => {
    const mo = new MutationObserver(() => setT(read()))
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-blend'] })
    return () => mo.disconnect()
  }, [])
  return t
}
