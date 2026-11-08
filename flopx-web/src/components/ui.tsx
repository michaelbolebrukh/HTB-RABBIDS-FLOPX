import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { RotateCcw, Sparkles } from 'lucide-react'
import type { Freshness, Truth } from '@/data/core'
import type { HorizonId, ValidationStatus } from '@/data/fund'
import { useStore, type EntityRef } from '@/lib/store'
import { collapse, panelKey, useCollapsed } from '@/lib/layout'
import { Drawer, FoldButton } from './collapse'

export function cx(...c: (string | false | null | undefined)[]) {
  return c.filter(Boolean).join(' ')
}

export const TRUTH: Record<Truth, { label: string; short: string; color: string; desc: string }> = {
  obs: { label: 'Observed', short: 'OBS', color: 'var(--color-obs)', desc: 'Direct measurement or reported fact, with source and timestamp.' },
  inf: { label: 'Inferred', short: 'INF', color: 'var(--color-inf)', desc: 'Modelled hidden state, with evidence, assumptions and uncertainty.' },
  fct: { label: 'Forecast', short: 'FCT', color: 'var(--color-fct)', desc: 'A future distribution or scenario with a horizon.' },
  dec: { label: 'Decision', short: 'DEC', color: 'var(--color-dec)', desc: 'A proposed or executed action with owner, reasoning and risk.' },
}

/** Shape-coded swatch so categories survive colour-blindness and greyscale. */
export function TruthGlyph({ truth, size = 9 }: { truth: Truth; size?: number }) {
  const s = { width: size, height: size }
  if (truth === 'obs') return <span className="inline-block shrink-0 bg-obs" style={s} />
  if (truth === 'inf') return <span className="inline-block shrink-0 border border-inf hatch-inf" style={s} />
  if (truth === 'fct') return <span className="inline-block shrink-0 rounded-full border border-dashed border-fct" style={{ ...s, borderWidth: 1.5 }} />
  return <span className="inline-block shrink-0 rotate-45 bg-dec" style={{ width: size * 0.8, height: size * 0.8 }} />
}

export function TruthTag({ truth, compact, className }: { truth: Truth; compact?: boolean; className?: string }) {
  const t = TRUTH[truth]
  return (
    <span title={`${t.label}: ${t.desc}`} className={cx('inline-flex items-center gap-1.5 text-[11px] font-medium', className)} style={{ color: t.color }}>
      <TruthGlyph truth={truth} size={8} />
      {compact ? t.short : t.label}
    </span>
  )
}

type Mark = 'square' | 'half' | 'hollow' | 'ring' | 'dashed' | 'diamond' | 'hatch' | 'bar'

/** Validation icon: one ring, filled in proportion to the evidence (0 = none, 1 = quarter, 2 = half, 4 = full). */
function FillDisc({ level, size = 11 }: { level: 0 | 1 | 2 | 4; size?: number }) {
  const fill =
    level === 4 ? <circle cx="6" cy="6" r="3.4" fill="currentColor" />
    : level === 2 ? <path d="M6 2.6a3.4 3.4 0 0 0 0 6.8Z" fill="currentColor" />
    : level === 1 ? <path d="M6 6V2.6a3.4 3.4 0 0 1 3.4 3.4Z" fill="currentColor" />
    : null
  return (
    <svg width={size} height={size} viewBox="0 0 12 12" aria-hidden="true" className="shrink-0">
      <circle cx="6" cy="6" r="4.75" fill="none" stroke="currentColor" strokeWidth="1.3" />
      {fill}
    </svg>
  )
}

/**
 * Status icon in the badge's text colour (currentColor), drawn on a 12-unit grid:
 * square = check (validated), half = half-filled (partial), ring = quarter-filled (delayed),
 * hollow = clock (historical / too little history), dashed = dashed ring (simulated),
 * diamond = warning triangle (stale), hatch = hatched disc (experimental), bar = minus (unavailable / no edge).
 */
