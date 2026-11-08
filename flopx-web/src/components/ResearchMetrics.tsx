// The two research numbers the per-ticker harness never gives, and the history it stood on, read from
// /flopx/research/evaluation: `crossSection` (the mean Spearman IC across tickers per origin date),
// `tradable` (the Sharpe of the simplest sign-of-forecast trade, no costs) and `historyCoverage`
// (first and last close, trading days, origins scored per ticker). Every figure is the backend's own
// and carries provenance `research`; nothing here changes a verdict. A payload from a backend that
// predates these blocks renders the honest gap, never a placeholder number.
import { useEffect, useState } from 'react'
import { fetchJson } from '@/live/client'
import { cx, Fresh, Panel } from './ui'

export interface CrossSectionWire {
  horizonDays: number
  model: string
  nDates: number
  meanIc: number | null
  tStat: number | null
  shareDatesFivePlus: number | null
  tickers: number
  provenance: 'research'
  note: string
}
export interface TradableWire {
  horizonDays: number
  model: string
  nTrades: number
  sharpe: number | null
  meanExcessLogReturn: number | null
  hitRate: number | null
  tickers: number
  provenance: 'research'
  note: string
}
export interface HistoryCoverageWire {
  ticker: string
  firstClose: string | null
  lastClose: string | null
  tradingDays: number
  spanYears: number | null
  originsScored: number
  originsByHorizon: Record<string, number>
  inputKinds: string[]
}
/** The slice of the evaluation payload this panel reads; the three blocks are optional because an older backend omits them. */
export interface ResearchMetricsPayload {
  horizons: number[]
  crossSection?: CrossSectionWire[]
  tradable?: TradableWire[]
  historyCoverage?: HistoryCoverageWire[]
}

export const NOT_LOADED = 'not loaded · awaiting the FLOPX evaluation'
export const NOT_IN_PAYLOAD = 'This evaluation carries no IC or Sharpe yet: the backend answering predates them.'
export const NOT_SCORED = 'not scored'
const MODEL_LABEL: Record<string, string> = { baseline: 'Trailing-return baseline', augmented: '+ Compute-market data', combined: '+ News event intensity' }
const MODELS = ['baseline', 'augmented', 'combined']

const signed = (v: number, d: number) => `${v < 0 ? '−' : '+'}${Math.abs(v).toFixed(d)}`
/** "+0.174" or "—": a mean IC, three decimals, signed. */
export const fmtIc = (v: number | null | undefined) => (v == null ? '—' : signed(v, 3))
/** "−0.36" or "—": an annualised Sharpe, two decimals, signed. */
export const fmtSharpe = (v: number | null | undefined) => (v == null ? '—' : signed(v, 2))
/** "t 3.48" or "": the t-statistic beside its mean. */
export const fmtT = (v: number | null | undefined) => (v == null ? '' : `t ${v.toFixed(2)}`)
/** "52%" or "—". */
export const fmtShare = (v: number | null | undefined) => (v == null ? '—' : `${Math.round(v * 100)}%`)
/** "2026-05-11" from an ISO timestamp, or "—". */
export const fmtDay = (iso: string | null | undefined) => (iso ? iso.slice(0, 10) : '—')
/** Horizon label: 1d, 5d, 21d, 63d. */
export const hzLabel = (h: number) => `${h}d`

/** What the panel's badge says, read off the closes the evaluation ran on. */
export function basisOf(coverage: HistoryCoverageWire[] | undefined): { f: 'illustrative' | 'historical'; label: string } {
  const kinds = new Set((coverage ?? []).flatMap((c) => c.inputKinds))
  const sampleOnly = kinds.size === 0 || (kinds.size === 1 && kinds.has('sample'))
  return sampleOnly ? { f: 'illustrative', label: 'Research number · sample closes' } : { f: 'historical', label: 'Research number · observed closes' }
}

