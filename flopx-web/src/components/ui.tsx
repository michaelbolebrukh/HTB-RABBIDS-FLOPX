import { createContext, useContext, useEffect, useRef, useState, type ButtonHTMLAttributes, type ReactNode, type RefObject } from 'react'
import { Info, RotateCcw, Sparkles } from 'lucide-react'
import type { Freshness, Truth } from '@/data/core'
import type { HorizonId, ValidationStatus } from '@/data/fund'
import { useStore, useStoreOptional, type EntityRef } from '@/lib/store'
import { collapse, panelKey, useCollapsed } from '@/lib/layout'
import { fetchJson } from '@/live/client'
import type { ResearchMetricsPayload } from './ResearchMetrics'
import { Drawer, FoldButton, SideFoldButton, useRegisterInRow, useRowSlot } from './collapse'
import './phone.css'

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

const badge = 'inline-flex items-center gap-1.5 whitespace-nowrap font-medium'
/** Exceptions keep a 1px border and a light tint; routine states are glyph plus text only. */
const exception = 'rounded-[2px] border px-1.5'

// One word per provenance kind, everywhere: the `historical` kind (a stored, point-in-time record) reads
// "Archived" on the chip, the quiet note, the ticker strip and the Markets eyebrow alike (WF8 #1).
const FRESH: Record<Freshness, { label: string; cls: string; icon: ReactNode }> = {
  live: { label: 'Live', cls: 'text-up', icon: <span className="inline-block size-1.5 rounded-full bg-up" /> },
  delayed: { label: 'Delayed', cls: 'text-mute', icon: <StatusMark m="ring" /> },
  historical: { label: 'Archived', cls: 'text-dim', icon: <StatusMark m="hollow" /> },
  stale: { label: 'Stale', cls: cx(exception, 'border-warn/40 bg-warn/[0.07] text-warn'), icon: <StatusMark m="diamond" /> },
  unavailable: { label: 'Unavailable', cls: cx(exception, 'border-down/40 bg-down/[0.07] text-down'), icon: <StatusMark m="bar" /> },
  experimental: { label: 'Experimental', cls: cx(exception, 'border-inf/40 bg-inf/[0.06] text-inf'), icon: <StatusMark m="hatch" /> },
  simulated: { label: 'Simulated', cls: cx(exception, 'border-dashed border-dec/50 bg-dec/[0.06] text-dec'), icon: <StatusMark m="dashed" /> },
  illustrative: { label: 'Illustrative', cls: cx(exception, 'border-dashed border-line2 bg-bone/[0.04] text-mute'), icon: <StatusMark m="half" /> },
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
    <span data-fresh="chip" className={cx(badge, 'py-px text-[11px] leading-4', x.cls, className)}>
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
    <span title={`${v.label}. ${v.desc}`} className={cx(badge, 'h-6 text-[11px]', fill && 'flex w-full min-w-0', v.cls, className)}>
      {v.icon}
      <span className="truncate">{short ? v.short : v.label}</span>
    </span>
  )
}

const HORIZONS = ['1W', '1M', '3M'] as const

/** One strip, one equal-width cell per forecast horizon, always 1W · 1M · 3M in that order, so strips line up
 *  row to row. The status variant sets the icon and colour; a missing horizon shows a dash. Full wording is in the tooltip.
 *  A cell too narrow for its label shows the icon alone rather than a clipped word. */