function StatusMark({ m, size = 11 }: { m: Mark; size?: number }) {
  const ring = <circle cx="6" cy="6" r="4.75" fill="none" stroke="currentColor" strokeWidth="1.3" />
  const body = (() => {
    switch (m) {
      case 'square':
        return (
          <>
            <circle cx="6" cy="6" r="5.4" fill="currentColor" fillOpacity="0.16" />
            {ring}
            <path d="M3.7 6.2 5.3 7.7 8.4 4.5" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
          </>
        )
      case 'half':
        return <>{ring}<path d="M6 2.6a3.4 3.4 0 0 0 0 6.8Z" fill="currentColor" /></>
      case 'ring':
        return <>{ring}<path d="M6 6V2.6a3.4 3.4 0 0 1 3.4 3.4Z" fill="currentColor" /></>
      case 'hollow':
        return <>{ring}<path d="M6 3.6V6l1.7 1.1" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" /></>
      case 'dashed':
        return <circle cx="6" cy="6" r="4.75" fill="none" stroke="currentColor" strokeWidth="1.3" strokeDasharray="2.1 1.6" />
      case 'diamond':
        return (
          <>
            <path d="M6 1.6 10.8 10H1.2Z" fill="currentColor" fillOpacity="0.16" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
            <path d="M6 4.8v2.3" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
            <circle cx="6" cy="8.6" r="0.7" fill="currentColor" />
          </>
        )
      case 'hatch':
        return (
          <>
            <clipPath id="sm-hatch"><circle cx="6" cy="6" r="4.2" /></clipPath>
            <g clipPath="url(#sm-hatch)" stroke="currentColor" strokeWidth="0.9">
              <path d="M-1 7 7-1M1 9 9 1M3 11l8-8M5 13l8-8" />
            </g>
            {ring}
          </>
        )
      default:
        return <>{ring}<path d="M3.8 6h4.4" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" /></>
    }
  })()
  return (
    <svg width={size} height={size} viewBox="0 0 12 12" aria-hidden="true" className="shrink-0">
      {body}
    </svg>
  )
}

const badge = 'inline-flex items-center gap-1.5 rounded-[3px] border border-line2 whitespace-nowrap font-medium'

// One word per provenance kind, everywhere: the `historical` kind (a stored, point-in-time record) reads
// "Archived" on the chip, the quiet note, the ticker strip and the Markets eyebrow alike (WF8 #1).
const FRESH: Record<Freshness, { label: string; cls: string; icon: ReactNode }> = {
  live: { label: 'Live', cls: 'text-up', icon: <span className="pulse-dot inline-block size-1.5 rounded-full bg-up" /> },
  delayed: { label: 'Delayed', cls: 'text-mute', icon: <StatusMark m="ring" /> },
  historical: { label: 'Archived', cls: 'text-dim', icon: <StatusMark m="hollow" /> },
  stale: { label: 'Stale', cls: 'text-warn', icon: <StatusMark m="diamond" /> },
  unavailable: { label: 'Unavailable', cls: 'text-down', icon: <StatusMark m="bar" /> },
  experimental: { label: 'Experimental', cls: 'text-inf', icon: <StatusMark m="hatch" /> },
  simulated: { label: 'Simulated', cls: 'text-dec border-dashed', icon: <StatusMark m="dashed" /> },
  illustrative: { label: 'Illustrative', cls: 'text-mute border-dashed', icon: <StatusMark m="half" /> },
}
/** The one word a data-status kind reads as, for prose that names a status without rendering the badge. */
export const freshLabel = (f: Freshness): string => FRESH[f].label

/** Data-status label. The chip (bordered) marks a single figure, such as a headline KPI; `quiet` is the
 *  plain muted note a panel carries once in its eyebrow or foot instead of a chip on every row. */
export function Fresh({ f, className, label, quiet }: { f: Freshness; className?: string; label?: string; quiet?: boolean }) {
  const x = FRESH[f]
  if (quiet) {
    return (
      <span data-fresh="quiet" title={x.label} className={cx('inline-flex items-center gap-1 font-mono text-[10px] font-normal whitespace-nowrap text-dim', className)}>
        {x.icon}
        {label ?? x.label}
      </span>
    )
  }
  return (
    <span data-fresh="chip" className={cx(badge, 'px-1.5 py-[1px] text-[9px] tracking-[0.1em]', x.cls, className)}>
      {x.icon}
      {label ?? x.label}
    </span>
  )
}

/** The freshness most rows in a list share, so a panel can carry it once and only the rows that differ keep a chip. */
export function dominantFresh(rows: readonly Freshness[]): Freshness | null {
  if (!rows.length) return null
  const n = new Map<Freshness, number>()
  for (const f of rows) n.set(f, (n.get(f) ?? 0) + 1)
  return [...n.entries()].sort((a, b) => b[1] - a[1])[0][0]
}