/** The payload rendered; `payload` null or undefined means the evaluation has not answered. */
export function ResearchMetricsView({ payload }: { payload: ResearchMetricsPayload | null | undefined }) {
  const cs = payload?.crossSection
  const tr = payload?.tradable
  const cov = payload?.historyCoverage
  const present = !!payload && Array.isArray(cs) && Array.isArray(tr) && Array.isArray(cov)
  const basis = basisOf(cov)
  const badge = !payload ? <Fresh quiet f="unavailable" label={NOT_LOADED} /> : present ? <Fresh quiet f={basis.f} label={basis.label} /> : <Fresh quiet f="unavailable" label="Not in this payload" />
  const horizons = payload?.horizons ?? []
  const csAt = (h: number, m: string) => cs?.find((r) => r.horizonDays === h && r.model === m)
  const trAt = (h: number, m: string) => tr?.find((r) => r.horizonDays === h && r.model === m)
  const icNote = cs?.find((r) => r.note)?.note
  const sharpeNote = tr?.find((r) => r.note)?.note
  return (
    <Panel bodyClass="max-md:overflow-x-auto" title="Rank skill and a tradable Sharpe" eyebrow={<>IC across tickers · sign-of-forecast trade · how much history {badge}</>} right={present && cov ? <span className="font-mono text-[10px] text-dim">{cov.length} tickers · {horizons.map(hzLabel).join(' · ')}</span> : null}>
      {!payload ? (
        <div className="py-3 font-mono text-[11px] text-dim">{NOT_LOADED}</div>
      ) : !present ? (
        <div className="py-3 text-[12px] text-mute">{NOT_IN_PAYLOAD}</div>
      ) : (
        <>
          <div className="grid grid-cols-[auto_1.6fr_repeat(6,minmax(0,1fr))] gap-x-3 max-md:min-w-[620px] border-b border-line pb-1.5 text-[11px] text-dim font-medium">
            <span>h</span><span>Model</span><span className="text-right">IC (mean)</span><span className="text-right">t</span><span className="text-right">dates · 5+</span><span className="text-right">Sharpe</span><span className="text-right">trades</span><span className="text-right">hit</span>
          </div>
          {horizons.flatMap((h) =>
            MODELS.map((m) => {
              const c = csAt(h, m)
              const t = trAt(h, m)
              const scored = (c?.nDates ?? 0) > 0 || (t?.nTrades ?? 0) > 0
              return (
                <div key={`${h}-${m}`} className={cx('grid grid-cols-[auto_1.6fr_repeat(6,minmax(0,1fr))] items-center gap-x-3 max-md:min-w-[620px] border-b border-line/60 py-2 text-[12px]', !scored && 'opacity-60')} data-row={`${h}-${m}`}>
                  <span className="font-mono text-[10px] text-dim">{hzLabel(h)}</span>
                  <span>{MODEL_LABEL[m] ?? m}</span>
                  <span className="num text-right">{fmtIc(c?.meanIc)}</span>
                  <span className="num text-right text-mute">{c?.tStat == null ? '—' : fmtT(c.tStat)}</span>
                  <span className="num text-right text-mute">{c && c.nDates > 0 ? `${c.nDates} · ${fmtShare(c.shareDatesFivePlus)}` : NOT_SCORED}</span>
                  <span className="num text-right">{fmtSharpe(t?.sharpe)}</span>
                  <span className="num text-right text-mute">{t && t.nTrades > 0 ? `${t.nTrades}` : '—'}</span>
                  <span className="num text-right text-mute">{fmtShare(t?.hitRate)}</span>
                </div>
              )
            }),
          )}
          <div className="mt-2 space-y-1 font-mono text-[10px] text-dim">
            {icNote && <div>IC: {icNote}.</div>}
            {sharpeNote && <div>Sharpe: {sharpeNote}.</div>}
          </div>
          <div className="mt-4 mb-1 text-[11px] font-medium text-dim">Enough history? What each ticker was scored on</div>
          <div className="grid grid-cols-[auto_1.4fr_repeat(3,minmax(0,1fr))_1.6fr] gap-x-3 max-md:min-w-[620px] border-b border-line pb-1.5 text-[11px] text-dim font-medium">
            <span>Ticker</span><span>Closes</span><span className="text-right">trading days</span><span className="text-right">years</span><span className="text-right">origins 1d</span><span className="text-right">origins by horizon</span>
          </div>
          {cov.map((c) => (
            <div key={c.ticker} className="grid grid-cols-[auto_1.4fr_repeat(3,minmax(0,1fr))_1.6fr] items-center gap-x-3 max-md:min-w-[620px] border-b border-line/60 py-1.5 text-[12px]" data-ticker={c.ticker}>
              <span className="font-mono">{c.ticker}</span>
              <span className="font-mono text-[10.5px] text-mute">{fmtDay(c.firstClose)} → {fmtDay(c.lastClose)}</span>
              <span className="num text-right">{c.tradingDays}</span>
              <span className="num text-right text-mute">{c.spanYears == null ? '—' : c.spanYears.toFixed(1)}</span>
              <span className="num text-right">{c.originsScored}</span>
              <span className="num text-right font-mono text-[10.5px] text-mute">{horizons.map((h) => `${hzLabel(h)} ${c.originsByHorizon[String(h)] ?? 0}`).join(' · ')}</span>
            </div>
          ))}
        </>
      )}
    </Panel>
  )
}

/** Fetches the evaluation itself when no payload is handed in, so the page needs one line and no adapter change. */
export function ResearchMetrics({ payload }: { payload?: ResearchMetricsPayload | null }) {
  const [fetched, setFetched] = useState<ResearchMetricsPayload | null>(null)
  const ownFetch = payload === undefined
  useEffect(() => {
    if (!ownFetch) return
    let alive = true
    fetchJson<ResearchMetricsPayload>('/flopx/research/evaluation', { timeoutMs: 30_000 }).then((r) => {
      if (alive && r.ok) setFetched(r.data)
    })
    return () => {
      alive = false
    }
  }, [ownFetch])
  return <ResearchMetricsView payload={ownFetch ? fetched : payload} />
}
