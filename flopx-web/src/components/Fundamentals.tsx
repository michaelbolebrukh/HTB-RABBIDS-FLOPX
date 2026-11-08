// The signed-in half of the Securities fundamentals panel: the archived WRDS Compustat quarterlies
// (revenue, capex, capex intensity) that /flopx/companies/{t} carries only with a token (WF7 #2).
// Renders nothing without them, so the anonymous panel is exactly what it was. The SEC-derived
// revenue growth line stays where it is in the page; this block sits below it.
import type { Quarter, Quarterly } from '@/live/adapt-w2'
import { fmtDay } from '@/live/provenance'
import { Fresh } from './ui'

// The badge and its audience line are split because the full "WRDS Compustat · archived · signed-in
// readers" does not fit a 9 px nowrap badge inside the 290 px Fundamentals panel at 1440.
export const WRDS_BADGE = 'WRDS Compustat · archived'
export const WRDS_AUDIENCE = 'signed-in readers'
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** "Q2 · Jul 2026" (fiscal quarter when the row states one, else the period end alone). */
export function quarterLabel(q: Quarter): string {
  const d = new Date(q.periodEnd)
  const end = `${MON[d.getUTCMonth()]} ${d.getUTCFullYear()}`
  return q.fq ? `Q${q.fq} · ${end}` : end
}

/** $96.2B / $977M / —, never an estimate for a missing side. */
export function fmtUsd(v: number | null): string {
  if (v == null || !Number.isFinite(v)) return '—'
  const a = Math.abs(v)
  const s = v < 0 ? '−' : ''
  if (a >= 1e9) return `${s}$${(a / 1e9).toFixed(1)}B`
  if (a >= 1e6) return `${s}$${(a / 1e6).toFixed(0)}M`
  return `${s}$${a.toFixed(0)}`
}

export const fmtIntensity = (v: number | null) => (v == null ? '—' : `${(v * 100).toFixed(1)}%`)

/** The quarterly table, last `n` fiscal quarters, in the page's own table vocabulary. */
export function QuarterlyTable({ q, n = 8 }: { q: Quarterly; n?: number }) {
  const rows = q.quarters.slice(-n)
  return (
    <div data-testid="quarterly-fundamentals" className="mt-3 border-t border-line pt-2.5">
      <div className="mb-1 flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="eyebrow">Quarterly</span>
        <Fresh f={q.fresh.freshness} label={WRDS_BADGE} />
      </div>
      <div className="mb-1.5 text-[10.5px] text-dim">Served to {WRDS_AUDIENCE} · last {rows.length} of {q.quarters.length} fiscal quarters</div>
      <table className="w-full text-[11px]">
        <thead>
          <tr className="text-[10.5px] text-dim font-medium">
            {['Quarter', 'Revenue', 'Capex', 'Capex / rev'].map((h, i) => <th key={h} className={i ? 'py-1 text-right font-normal' : 'py-1 text-left font-normal'}>{h}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.periodEnd} className="border-t border-line/60">
              <td className="py-[3px] font-mono text-[10.5px] text-mute">{quarterLabel(r)}</td>
              <td className="num py-[3px] text-right">{fmtUsd(r.revenue)}</td>
              <td className="num py-[3px] text-right">{fmtUsd(r.capex)}</td>
              <td className="num py-[3px] text-right">{fmtIntensity(r.intensity)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="mt-1.5 text-[10.5px] text-dim">Compustat fiscal quarters to {fmtDay(q.through)}; capex derived from the year-to-date figure; a quarter without a revenue row shows no ratio. Revenue growth above stays the SEC filing figure.</div>
    </div>
  )
}

/** The branch the page renders: the table for a signed-in reader whose detail carried the rows, nothing otherwise. */
export function QuarterlySection({ quarterly }: { quarterly: Quarterly | null | undefined }) {
  return quarterly ? <QuarterlyTable q={quarterly} /> : null
}