const VAL: Record<ValidationStatus, { label: string; short: string; tone: string; cls: string; icon: ReactNode; desc: string }> = {
  validated: { label: 'Validated', short: 'Validated', tone: 'text-up', cls: 'text-up', icon: <FillDisc level={4} />, desc: 'Out-of-sample tests beat baseline with enough independent windows.' },
  exploratory: { label: 'Exploratory', short: 'Exploratory', tone: 'text-warn', cls: 'text-warn', icon: <FillDisc level={2} />, desc: 'Promising but fewer than 12 independent windows.' },
  insufficient: { label: 'Insufficient history', short: 'Little data', tone: 'text-mute', cls: 'text-mute', icon: <FillDisc level={1} />, desc: 'Too little history to validate. Shown for research only.' },
  'no-edge': { label: 'No edge found', short: 'No edge', tone: 'text-dim', cls: 'text-dim', icon: <FillDisc level={0} />, desc: 'Tested; did not beat the baseline.' },
}
/** Validation status badge. `short` uses the compact label (full wording stays in the tooltip); `fill` stretches it to its cell so a column of badges lines up. */
export function Validation({ s, short, fill, className }: { s: ValidationStatus; short?: boolean; fill?: boolean; className?: string }) {
  const v = VAL[s]
  return (
    <span title={`${v.label}. ${v.desc}`} className={cx(badge, 'h-6 px-2 text-[11px]', fill && 'flex w-full min-w-0', v.cls, className)}>
      {v.icon}
      <span className="truncate">{short ? v.short : v.label}</span>
    </span>
  )
}

const HORIZONS = ['1W', '1M', '3M'] as const

/** One strip, one equal-width cell per forecast horizon, always 1W · 1M · 3M in that order, so strips line up
 *  row to row. The status variant sets the icon and colour; a missing horizon shows a dash. Full wording is in the tooltip. */
export function HorizonStatus({ horizons, className }: { horizons: Partial<Record<HorizonId, { status: ValidationStatus }>>; className?: string }) {
  return (
    <div role="list" aria-label="Validation by horizon" className={cx('grid w-full grid-cols-3 overflow-hidden rounded-[3px] border border-line2 text-[11px] whitespace-nowrap', className)}>
      {HORIZONS.map((h, i) => {
        const st = horizons[h]?.status
        const v = st ? VAL[st] : null
        return (
          <span key={h} role="listitem" title={v ? `${h}: ${v.label}. ${v.desc}` : `${h}: not tested`} className={cx('flex min-w-0 items-center gap-1.5 px-2 py-[3px]', i > 0 && 'border-l border-line2')}>
            <span className="w-5 shrink-0 font-mono text-[10px] text-dim">{h}</span>
            {v ? <span className={cx('flex min-w-0 items-center gap-1.5', v.tone)}>{v.icon}<span className="truncate">{v.short}</span></span> : <span className="text-dim">—</span>}
          </span>
        )
      })}
    </div>
  )
}

const DIRECTION_TONE = { Long: 'text-up', Short: 'text-down', 'No position': 'text-dim' } as const

/** Signal direction tag: one style, the variant only sets the colour. */
export function DirectionTag({ direction, asset, className }: { direction: keyof typeof DIRECTION_TONE; asset?: string; className?: string }) {
  return (
    <span className={cx('shrink-0 text-[11px] font-medium whitespace-nowrap', DIRECTION_TONE[direction], className)}>
      {direction}{asset && <span className="ml-1 font-mono text-[10.5px]">{asset}</span>}
    </span>
  )
}

/** Card with an optional header. A titled panel can be folded with the header chevron (as on Compute X-Ray);
 *  the fold is remembered per page. Pass `id` when the title is not plain text, or `collapsible={false}` to opt out. */
export function Panel({ title, eyebrow, right, children, className, bodyClass, truth, id, collapsible = true }: { title?: ReactNode; eyebrow?: ReactNode; right?: ReactNode; children: ReactNode; className?: string; bodyClass?: string; truth?: Truth; id?: string; collapsible?: boolean }) {
  const { route } = useStore()
  const folded = useCollapsed()
  const pid = id ?? (typeof title === 'string' ? title : undefined)
  const canFold = collapsible && pid != null
  const open = !canFold || !folded.includes(panelKey(route.page, pid))
  const body = <div className={cx('p-4', bodyClass)}>{children}</div>
  return (
    <section className={cx('min-w-0 rounded-md border border-line bg-panel', className, !open && 'self-start')}>
      {(title || eyebrow || right) && (
        <header className={cx('flex items-start justify-between gap-3 px-4 py-2.5 transition-[border-color] duration-300', open ? 'border-b border-line' : 'border-b border-transparent')}>
          <div className="min-w-0">
            {eyebrow && <div className="eyebrow mb-0.5 flex items-center gap-2">{eyebrow}</div>}
            {title && (
              <h3 className="flex items-center gap-2 text-[13px] font-semibold tracking-tight text-bone">
                {truth && <TruthGlyph truth={truth} />}
                {title}
              </h3>
            )}
          </div>
          {(right || canFold) && (
            <div className="flex shrink-0 items-center gap-2">
              {open && right}
              {canFold && <FoldButton open={open} label={pid.toLowerCase()} onClick={() => collapse.toggle(route.page, pid)} />}
            </div>
          )}
        </header>
      )}
      {canFold ? <Drawer open={open}>{body}</Drawer> : body}
    </section>
  )
}