export function HorizonStatus({ horizons, className }: { horizons: Partial<Record<HorizonId, { status: ValidationStatus }>>; className?: string }) {
  return (
    <div role="list" aria-label="Validation by horizon" className={cx('grid w-full grid-cols-3 gap-x-3 text-[11px] leading-4 whitespace-nowrap', className)}>
      {HORIZONS.map((h) => {
        const st = horizons[h]?.status
        const v = st ? VAL[st] : null
        return (
          <span key={h} role="listitem" title={v ? `${h}: ${v.label}. ${v.desc}` : `${h}: not tested`} className="@container flex min-w-0 items-center gap-1.5 py-[3px]">
            <span className="w-5 shrink-0 text-dim">{h}</span>
            {v ? <span className={cx('flex min-w-0 items-center gap-1.5', v.tone)}>{v.icon}<span className="@max-[6.5rem]:sr-only">{v.short}</span></span> : <span className="text-dim">—</span>}
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

/** Surface role: canvas = bordered work surface (charts, maps, tables); rail = unbordered list split by rules;
 *  strip = one KPI surface with vertical dividers (fill it with StripCell); callout = exception or decision with a coloured left rule. */
export type PanelVariant = 'canvas' | 'rail' | 'strip' | 'callout'
export type CalloutTone = 'warn' | 'bad' | 'dec' | 'inf'

/** Rows inside a rail (or any list) split by soft rules. A rail body applies this to its direct children already. */
export const RULED = 'divide-y divide-line-soft'

const SURFACE: Record<PanelVariant, string> = {
  canvas: 'rounded-[2px] border border-line bg-panel',
  strip: 'rounded-[2px] border border-line bg-panel',
  rail: '',
  callout: 'rounded-r-[2px] border-l-2',
}
const CALLOUT: Record<CalloutTone, string> = {
  warn: 'border-l-warn bg-warn/[0.06]',
  bad: 'border-l-down bg-down/[0.06]',
  dec: 'border-l-dec bg-dec/[0.06]',
  inf: 'border-l-inf bg-inf/[0.06]',
}
const BODY: Record<PanelVariant, string> = {
  canvas: 'p-4',
  strip: 'flex flex-wrap divide-x divide-line-soft max-md:grid max-md:grid-cols-2 max-md:divide-x-0',
  rail: cx('py-1', RULED),
  callout: 'px-4 pb-3 pt-1',
}

/** When set, the nearest Panel shows this (a view switcher) in place of its title. Reset inside panel bodies. */
const TitleSwap = createContext<ReactNode>(null)

/**
 * Several panels sharing one slot: the active one renders, and its title becomes a switcher between them
 * (each keeps its own eyebrow). Use it to fold related views into one place instead of stacking more rows.
 */
export function PanelSwitch({ items, className }: { items: { label: string; node: ReactNode }[]; className?: string }) {
  const [i, setI] = useState(0)
  const seg = <Seg size="sm" value={String(i)} onChange={(v) => setI(+v)} options={items.map((it, k) => ({ id: String(k), label: it.label }))} />
  return <TitleSwap.Provider value={seg}><div className={cx('min-w-0', className)}>{items[i]?.node}</div></TitleSwap.Provider>
}

/** Sets a tooltip with the element's full text, so a truncated eyebrow can still be read. */
const fullTextTitle = (e: { currentTarget: HTMLElement }) => { e.currentTarget.title = e.currentTarget.textContent ?? '' }

/**
 * Surface with an optional one-line header: the title is the subject, the eyebrow follows it as quiet text that
 * truncates (full text on hover). A titled panel folds with its header chevron and the fold is remembered per page;
 * inside a Row it also folds sideways (that chevron shows on header hover or focus). Pass `id` when the title is not
 * plain text, or `collapsible={false}` to opt out.
 */
export function Panel({ title, eyebrow, right, children, className, bodyClass, truth, id, collapsible = true, variant = 'canvas', tone = 'warn' }: { title?: ReactNode; eyebrow?: ReactNode; right?: ReactNode; children: ReactNode; className?: string; bodyClass?: string; truth?: Truth; id?: string; collapsible?: boolean; variant?: PanelVariant; tone?: CalloutTone }) {
  // The page key comes from the store when one is mounted; rendered in isolation (static tests) a panel is simply open.
  const page = useStoreOptional()?.route.page
  const folded = useCollapsed()
  const pid = id ?? (typeof title === 'string' ? title : undefined)
  const canFold = collapsible && pid != null && page != null
  const open = !canFold || !folded.includes(panelKey(page, pid))
  const slot = useRowSlot()
  useRegisterInRow(slot, collapsible ? pid : undefined)
  const sideFold = collapsible && pid != null && slot?.active
  const rail = variant === 'rail'
  const ruled = variant !== 'callout'
  const swap = useContext(TitleSwap)
  const body = <TitleSwap.Provider value={null}><div className={cx(BODY[variant], bodyClass)}>{children}</div></TitleSwap.Provider>
  return (
    <section className={cx('min-w-0', SURFACE[variant], variant === 'callout' && CALLOUT[tone], className, !open && 'self-start')}>
      {(title || eyebrow || right) && (
        <header className={cx('group/ph flex min-h-10 items-center justify-between gap-3 py-1.5 max-md:flex-wrap', rail ? 'px-0' : 'px-4 max-md:px-3',
          ruled && 'border-b transition-[border-color] duration-300', ruled && (open ? (rail ? 'border-line' : 'border-line-soft') : 'border-transparent'))}>
          <div className="flex min-w-0 flex-1 items-baseline gap-2">
            {title && (swap
              ? <div className="flex shrink-0 items-center gap-2 self-center max-md:min-w-0 max-md:max-w-full max-md:shrink">{truth && <TruthGlyph truth={truth} />}{swap}</div>
              : (
                <h3 className="flex min-w-0 max-w-full shrink-0 items-center gap-2 text-[13px] leading-[18px] font-semibold tracking-tight text-bone">
                  {truth && <TruthGlyph truth={truth} />}
                  <span className="min-w-0 truncate">{title}</span>
                </h3>
              ))}
            {eyebrow && <div onMouseEnter={fullTextTitle} className={cx('min-w-0 flex-1 truncate leading-4 [&>*]:mr-1.5 [&>*]:align-[-1px] max-md:line-clamp-2 max-md:whitespace-normal max-md:break-words', title ? 'text-[11.5px] text-dim' : 'text-[12px] font-medium text-mute')}>{eyebrow}</div>}
          </div>
          {(right || canFold) && (
            <div className="flex shrink-0 items-center gap-1 max-md:ml-auto max-md:min-w-0 max-md:shrink max-md:flex-wrap max-md:justify-end">
              {open && right && <div className="mr-1 flex min-w-0 items-center gap-2 max-md:flex-wrap max-md:[&>*]:min-w-0 max-md:[&>*]:max-w-full max-md:[&>*]:flex-wrap">{right}</div>}
              {sideFold && <SideFoldButton side={slot.side} label={pid.toLowerCase()} onClick={slot.fold} />}
              {canFold && <FoldButton open={open} label={pid.toLowerCase()} onClick={() => collapse.toggle(page as string, pid)} />}
            </div>
          )}
        </header>
      )}
      {canFold ? <Drawer open={open}>{body}</Drawer> : body}
    </section>
  )
}

/**
 * One cell of a strip panel: an Inter label over a mono figure. Mark one cell `lead` per strip; it takes more width
 * and a larger figure (28/32) while the rest stay at 18/24. `sub` is a quiet line under the figure (unit, change, source).
 */
export function StripCell({ label, sub, lead, className, children }: { label: ReactNode; sub?: ReactNode; lead?: boolean; className?: string; children: ReactNode }) {
  return (
    <div className={cx('min-w-[140px] px-4 py-3 max-md:min-w-0 max-md:border-line-soft max-md:px-3 max-md:[&:nth-child(even)]:border-l max-md:[&:nth-child(n+3)]:border-t', lead ? 'flex-[1.6]' : 'flex-1', className)}>
      <div className="truncate text-[11px] leading-4 text-dim">{label}</div>
      <div className={cx('num mt-1 text-bone max-md:break-words', lead ? 'text-[28px] leading-8 max-md:text-[24px] max-md:leading-7' : 'text-[19px] leading-6')}>{children}</div>
      {sub && <div className="mt-1 text-[11px] leading-4 text-mute">{sub}</div>}
    </div>
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
        'inline-flex items-center justify-center gap-1.5 rounded-[3px] font-medium transition-colors duration-[120ms] disabled:cursor-not-allowed disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-mute whitespace-nowrap cursor-pointer',
        size === 'sm' ? 'h-7 px-2.5 text-[11.5px]' : 'h-8 px-3 text-[12.5px]',
        'max-md:h-11 max-md:px-3.5 max-md:text-[13px]',
        variant === 'primary' && 'bg-bone text-ink hover:bg-bone/90',
        variant === 'outline' && 'border border-line2 text-bone hover:bg-hover',
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
    <div className={cx('inline-flex max-w-full items-stretch overflow-x-auto rounded-[3px] border border-line bg-ink p-[2px] [scrollbar-width:none]', size === 'sm' ? 'h-6' : 'h-7', 'max-md:h-[46px] max-md:p-0', className)} role="tablist">
      {options.map((o) => (
        <button
          key={o.id}
          title={o.title}
          role="tab"
          aria-selected={value === o.id}
          onClick={() => onChange(o.id)}
          className={cx('flex shrink-0 cursor-pointer items-center rounded-[2px] border font-medium whitespace-nowrap transition-colors duration-[120ms] focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-mute', size === 'sm' ? 'px-2 text-[11px]' : 'px-2.5 text-[12px]', 'max-md:min-w-11 max-md:justify-center max-md:px-3 max-md:text-[13px]', value === o.id ? 'border-line2 bg-hover text-bone' : 'border-transparent text-dim hover:text-mute')}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}

export function Tabs<T extends string>({ value, tabs, onChange, className }: { value: T; tabs: { id: T; label: ReactNode; count?: number }[]; onChange: (v: T) => void; className?: string }) {
  return (
    <div className={cx('flex gap-5 border-b border-line max-md:flex-wrap max-md:gap-x-4 max-md:gap-y-1', className)} role="tablist">
      {tabs.map((t) => (
        <button key={t.id} role="tab" aria-selected={value === t.id} onClick={() => onChange(t.id)} className={cx('-mb-px cursor-pointer border-b-2 pb-2.5 pt-1 text-[13px] font-medium transition-colors max-md:min-h-11', value === t.id ? 'border-bone text-bone' : 'border-transparent text-dim hover:text-mute')}>
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
    <span className={cx('text-[11px] leading-4 text-dim', className)}>
      {source}
      {time && <> · {time}</>}
    </span>
  )
}

export function Empty({ icon, title, body, action, tone = 'neutral', className }: { icon?: ReactNode; title: string; body?: ReactNode; action?: ReactNode; tone?: 'neutral' | 'warn' | 'bad'; className?: string }) {
  return (
    <div className={cx('flex flex-col items-center justify-center gap-2 rounded-[2px] border border-dashed px-6 py-8 text-center', tone === 'warn' ? 'border-warn/40' : tone === 'bad' ? 'border-down/40' : 'border-line2', className)}>
      {icon && <div className={cx(tone === 'warn' ? 'text-warn' : tone === 'bad' ? 'text-down' : 'text-dim')}>{icon}</div>}
      <div className="text-[13px] font-semibold text-bone">{title}</div>
      {body && <div className="max-w-sm text-[12px] leading-relaxed text-mute">{body}</div>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  )
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="rounded-[2px] border border-line2 bg-raise px-1 font-mono text-[10px] text-mute">{children}</kbd>
}

export function AIBlock({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cx('rounded-[2px] border border-dashed border-line2 p-3', className)}>
      <div className="mb-1.5 flex items-center gap-1.5 text-[11px] text-mute font-medium">
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

/**
 * `briefing` is the editorial headline (Global Intelligence only); `workspace` is the compact noun title for tool pages.
 * Keep `sub` to one line; longer method notes go in `about`, which opens from an info toggle after the sub. It is a
 * native <details>, so find-in-page still reaches the text.
 */
export function PageHeader({ eyebrow, title, sub, about, right, variant = 'workspace' }: { eyebrow?: ReactNode; title: ReactNode; sub?: ReactNode; about?: ReactNode; right?: ReactNode; variant?: 'briefing' | 'workspace' }) {
  const briefing = variant === 'briefing'
  right = <><ResetLayout />{right}</>
  return (
    <div className={cx('flex flex-wrap items-end justify-between gap-4', briefing ? 'mb-6' : 'mb-4')}>
      <div className="min-w-0">
        {eyebrow && <div className="mb-1 flex items-center gap-2 text-[11px] leading-4 font-medium text-dim">{eyebrow}</div>}
        {briefing
          ? <h1 className="line-clamp-2 max-w-4xl font-serif text-[32px] leading-[38px] tracking-tight text-bone">{title}</h1>
          : <h1 className="text-[24px] leading-[30px] font-semibold tracking-[-0.02em] text-bone">{title}</h1>}
        {sub && !about && <p className={cx('max-w-[760px] text-[13px] leading-5 text-mute', briefing ? 'mt-2' : 'mt-1')}>{sub}</p>}
        {about && (
          <details className={cx('group max-w-[760px] text-[13px] leading-5 text-mute', briefing ? 'mt-2' : 'mt-1')}>
            <summary className="inline cursor-pointer list-none [&::-webkit-details-marker]:hidden max-md:block max-md:min-h-11">
              {sub}
              <span className="ml-1.5 inline-flex translate-y-[2px] items-center text-dim hover:text-bone group-open:text-bone" title="About this page" aria-label="About this page"><Info size={13} /></span>
            </summary>
            <div className="mt-1.5 border-l border-line2 pl-3 text-[12px] leading-5 text-dim">{about}</div>
          </details>
        )}
      </div>
      <div className="flex min-w-0 max-w-full flex-wrap items-center gap-2 empty:hidden">{right}</div>
    </div>
  )
}

/** Footnote marker: a small superscript number beside the value it qualifies; the note is its tooltip and is listed in `Notes`. */
export function Fn({ n, note }: { n: number; note: string }) {
  return <sup tabIndex={0} title={note} aria-label={`Note ${n}: ${note}`} className="ml-0.5 cursor-help text-[9px] font-medium text-dim outline-none hover:text-bone focus-visible:text-bone">{n}</sup>
}

/** The page's caveats, written once and numbered to match their `Fn` markers. */
export function Notes({ items, className }: { items: string[]; className?: string }) {
  if (!items.length) return null
  return (
    <ol className={cx('mt-6 space-y-0.5 border-t border-line-soft pt-3 text-[11px] leading-4 text-dim', className)}>
      {items.map((t, i) => <li key={i} className="flex gap-2"><span className="w-3 shrink-0 text-right tabular-nums">{i + 1}</span><span>{t}</span></li>)}
    </ol>
  )
}

// IC and Sharpe where Emre's model comparison shows them, filled from /flopx/research/evaluation
// (`crossSection` meanIc · tStat · nDates, `tradable` sharpe · nTrades). One fetch is shared by every
// mount. A payload without those blocks renders the honest gap, never a placeholder number.
let evalCache: Promise<ResearchMetricsPayload | null> | null = null
/** The research evaluation payload, fetched once per page load; undefined while loading, null when it did not answer. */
export function useResearchEval(): ResearchMetricsPayload | null | undefined {
  const [data, setData] = useState<ResearchMetricsPayload | null | undefined>(undefined)
  useEffect(() => {
    let alive = true
    evalCache ??= fetchJson<ResearchMetricsPayload>('/flopx/research/evaluation', { timeoutMs: 30_000 }).then((r) => (r.ok ? r.data : null))
    evalCache.then((d) => alive && setData(d))
    return () => {
      alive = false
    }
  }, [])
  return data
}
/** The design's horizon ids mapped onto the evaluation's trading-day horizons. */
export const HORIZON_DAYS: Record<HorizonId, number> = { '1W': 5, '1M': 21, '3M': 63 }
const IC_MODELS: { id: string; label: string }[] = [
  { id: 'baseline', label: 'Trailing-return baseline' },
  { id: 'augmented', label: '+ Compute-market data' },
  { id: 'combined', label: '+ News event intensity' },
]
const sgn = (v: number, d: number) => `${v < 0 ? '\u2212' : '+'}${Math.abs(v).toFixed(d)}`
export const IC_SHARPE_GAP = 'This evaluation carries no IC or Sharpe yet: the backend answering predates them.'
/** IC (mean, t, dates) and Sharpe (trades) per model at one horizon, in the model-comparison grid. */
export function IcSharpeView({ payload, horizon }: { payload: ResearchMetricsPayload | null | undefined; horizon: HorizonId }) {
  const days = HORIZON_DAYS[horizon]
  const cs = payload?.crossSection
  const tr = payload?.tradable
  const present = !!payload && Array.isArray(cs) && Array.isArray(tr)
  const kinds = new Set((payload?.historyCoverage ?? []).flatMap((c) => c.inputKinds))
  const sampleOnly = kinds.size === 0 || (kinds.size === 1 && kinds.has('sample'))
  const badge = payload === undefined ? <Fresh quiet f="unavailable" label="not loaded · awaiting the FLOPX evaluation" /> : present ? <Fresh quiet f={sampleOnly ? 'illustrative' : 'historical'} label={sampleOnly ? 'Research number · sample closes' : 'Research number · observed closes'} /> : <Fresh quiet f="unavailable" label="Not in this payload" />
  return (
    <Panel bodyClass="max-md:overflow-x-auto" title="Rank skill and Sharpe by model" eyebrow={<>IC across tickers · sign-of-forecast trade {badge}</>} right={<span className="text-[11px] text-dim">{horizon} horizon · {days} trading days</span>}>
      {!present ? (
        <div className="py-3 text-[12px] text-mute" data-ic-sharpe="gap">{payload === undefined ? 'Loading the research evaluation.' : payload === null ? 'The research evaluation did not answer, so no IC or Sharpe is shown.' : IC_SHARPE_GAP}</div>
      ) : (
        <>
          <div className="grid h-8 grid-cols-[1.6fr_repeat(4,minmax(0,1fr))] items-center gap-x-3 border-b border-line text-[11px] font-medium text-dim max-md:min-w-[480px] max-md:[&>:first-child]:sticky max-md:[&>:first-child]:left-0 max-md:[&>:first-child]:z-[1] max-md:[&>:first-child]:bg-panel">
            <span>Model</span><span className="text-right">IC</span><span className="text-right">t · dates</span><span className="text-right">Sharpe</span><span className="text-right">trades</span>
          </div>
          {IC_MODELS.map((m, i) => {
            const c = cs!.find((r) => r.horizonDays === days && r.model === m.id)
            const t = tr!.find((r) => r.horizonDays === days && r.model === m.id)
            const scored = (c?.nDates ?? 0) > 0 || (t?.nTrades ?? 0) > 0
            return (
              <div key={m.id} data-row={`${days}-${m.id}`} className={cx('grid grid-cols-[1.6fr_repeat(4,minmax(0,1fr))] items-center gap-x-3 border-b border-line-soft py-1.5 max-md:min-w-[480px] max-md:[&>:first-child]:sticky max-md:[&>:first-child]:left-0 max-md:[&>:first-child]:z-[1] max-md:[&>:first-child]:bg-panel', !scored && 'opacity-70')}>
                <span className="text-[12px]"><span className="num mr-2 text-[10.5px] text-dim">{String.fromCharCode(65 + i)}</span>{m.label}</span>
                <span className="num text-right text-[12px]">{c?.meanIc == null ? '\u2014' : sgn(c.meanIc, 3)}</span>
                <span className="num text-right text-[11px] text-mute">{c && c.nDates > 0 ? `${c.tStat == null ? '\u2014' : `t ${c.tStat.toFixed(2)}`} · ${c.nDates}` : 'not scored'}</span>
                <span className="num text-right text-[12px]">{t?.sharpe == null ? '\u2014' : sgn(t.sharpe, 2)}</span>
                <span className="num text-right text-[11px] text-mute">{t && t.nTrades > 0 ? t.nTrades : '\u2014'}</span>
              </div>
            )
          })}
          <div className="mt-2 font-mono text-[10px] text-dim">no costs, research number</div>
        </>
      )}
    </Panel>
  )
}
/** Fetches (shared) and renders IcSharpeView. */
export function IcSharpe({ horizon }: { horizon: HorizonId }) {
  return <IcSharpeView payload={useResearchEval()} horizon={horizon} />
}

// ---------------- Phone bottom sheets (m-prims) ----------------
// Dialogs, the Ask FLOPX panel, the inspector and the top-bar menus become bottom sheets under 768px. Each piece
// below is phone-only: the class strings are all `max-md:` variants and the elements render only when
// useIsPhone() is true, so the desktop markup stays byte for byte what it was.

/** A full-height sheet: fixed to the viewport, the full width, its top just under the safe area. */
export const SHEET_FULL = 'max-md:fixed max-md:inset-x-0 max-md:bottom-0 max-md:top-[max(12px,env(safe-area-inset-top))] max-md:right-0 max-md:left-0 max-md:z-[95] max-md:h-auto max-md:max-h-none max-md:w-full max-md:max-w-none max-md:rounded-b-none max-md:border-x-0 max-md:border-b-0 max-md:border-t max-md:border-line2 max-md:pb-[env(safe-area-inset-bottom)] max-md:shadow-[0_-12px_32px_rgba(0,0,0,0.35)]'
/** A content-height sheet for menus and popovers, capped at most of the screen and scrolling inside. */
export const SHEET_MENU = 'max-md:fixed max-md:inset-x-0 max-md:top-auto max-md:right-0 max-md:bottom-0 max-md:left-0 max-md:z-[96] max-md:max-h-[85dvh] max-md:w-full max-md:overflow-y-auto max-md:rounded-b-none max-md:border-x-0 max-md:border-b-0 max-md:pb-[calc(16px+env(safe-area-inset-bottom))] max-md:shadow-[0_-12px_32px_rgba(0,0,0,0.35)]'
/** Grows an existing close button to a 44x44 touch target on a phone without moving its glyph. */
export const SHEET_CLOSE = 'max-md:flex max-md:size-11 max-md:items-center max-md:justify-center max-md:-my-2 max-md:-mr-2.5'

/** Dims the page behind a phone sheet; a tap on it closes the sheet. */
export function SheetBackdrop({ onClose, z = 'z-[94]' }: { onClose: () => void; z?: string }) {
  return <div aria-hidden="true" data-sheet-backdrop className={cx('m-sheet-backdrop fixed inset-0 bg-black/55 md:hidden', z)} onClick={onClose} />
}

/** Drag handle on top of a phone sheet: drag it down past 96px (or flick) to close, release earlier to snap back. */
export function SheetHandle({ sheet, onClose }: { sheet: RefObject<HTMLElement | null>; onClose: () => void }) {
  const drag = useRef<{ y: number; t: number } | null>(null)
  const move = (dy: number) => { const el = sheet.current; if (el) el.style.transform = dy > 0 ? `translateY(${dy}px)` : '' }
  const end = (y: number | null) => {
    const d = drag.current
    drag.current = null
    const el = sheet.current
    if (el) el.style.transition = 'transform 180ms ease-out'
    if (!d || y == null) return move(0)
    const dy = y - d.y
    const fast = dy > 40 && dy / Math.max(1, performance.now() - d.t) > 0.6
    if (dy > 96 || fast) onClose()
    else move(0)
  }
  return (
    <div data-sheet-handle aria-hidden="true" className="flex h-6 w-full shrink-0 cursor-grab touch-none items-center justify-center md:hidden"
      onPointerDown={(e) => { drag.current = { y: e.clientY, t: performance.now() }; e.currentTarget.setPointerCapture(e.pointerId); const el = sheet.current; if (el) el.style.transition = 'none' }}
      onPointerMove={(e) => { if (drag.current) move(e.clientY - drag.current.y) }}
      onPointerUp={(e) => end(e.clientY)}
      onPointerCancel={() => end(null)}>
      <span className="h-1 w-10 rounded-full bg-line2" />
    </div>
  )
}
