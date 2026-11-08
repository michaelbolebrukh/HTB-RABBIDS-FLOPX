import type { ReactNode } from 'react'
import { ChevronDown, ChevronLeft, ChevronRight, ChevronUp } from 'lucide-react'
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
 * Round chevron that sits on a panel's inner edge (kiri style). It points toward the panel to hide it
 * and away from it to bring it back, and stays put when the panel is collapsed.
 */
export function EdgeToggle({ edge, open, label, onClick }: { edge: 'top' | 'bottom' | 'left' | 'right'; open: boolean; label: string; onClick: () => void }) {
  // Direction the panel moves when it hides: a right-edge toggle belongs to a left panel, which slides left.
  const hideDir = { right: 'left', left: 'right', top: 'down', bottom: 'up' }[edge]
  const dir = open ? hideDir : ({ left: 'right', right: 'left', up: 'down', down: 'up' } as const)[hideDir as 'left' | 'right' | 'up' | 'down']
  const Icon = { left: ChevronLeft, right: ChevronRight, up: ChevronUp, down: ChevronDown }[dir as 'left' | 'right' | 'up' | 'down']
  // Open: straddle the panel edge. Collapsed: sit just inside the map so the chevron is never clipped.
  const pos = (open
    ? { right: '-right-3', left: '-left-3', top: '-top-3', bottom: '-bottom-3' }
    : { right: 'left-1.5', left: 'right-1.5', top: '-top-8', bottom: '-bottom-3' })[edge]
  const axis = edge === 'left' || edge === 'right' ? 'top-1/2 -translate-y-1/2' : 'left-1/2 -translate-x-1/2'
  const text = `${open ? 'Hide' : 'Show'} ${label}`
  return (
    <button type="button" onClick={onClick} aria-expanded={open} aria-label={text} title={text}
      className={cx('absolute z-30 flex size-6 cursor-pointer items-center justify-center rounded-full border border-line2 bg-panel text-mute shadow-[0_2px_8px_rgba(0,0,0,0.25)] transition-colors hover:border-mute hover:text-bone focus-visible:outline focus-visible:outline-1 focus-visible:outline-fct', pos, axis)}>
      <Icon size={14} strokeWidth={2} />
    </button>
  )
}

/** The same round chevron as EdgeToggle, laid out inline (panel headers). Points up to fold, down to unfold. */
export function FoldButton({ open, label, onClick }: { open: boolean; label: string; onClick: () => void }) {
  const text = `${open ? 'Hide' : 'Show'} ${label}`
  const Icon = open ? ChevronUp : ChevronDown
  return (
    <button type="button" onClick={onClick} aria-expanded={open} aria-label={text} title={text}
      className="flex size-6 shrink-0 cursor-pointer items-center justify-center rounded-full border border-line2 bg-panel text-mute transition-colors hover:border-mute hover:text-bone focus-visible:outline focus-visible:outline-1 focus-visible:outline-fct">
      <Icon size={14} strokeWidth={2} />
    </button>
  )
}