export function Delta({ v, d = 1, suffix = '%', className, invert }: { v: number; d?: number; suffix?: string; className?: string; invert?: boolean }) {
  const pos = invert ? v < 0 : v > 0
  const neg = invert ? v > 0 : v < 0
  return (
    <span className={cx('num', pos && 'text-up', neg && 'text-down', !pos && !neg && 'text-mute', className)}>
      {v > 0 ? '+' : v < 0 ? '−' : ''}
      {Math.abs(v).toFixed(d)}
      {suffix}
    </span>
  )
}

type BtnVariant = 'primary' | 'ghost' | 'outline' | 'danger' | 'decision' | 'subtle'
export function Btn({ variant = 'outline', size = 'md', className, children, ...rest }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: BtnVariant; size?: 'sm' | 'md' }) {
  return (
    <button
      {...rest}
      className={cx(
        'inline-flex items-center justify-center gap-1.5 rounded-[4px] font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40 focus-visible:outline focus-visible:outline-1 focus-visible:outline-fct whitespace-nowrap cursor-pointer',
        size === 'sm' ? 'h-7 px-2.5 text-[11.5px]' : 'h-8 px-3 text-[12.5px]',
        variant === 'primary' && 'bg-bone text-ink hover:bg-white',
        variant === 'outline' && 'border border-line2 text-bone hover:border-mute hover:bg-hover',
        variant === 'ghost' && 'text-mute hover:bg-hover hover:text-bone',
        variant === 'subtle' && 'bg-raise text-bone hover:bg-hover',
        variant === 'danger' && 'border border-down/40 text-down hover:bg-down/10',
        variant === 'decision' && 'bg-dec text-ink hover:brightness-110',
        className,
      )}
    >
      {children}
    </button>
  )
}

