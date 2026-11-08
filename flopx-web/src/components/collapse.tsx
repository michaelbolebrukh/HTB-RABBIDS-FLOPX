import { Children, createContext, useCallback, useContext, useLayoutEffect, useState, useSyncExternalStore, type ReactNode, type Ref } from 'react'
import { ChevronDown, ChevronLeft, ChevronRight, ChevronUp } from 'lucide-react'
import { collapse, RAIL, rowTemplate, sideKey, useCollapsed, panelKey } from '@/lib/layout'
import { useStore } from '@/lib/store'
import { cx } from './ui'

/** Folds its content to zero height and back. Content stays mounted; `inert` keeps it out of tab order while folded. */
export function Drawer({ open, children }: { open: boolean; children: ReactNode }) {
  return (
    <div className={cx('grid shrink-0 transition-[grid-template-rows] duration-300 ease-out motion-reduce:transition-none', open ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]')}>
      <div inert={!open} className="min-h-0 overflow-hidden">{children}</div>
    </div>
  )
}

/**
 * Small square chevron that sits on a panel's inner edge (kiri style). It points toward the panel to hide it
 * and away from it to bring it back, and stays put when the panel is collapsed. Pass `rail` when the folded
 * panel keeps a visible strip, so the chevron keeps straddling its edge.
 */
export function EdgeToggle({ edge, open, label, onClick, rail }: { edge: 'top' | 'bottom' | 'left' | 'right'; open: boolean; label: string; onClick: () => void; rail?: boolean }) {
  // Direction the panel moves when it hides: a right-edge toggle belongs to a left panel, which slides left.
  const hideDir = { right: 'left', left: 'right', top: 'down', bottom: 'up' }[edge]
  const dir = open ? hideDir : ({ left: 'right', right: 'left', up: 'down', down: 'up' } as const)[hideDir as 'left' | 'right' | 'up' | 'down']
  const Icon = { left: ChevronLeft, right: ChevronRight, up: ChevronUp, down: ChevronDown }[dir as 'left' | 'right' | 'up' | 'down']
  // Open (or folded to a visible rail): straddle the panel edge. Collapsed to nothing: sit just inside the map so the chevron is never clipped.
  const pos = (open || rail
    ? { right: '-right-3', left: '-left-3', top: '-top-3', bottom: '-bottom-3' }
    : { right: 'left-1.5', left: 'right-1.5', top: '-top-8', bottom: '-bottom-3' })[edge]
  const axis = edge === 'left' || edge === 'right' ? 'top-1/2 -translate-y-1/2' : 'left-1/2 -translate-x-1/2'
  const text = `${open ? 'Hide' : 'Show'} ${label}`
  return (
    <button type="button" onClick={onClick} aria-expanded={open} aria-label={text} title={text}
      className={cx('absolute z-30 flex size-6 cursor-pointer items-center justify-center rounded-[3px] border border-line2 bg-panel text-dim transition-colors duration-[120ms] hover:text-bone focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-mute', pos, axis)}>
      <Icon size={14} strokeWidth={2} />
    </button>
  )
}

/** Unframed 14px chevron in a 24px hit target: dim at rest, bone on hover, a neutral outline on keyboard focus. */
const chevron = 'flex size-6 shrink-0 cursor-pointer items-center justify-center rounded-[3px] text-dim transition-[color,opacity] duration-[120ms] hover:text-bone focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-mute'

/** Header fold toggle (panels, Foldable): points up to fold, down to unfold. */
export function FoldButton({ open, label, onClick }: { open: boolean; label: string; onClick: () => void }) {
  const text = `${open ? 'Hide' : 'Show'} ${label}`
  const Icon = open ? ChevronUp : ChevronDown
  return (
    <button type="button" onClick={onClick} aria-expanded={open} aria-label={text} title={text} className={chevron}>
      <Icon size={14} strokeWidth={2} />
    </button>
  )
}

/**
 * Toggle for a sideways fold, laid out inline like FoldButton. `side` is the edge the panel folds toward.
 * It stays hidden until its `group/ph` ancestor (the panel header) is hovered or holds focus, and always shows
 * on devices without hover.
 */
export function SideFoldButton({ side, label, onClick }: { side: 'left' | 'right'; label: string; onClick: () => void }) {
  const Icon = side === 'left' ? ChevronLeft : ChevronRight
  return (
    <button type="button" onClick={onClick} aria-label={`Fold ${label} aside`} title={`Fold ${label} aside`}
      className={cx(chevron, 'opacity-0 group-hover/ph:opacity-100 group-focus-within/ph:opacity-100 focus-visible:opacity-100 [@media(hover:none)]:opacity-100')}>
      <Icon size={14} strokeWidth={2} />
    </button>
  )
}

/** What a Panel inside a Row cell needs: whether the row is side by side now, which edge to fold toward, and how. */
interface RowSlot { active: boolean; side: 'left' | 'right'; register: (label: string) => () => void; fold: () => void }
const RowSlotContext = createContext<RowSlot | null>(null)
export const useRowSlot = () => useContext(RowSlotContext)

const BREAKPOINT = { lg: '64rem', xl: '80rem', '2xl': '96rem' } as const

/** True while the viewport is at least `w` wide (CSS length). */
export function useMinWidth(w: string) {
  const q = `(min-width: ${w})`
  return useSyncExternalStore(
    (cb) => { const m = window.matchMedia(q); m.addEventListener('change', cb); return () => m.removeEventListener('change', cb) },
    () => window.matchMedia(q).matches,
    () => true,
  )
}

/**
 * A row of panels, one column below the `at` breakpoint and side by side above it. Each cell can fold
 * sideways into a rail (the others take its width) and the fold is remembered per page. Panels in a cell
 * add the fold chevron to their header and name the rail; a cell without a Panel needs `labels[i]`.
 * Below the breakpoint a folded cell shows as a thin bar instead.
 */
export function Row({ cols, at = 'xl', labels = [], gap = 'gap-5', className, ref, children }: { cols: (number | string)[]; at?: keyof typeof BREAKPOINT; labels?: (string | undefined)[]; gap?: string; className?: string; ref?: Ref<HTMLDivElement>; children: ReactNode }) {
  const { route } = useStore()
  const keys = useCollapsed()
  const wide = useMinWidth(BREAKPOINT[at])
  const cells = Children.toArray(children)
  const [named, setNamed] = useState<Record<number, string>>({})
  const label = (i: number) => labels[i] ?? named[i]
  const folded = cells.map((_, i) => { const l = label(i); return l != null && keys.includes(panelKey(route.page, sideKey(l))) })
  // The last cell never folds when all others are, so the row cannot vanish.
  const shown = folded.map((f, i) => f && !(folded.every(Boolean) && i === cells.length - 1))
  return (
    <div ref={ref} className={cx('grid grid-cols-1', gap, 'transition-[grid-template-columns] duration-300 ease-out motion-reduce:transition-none', className)}
      style={wide ? { gridTemplateColumns: rowTemplate(cols, shown) } : undefined}>
      {cells.map((c, i) => (
        <Cell key={i} index={i} count={cells.length} active={wide} folded={shown[i]} label={label(i)} explicit={labels[i] != null} page={route.page} setNamed={setNamed}>{c}</Cell>
      ))}
    </div>
  )
}

function Cell({ index, count, active, folded, label, explicit, page, setNamed, children }: { index: number; count: number; active: boolean; folded: boolean; label?: string; explicit: boolean; page: string; setNamed: (f: (n: Record<number, string>) => Record<number, string>) => void; children: ReactNode }) {
  const side: 'left' | 'right' = index === count - 1 && count > 1 ? 'right' : 'left'
  const toggle = useCallback(() => { if (label) collapse.toggle(page, sideKey(label)) }, [label, page])
  // The first Panel to register names the cell; later Panels in a stacked column share that name.
  const register = useCallback((l: string) => {
    setNamed((n) => (n[index] != null ? n : { ...n, [index]: l }))
    return () => setNamed((n) => { if (n[index] !== l) return n; const { [index]: _, ...rest } = n; return rest })
  }, [index, setNamed])
  const slot = { active: active && !folded, side, register, fold: toggle }
  const body = <RowSlotContext.Provider value={explicit ? { ...slot, register: () => () => {} } : slot}>{children}</RowSlotContext.Provider>
  if (folded && label) {
    return (
      <>
        <FoldRail label={label} side={side} vertical={active} onOpen={toggle} />
        <div hidden>{body}</div>
      </>
    )
  }
  if (!explicit) return body
  return (
    <div className="group/ph relative min-w-0">
      {body}
      {active && label && <div className={cx('absolute top-2', side === 'left' ? 'right-2' : 'left-2')}><SideFoldButton side={side} label={label.toLowerCase()} onClick={toggle} /></div>}
    </div>
  )
}

/** A folded cell: a rail with the title written down its length (side by side) or a thin bar (stacked). Click to unfold. */
function FoldRail({ label, side, vertical, onOpen }: { label: string; side: 'left' | 'right'; vertical: boolean; onOpen: () => void }) {
  const Icon = vertical ? (side === 'left' ? ChevronRight : ChevronLeft) : ChevronDown
  return (
    <button type="button" onClick={onOpen} aria-expanded={false} aria-label={`Show ${label.toLowerCase()}`} title={`Show ${label.toLowerCase()}`}
      style={vertical ? { width: RAIL } : undefined}
      className={cx('group flex min-w-0 cursor-pointer items-center gap-2 overflow-hidden rounded-[2px] border border-line bg-panel text-mute transition-colors duration-[120ms] hover:bg-hover hover:text-bone focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-mute', vertical ? 'min-h-[160px] flex-col self-stretch py-2' : 'h-10 px-3')}>
      <span className="flex size-6 shrink-0 items-center justify-center text-dim group-hover:text-bone"><Icon size={14} strokeWidth={2} /></span>
      <span className={cx('truncate text-[12px] font-medium', vertical && '[writing-mode:vertical-rl]')}>{label}</span>
    </button>
  )
}

/** Registers a Panel's title with its Row cell, if it has one. */
export function useRegisterInRow(slot: RowSlot | null, label: string | undefined) {
  const register = slot?.register
  useLayoutEffect(() => (register && label ? register(label) : undefined), [register, label])
}

/**
 * Fold-up for a block that is not a Panel (stat strips, filter bars, loose boxes): a thin header row with
 * the label and a FoldButton above the block, which keeps its own styling. Remembered per page like Panel.
 */
export function Foldable({ label, id, className, children }: { label: ReactNode; id?: string; className?: string; children: ReactNode }) {
  const { route } = useStore()
  const keys = useCollapsed()
  const pid = id ?? (typeof label === 'string' ? label : 'block')
  const open = !keys.includes(panelKey(route.page, pid))
  return (
    <div className={cx('min-w-0', className)}>
      <div className="mb-1 flex min-h-7 items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2 text-[11.5px] leading-4 font-medium text-dim">{label}</div>
        <FoldButton open={open} label={pid.toLowerCase()} onClick={() => collapse.toggle(route.page, pid)} />
      </div>
      <Drawer open={open}>{children}</Drawer>
    </div>
  )
}