export function Seg<T extends string>({ value, options, onChange, size = 'md', className }: { value: T; options: { id: T; label: ReactNode; title?: string }[]; onChange: (v: T) => void; size?: 'sm' | 'md'; className?: string }) {
  return (
    <div className={cx('inline-flex rounded-[5px] border border-line bg-ink p-0.5', className)} role="tablist">
      {options.map((o) => (
        <button
          key={o.id}
          title={o.title}
          role="tab"
          aria-selected={value === o.id}
          onClick={() => onChange(o.id)}
          className={cx('cursor-pointer rounded-[3px] font-mono tracking-wide transition-colors', size === 'sm' ? 'px-2 py-0.5 text-[10.5px]' : 'px-2.5 py-1 text-[11.5px]', value === o.id ? 'bg-raise text-bone shadow-[inset_0_0_0_1px_var(--color-line2)]' : 'text-dim hover:text-bone')}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}

export function Tabs<T extends string>({ value, tabs, onChange, className }: { value: T; tabs: { id: T; label: ReactNode; count?: number }[]; onChange: (v: T) => void; className?: string }) {
  return (
    <div className={cx('flex gap-5 border-b border-line', className)} role="tablist">
      {tabs.map((t) => (
        <button key={t.id} role="tab" aria-selected={value === t.id} onClick={() => onChange(t.id)} className={cx('-mb-px cursor-pointer border-b-2 pb-2.5 pt-1 text-[13px] font-medium transition-colors', value === t.id ? 'border-bone text-bone' : 'border-transparent text-dim hover:text-mute')}>
          {t.label}
          {t.count != null && <span className="num ml-1.5 text-[10px] text-dim">{t.count}</span>}
        </button>
      ))}
    </div>
  )
}

export function Spark({ values, width = 80, height = 22, color = 'var(--color-obs)', dashedFrom, className }: { values: (number | null)[]; width?: number; height?: number; color?: string; dashedFrom?: number; className?: string }) {
  const v = values.map((x) => (x == null || Number.isNaN(x) ? null : x))
  const nums = v.filter((x): x is number => x != null)
  if (!nums.length) return <svg width={width} height={height} className={className} />
  const min = Math.min(...nums)
  const max = Math.max(...nums)
  const sx = (i: number) => (i / (v.length - 1)) * (width - 2) + 1
  const sy = (y: number) => height - 2 - ((y - min) / (max - min || 1)) * (height - 4)
  const path = (from: number, to: number) => {
    let d = ''
    let pen = false
    for (let i = from; i <= to; i++) {
      const y = v[i]
      if (y == null) { pen = false; continue }
      d += `${pen ? 'L' : 'M'}${sx(i).toFixed(1)},${sy(y).toFixed(1)}`
      pen = true
    }
    return d
  }
  const cut = dashedFrom ?? v.length - 1
  return (
    <svg width={width} height={height} className={className} aria-hidden>
      <path d={path(0, cut)} fill="none" stroke={color} strokeWidth={1.25} />
      {dashedFrom != null && <path d={path(cut, v.length - 1)} fill="none" stroke="var(--color-fct)" strokeWidth={1.25} strokeDasharray="2 2" />}
    </svg>
  )
}

export function EntityLink({ r, children, className }: { r: EntityRef; children?: ReactNode; className?: string }) {
  const { inspect } = useStore()
  return (
    <button onClick={(e) => { e.stopPropagation(); inspect(r) }} className={cx('cursor-pointer text-left underline decoration-line2 decoration-dotted underline-offset-[3px] hover:text-white hover:decoration-mute', className)}>
      {children ?? r.label}
    </button>
  )
}

export function Source({ source, time, className }: { source: string; time?: string; className?: string }) {
  return (
    <span className={cx('font-mono text-[10px] text-dim', className)}>
      {source}
      {time && <> · {time}</>}
    </span>
  )
}

export function Empty({ icon, title, body, action, tone = 'neutral', className }: { icon?: ReactNode; title: string; body?: ReactNode; action?: ReactNode; tone?: 'neutral' | 'warn' | 'bad'; className?: string }) {
  return (
    <div className={cx('flex flex-col items-center justify-center gap-2 rounded-md border border-dashed px-6 py-8 text-center', tone === 'warn' ? 'border-warn/40' : tone === 'bad' ? 'border-down/40' : 'border-line2', className)}>
      {icon && <div className={cx(tone === 'warn' ? 'text-warn' : tone === 'bad' ? 'text-down' : 'text-dim')}>{icon}</div>}
      <div className="text-[13px] font-semibold text-bone">{title}</div>
      {body && <div className="max-w-sm text-[12px] leading-relaxed text-mute">{body}</div>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  )
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="rounded-[3px] border border-line2 bg-raise px-1 font-mono text-[10px] text-mute">{children}</kbd>
}

export function AIBlock({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cx('rounded-md border border-dashed border-fct/30 bg-fct/[0.03] p-3', className)}>
      <div className="mb-1.5 flex items-center gap-1.5 text-[11px] text-fct font-medium">
        <Sparkles size={11} /> AI-generated interpretation · not a retrieved fact
      </div>
      <div className="text-[12.5px] leading-relaxed text-bone/90">{children}</div>
    </div>
  )
}

export function Meter({ value, max = 1, color = 'var(--color-bone)', className, hatched }: { value: number; max?: number; color?: string; className?: string; hatched?: boolean }) {
  return (
    <div className={cx('h-1.5 w-full overflow-hidden rounded-full bg-line', className)}>
      <div className={cx('h-full rounded-full', hatched && 'hatch-inf')} style={{ width: `${Math.max(0, Math.min(1, value / max)) * 100}%`, background: hatched ? undefined : color, border: hatched ? `1px solid ${color}` : undefined }} />
    </div>
  )
}

/** Unfolds every folded panel on the current page. Renders nothing while nothing is folded. */
export function ResetLayout() {
  const { route } = useStore()
  useCollapsed()
  if (!collapse.anyOnPage(route.page)) return null
  return <Btn size="sm" variant="ghost" onClick={() => collapse.resetPage(route.page)} title="Show every folded panel on this page"><RotateCcw size={12} /> Reset layout</Btn>
}

export function PageHeader({ eyebrow, title, sub, right }: { eyebrow: ReactNode; title: ReactNode; sub?: ReactNode; right?: ReactNode }) {
  right = <><ResetLayout />{right}</>
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        <div className="eyebrow mb-1.5 flex items-center gap-2">{eyebrow}</div>
        <h1 className="font-serif text-[34px] leading-[1.05] tracking-tight text-bone">{title}</h1>
        {sub && <p className="mt-2 max-w-3xl text-[13px] leading-relaxed text-mute">{sub}</p>}
      </div>
      <div className="flex flex-wrap items-center gap-2 empty:hidden">{right}</div>
    </div>
  )
}
