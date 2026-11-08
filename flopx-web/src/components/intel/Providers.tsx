// Layer 2 panels (providers): the same open weights served by many hosts at many prices.
import { useMemo, useState, type ReactNode } from 'react'
import { Panel, Seg, TruthTag, cx } from '@/components/ui'
import { useWidth } from '@/components/charts'
import { useChartStyle } from '@/lib/chartStyle'
import {
  FEATURES, bits, bitsLabel, blended, decompose, leaderboard, median, pctEffect, route, spreadFor, spreads, standardLive,
  type EndpointRow, type Spread,
} from '@/lib/tokenmarket'
import { FIT, MODEL, SNAP, times, usd } from '@/components/intel/shared'

const SPREADS = spreads(SNAP.endpoints)
const FIT_ROW = new Map(FIT.rows.map((r) => [r.e, r]))
const name = (id: string) => MODEL.get(id)?.name ?? id

/** Context window: 1.05M, 262K. */
export const ctxLabel = (n: number) => (n >= 1e6 ? `${(n / 1e6).toFixed(2)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}K` : String(n))
/** Signed whole percent with a true minus sign: +12%, −39%. */
export const signedPct = (v: number, d = 0) => `${v >= 0 ? '+' : '−'}${Math.abs(v).toFixed(d)}%`
/** Signed number with a true minus sign. */
const signed = (v: number, d = 0) => `${v >= 0 ? '+' : '−'}${Math.abs(v).toFixed(d)}`
/** Total spend in $: $12,480 · $41.25 · $0.315. */
export const money = (v: number) => (v >= 1000 ? `$${Math.round(v).toLocaleString('en-US')}` : v >= 1 ? `$${v.toFixed(2)}` : usd(v))
/** Listed price vs the hedonic fair price, in %; null when the endpoint is outside the fit. */
const vsFair = (e: EndpointRow) => { const r = FIT_ROW.get(e); return r ? (blended(e) / r.fair - 1) * 100 : null }
const samePrec = (s: Spread) => (s.samePrecision ? `${times(s.samePrecision.spread)} ${s.samePrecision.bits} bit` : '—')

const INPUT = 'h-7 rounded-[4px] border border-line2 bg-ink px-2 font-mono text-[11px] text-bone outline-none focus:border-mute'
const TH = 'px-2 py-1.5 text-left font-mono text-[9.5px] font-normal tracking-wider text-dim uppercase whitespace-nowrap'
const TD = 'px-2 py-1.5 whitespace-nowrap'

function Foot({ children }: { children: ReactNode }) {
  return <div className="mt-3 text-[10.5px] leading-relaxed text-dim">{children}</div>
}

// ── Spread ladder ────────────────────────────────────────────────────────────

const LADDER_TICKS = [0.03, 0.1, 0.3, 1, 3, 10]

export function SpreadLadder({ onRoute }: { onRoute: (modelId: string) => void }) {
  const [view, setView] = useState<'top' | 'all'>('top')
  const rows = view === 'top' ? SPREADS.slice(0, 12) : SPREADS
  const [ref, tw] = useWidth<HTMLDivElement>()
  const { fontClass } = useChartStyle()
  const prices = SNAP.endpoints.map(blended).filter((v) => v > 0)
  const lo = Math.log(Math.min(0.03, Math.min(...prices) / 1.15)), hi = Math.log(Math.max(10, Math.max(...prices) * 1.05))
  const pad = 6
  const x = (v: number) => pad + ((Math.log(v) - lo) / (hi - lo)) * Math.max(1, tw - 2 * pad)
  const grid = 'grid grid-cols-[minmax(0,11rem)_minmax(0,1fr)_3.25rem_5.5rem] items-center gap-x-3'
  return (
    <Panel
      title="Spread ladder"
      eyebrow="Same open weights across providers · blended $/M · log scale"
      right={<Seg size="sm" value={view} onChange={setView} options={[{ id: 'top', label: 'Top 12' }, { id: 'all', label: `All ${SPREADS.length} models` }]} />}
    >
      <div className={cx(grid, 'border-b border-line pb-1')}>
        <span className={TH.replace('px-2 ', '')}>Model</span>
        <div ref={ref} className="relative h-4">
          <svg width={tw} height={16} className="absolute inset-0 overflow-visible">
            {LADDER_TICKS.map((t) => (
              <text key={t} x={x(t)} y={11} textAnchor="middle" className={`fill-dim ${fontClass}`} fontSize={9}>${t}</text>
            ))}
          </svg>
        </div>
        <span className={cx(TH.replace('px-2 ', ''), 'text-right')}>Spread</span>
        <span className={cx(TH.replace('px-2 ', ''), 'text-right')}>Same prec.</span>
      </div>
      <div className={cx(view === 'all' && 'max-h-[560px] overflow-y-auto')}>
        {rows.map((s) => {
          // Draw order: not up, other tiers, standard, then the cheapest and dearest on top.
          const rank = (e: EndpointRow) => (e === s.cheapest || e === s.dearest ? 3 : !e.up ? 0 : e.tier !== 'standard' ? 1 : 2)
          const eps = SNAP.endpoints.filter((e) => e.model === s.model && blended(e) > 0).sort((p, q) => rank(p) - rank(q))
          return (
            <button
              key={s.model}
              onClick={() => onRoute(s.model)}
              title={`Route ${name(s.model)}`}
              className={cx(grid, 'w-full cursor-pointer border-b border-line/50 py-[3px] text-left hover:bg-hover')}
            >
              <span className="flex min-w-0 items-baseline gap-1 text-[11.5px] text-bone"><span className="truncate">{name(s.model)}</span><span className="num shrink-0 text-[9.5px] text-dim">{s.hosts}</span></span>
              <svg width={tw} height={18} className="block overflow-visible">
                {LADDER_TICKS.map((t) => <line key={t} x1={x(t)} x2={x(t)} y1={0} y2={18} stroke="var(--color-line)" strokeDasharray="1 3" />)}
                <line x1={x(blended(s.cheapest))} x2={x(blended(s.dearest))} y1={9} y2={9} stroke="var(--color-line2)" strokeWidth={2} />
                <line x1={x(s.median)} x2={x(s.median)} y1={3} y2={15} stroke="var(--color-bone)" strokeWidth={1.25} />
                {eps.map((e, i) => <LadderMark key={i} e={e} cx={x(blended(e))} s={s} />)}
              </svg>
              <span className="num text-right text-[11.5px] text-bone">{times(s.spread)}</span>
              <span className="num text-right text-[11px] text-mute">{samePrec(s)}</span>
            </button>
          )
        })}
      </div>
      <div className="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-[10.5px] text-mute">
        <LegendSvg><circle cx={6} cy={6} r={3.2} fill="var(--color-fct)" fillOpacity={0.75} /></LegendSvg>
        <span className="-ml-3">standard, up</span>
        <LegendSvg><circle cx={6} cy={6} r={3.6} fill="var(--color-up)" /></LegendSvg><span className="-ml-3">cheapest</span>
        <LegendSvg><circle cx={6} cy={6} r={3.6} fill="var(--color-down)" /></LegendSvg><span className="-ml-3">dearest</span>
        <LegendSvg><line x1={6} x2={6} y1={1} y2={11} stroke="var(--color-bone)" strokeWidth={1.25} /></LegendSvg><span className="-ml-3">median</span>
        <LegendSvg><rect x={3} y={3} width={6} height={6} transform="rotate(45 6 6)" fill="none" stroke="var(--color-dec)" /></LegendSvg><span className="-ml-3">priority / flex</span>
        <LegendSvg><path d="M3.5 3.5l5 5M8.5 3.5l-5 5" stroke="var(--color-dim)" strokeWidth={1.2} /></LegendSvg><span className="-ml-3">not up</span>
      </div>
      <Foot>Spread = dearest ÷ cheapest standard-tier endpoint that is up. Same precision = the spread inside the largest labelled precision group with 3+ endpoints, so quantization cannot explain it. Deepest markets (most endpoints) first.</Foot>
    </Panel>
  )
}

function LegendSvg({ children }: { children: ReactNode }) {
  return <svg width={12} height={12} className="shrink-0">{children}</svg>
}

function LadderMark({ e, cx: x0, s }: { e: EndpointRow; cx: number; s: Spread }) {
  const tip = <title>{`${e.provider} · ${usd(blended(e))}/M · ${bitsLabel(bits(e.quant))} · ${e.tier}${e.up ? '' : ' · not up'}`}</title>
  if (!e.up) return <path d={`M${x0 - 2.5} 6.5l5 5M${x0 + 2.5} 6.5l-5 5`} stroke="var(--color-dim)" strokeWidth={1.2}>{tip}</path>
  if (e.tier !== 'standard') return <rect x={x0 - 3} y={6} width={6} height={6} transform={`rotate(45 ${x0} 9)`} fill="var(--color-panel)" stroke="var(--color-dec)">{tip}</rect>
  const end = e === s.cheapest ? 'var(--color-up)' : e === s.dearest ? 'var(--color-down)' : null
  return end ? <circle cx={x0} cy={9} r={3.6} fill={end}>{tip}</circle> : <circle cx={x0} cy={9} r={3.2} fill="var(--color-fct)" fillOpacity={0.75}>{tip}</circle>
}

// ── Token procurement ────────────────────────────────────────────────────────

const ROUTABLE = (() => {
  const n = new Map<string, number>()
  for (const e of SNAP.endpoints) if (MODEL.get(e.model)?.open !== false) n.set(e.model, (n.get(e.model) ?? 0) + 1)
  return [...n].sort((a, b) => b[1] - a[1] || name(a[0]).localeCompare(name(b[0])))
})()

export function TokenProcurement({ model, onModel }: { model: string; onModel: (modelId: string) => void }) {
  const [promptM, setPromptM] = useState('75')
  const [completionM, setCompletionM] = useState('25')
  const [minUptime, setMinUptime] = useState(99)
  const [standardOnly, setStandardOnly] = useState(true)
  const [tools, setTools] = useState(false)
  const num = (s: string) => Math.max(0, Number(s) || 0)
  const r = useMemo(
    () => route(model, SNAP.endpoints, { promptM: num(promptM), completionM: num(completionM), minUptime, standardOnly, tools }),
    [model, promptM, completionM, minUptime, standardOnly, tools],
  )
  const best = r.best
  const label = 'mb-1 block font-mono text-[9.5px] tracking-wider text-dim uppercase'
  return (
    <Panel title="Token procurement" eyebrow={<><TruthTag truth="dec" compact /> Cheapest endpoint that meets the job · list price × volume</>}>
      <div className="grid grid-cols-2 gap-x-3 gap-y-2.5 sm:grid-cols-[minmax(0,2fr)_1fr_1fr_1fr]">
        <label className="col-span-2 sm:col-span-1">
          <span className={label}>Model</span>
          <select className={cx(INPUT, 'w-full')} value={model} onChange={(e) => onModel(e.target.value)}>
            {ROUTABLE.map(([id, n]) => <option key={id} value={id}>{name(id)} · {n} endpoints</option>)}
          </select>
        </label>
        <label>
          <span className={label}>Prompt tok. M</span>
          <input type="number" min={0} step="any" className={cx(INPUT, 'w-full')} value={promptM} onChange={(e) => setPromptM(e.target.value)} />
        </label>
        <label>
          <span className={label}>Compl. tok. M</span>
          <input type="number" min={0} step="any" className={cx(INPUT, 'w-full')} value={completionM} onChange={(e) => setCompletionM(e.target.value)} />
        </label>
        <label className="col-span-2 sm:col-span-1">
          <span className={label}>Min uptime 1d</span>
          <select className={cx(INPUT, 'w-full')} value={minUptime} onChange={(e) => setMinUptime(Number(e.target.value))}>
            <option value={0}>any</option>
            <option value={95}>≥95%</option>
            <option value={99}>≥99%</option>
            <option value={99.9}>≥99.9%</option>
          </select>
        </label>
      </div>
      <div className="mt-2.5 flex flex-wrap gap-x-5 gap-y-1 text-[11.5px] text-mute">
        <label className="flex cursor-pointer items-center gap-1.5"><input type="checkbox" className="accent-[var(--color-dec)]" checked={standardOnly} onChange={(e) => setStandardOnly(e.target.checked)} />Standard tier only</label>
        <label className="flex cursor-pointer items-center gap-1.5"><input type="checkbox" className="accent-[var(--color-dec)]" checked={tools} onChange={(e) => setTools(e.target.checked)} />Needs tool calling</label>
      </div>

      {best ? (
        <>
          <div className="mt-3.5 rounded-md border border-dec/40 bg-raise/60 p-3">
            <div className="mb-2.5 text-[12.5px] text-bone">
              Route to <b className="font-semibold">{best.e.provider}</b> <span className="font-mono text-[11px] text-mute">{best.e.tag}</span>
              <span className="text-mute"> · {bitsLabel(bits(best.e.quant))} · {best.e.tier}</span>
            </div>
            <div className="grid grid-cols-3 gap-3">
              <Figure label="Cheapest plan" value={money(best.cost)} sub={`${usd(best.e.prompt)} in · ${usd(best.e.completion)} out`} tone="text-dec" />
              <Figure label="Median feasible" value={money(r.median)} sub={`${r.plans.length} feasible of ${r.listed}`} />
              <Figure label="Saving vs median" value={money(r.saving)} sub={`${r.savingPct.toFixed(0)}% · vs dearest ${r.vsDearestPct.toFixed(0)}%`} tone="text-up" />
            </div>
          </div>
          <table className="mt-3 w-full text-[11.5px]">
            <tbody>
              {r.plans.slice(0, 5).map((p, i) => (
                <tr key={i} className="border-b border-line/50 last:border-0">
                  <td className="num w-6 py-1 text-dim">{i + 1}</td>
                  <td className="py-1 text-bone"><span className="truncate">{p.e.provider}</span><span className="text-dim"> · {bitsLabel(bits(p.e.quant))}</span></td>
                  <td className="num py-1 text-right text-bone">{money(p.cost)}</td>
                  <td className="num w-16 py-1 text-right text-mute">{i === 0 ? 'best' : `+${((p.cost / best.cost - 1) * 100).toFixed(0)}%`}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : (
        <div className="mt-3.5 rounded-md border border-dashed border-line2 p-4 text-[12px] text-mute">
          No endpoint of {name(model)} meets these needs ({r.listed} listed). Loosen the uptime floor, allow other tiers or drop tool calling.
        </div>
      )}
      <Foot>Routing only: providers post list prices, so the lever is where you buy, not when. Costs are list price × volume; FLOPX has bought nothing. Cache discounts are ignored and uptime is the last day only.</Foot>
    </Panel>
  )
}

function Figure({ label, value, sub, tone = 'text-bone' }: { label: string; value: ReactNode; sub: ReactNode; tone?: string }) {
  return (
    <div className="min-w-0">
      <div className="font-mono text-[9.5px] tracking-wider text-dim uppercase">{label}</div>
      <div className={cx('num truncate text-[19px] leading-tight', tone)}>{value}</div>
      <div className="num truncate text-[10.5px] text-mute">{sub}</div>
    </div>
  )
}

// ── Host table ───────────────────────────────────────────────────────────────

export function HostTable({ onRoute }: { onRoute: (modelId: string) => void }) {
  const [view, setView] = useState<'top' | 'all'>('top')
  const rows = view === 'top' ? SPREADS.slice(0, 10) : SPREADS
  return (
    <Panel
      title="Cheapest and dearest host per model"
      eyebrow="Standard tier, endpoints that are up · blended $/M"
      right={<Seg size="sm" value={view} onChange={setView} options={[{ id: 'top', label: 'Top 10' }, { id: 'all', label: `All ${SPREADS.length}` }]} />}
    >
      <div className={cx('overflow-x-auto', view === 'all' && 'max-h-[520px] overflow-y-auto')}>
        <table className="w-full text-[11.5px]">
          <thead className="sticky top-0 bg-panel">
            <tr className="border-b border-line">
              <th className={TH}>Model</th>
              <th className={cx(TH, 'text-right')}>Hosts</th>
              <th className={TH}>Cheapest</th>
              <th className={cx(TH, 'text-right')}>Median</th>
              <th className={TH}>Dearest</th>
              <th className={cx(TH, 'text-right')}>Spread</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((s) => (
              <tr key={s.model} onClick={() => onRoute(s.model)} className="cursor-pointer border-b border-line/50 hover:bg-hover">
                <td className={cx(TD, 'max-w-[13rem] truncate text-bone')}>{name(s.model)}</td>
                <td className={cx(TD, 'num text-right text-mute')}>{s.hosts}</td>
                <td className={TD}><span className="num text-up">{usd(blended(s.cheapest))}</span> <span className="text-mute">{s.cheapest.provider}</span></td>
                <td className={cx(TD, 'num text-right text-bone')}>{usd(s.median)}</td>
                <td className={TD}><span className="num text-down">{usd(blended(s.dearest))}</span> <span className="text-mute">{s.dearest.provider}</span></td>
                <td className={cx(TD, 'num text-right text-bone')}>{times(s.spread)}<span className="text-dim"> / {s.samePrecision ? times(s.samePrecision.spread) : '—'}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Foot>Spread / same-precision spread. Median spread across {SPREADS.length} models: {times(median(SPREADS.map((s) => s.spread)))}.</Foot>
    </Panel>
  )
}

// ── Why it is cheaper ────────────────────────────────────────────────────────

const GROUP_LABEL: Record<string, string> = { precision: 'Precision', tier: 'Tier', context: 'Context cap', uptime: 'Uptime', features: 'Features' }

export function WhyCheaper({ model }: { model: string }) {
  const s = spreadFor(model, SNAP.endpoints)
  const d = s ? decompose(FIT, s.cheapest, s.dearest) : null
  const eyebrow = <><TruthTag truth="inf" compact /> {name(model)}: cheapest vs dearest host, same weights</>
  if (!s || !d) {
    return (
      <Panel title="Why it is cheaper" eyebrow={eyebrow}>
        <div className="rounded-md border border-dashed border-line2 p-4 text-[12px] text-mute">
          {name(model)} has fewer than two standard-tier endpoints that are up, so there is no same-weights gap to explain. Pick a deeper market in the spread ladder.
        </div>
      </Panel>
    )
  }
  const A = s.cheapest, B = s.dearest
  const terms = [...d.groups].filter(([, v]) => Math.abs(v) >= 0.5)
  // Flip signs for the prose: "A lists N log points below B".
  const gap = -d.gap, explained = -d.explained, unexplained = -d.unexplained
  const sum = d.explained + d.unexplained
  const unlabeled = [A, B].filter((e) => bits(e.quant) === 0)
  return (
    <Panel title="Why it is cheaper" eyebrow={eyebrow}>
      <div className="grid grid-cols-2 gap-3">
        <HostCard e={A} tone="text-up" />
        <HostCard e={B} tone="text-down" />
      </div>
      <Waterfall terms={terms} unexplained={d.unexplained} gap={d.gap} />
      <div className="mt-1 num text-[10.5px] text-dim">
        Σ terms {signed(sum, 1)} vs observed {signed(d.gap, 1)} · {Math.abs(sum - d.gap) < 0.05 ? 'exact' : `off by ${Math.abs(sum - d.gap).toFixed(2)}`}
      </div>
      <p className="mt-2.5 text-[12px] leading-relaxed text-mute">
        <b className="text-bone">{A.provider}</b> lists {gap.toFixed(0)} log points ({times(blended(B) / blended(A))}) below <b className="text-bone">{B.provider}</b> for the same weights.
        {' '}Attributes explain {explained.toFixed(0)}{terms.length ? ` (${terms.map(([g, v]) => `${g} ${(-v).toFixed(0)}`).join(', ')})` : ''};
        {' '}{unexplained.toFixed(0)} ({gap ? Math.round((unexplained / gap) * 100) : 0}% of the gap) is unexplained by anything this snapshot can see, including speed, which is not observed.
        {unlabeled.length > 0 && <> {unlabeled.map((e) => e.provider).join(' and ')} publish{unlabeled.length === 1 ? 'es' : ''} no precision label, so the precision term is the fitted average for an unlabelled endpoint, not a measured difference.</>}
      </p>
    </Panel>
  )
}

function HostCard({ e, tone }: { e: EndpointRow; tone: string }) {
  const vf = vsFair(e)
  const fair = FIT_ROW.get(e)?.fair
  const row = (k: string, v: ReactNode) => (
    <div className="flex justify-between gap-2 border-t border-line/50 py-1"><span className="text-dim">{k}</span><span className="num truncate text-right text-bone">{v}</span></div>
  )
  return (
    <div className="min-w-0 rounded-md border border-line bg-raise/40 p-2.5 text-[11px]">
      <div className="truncate text-[12.5px] font-semibold text-bone">{e.provider}</div>
      <div className="truncate font-mono text-[10px] text-dim">{e.tag}</div>
      <div className={cx('num mb-1.5 mt-1 text-[18px] leading-tight', tone)}>{usd(blended(e))}<span className="ml-0.5 text-[11px] text-dim">/M blended</span></div>
      {row('Fair (model)', fair != null && vf != null ? `${usd(fair)} · ${signedPct(vf)}` : '—')}
      {row('Precision', bitsLabel(bits(e.quant)))}
      {row('Context', ctxLabel(e.context))}
      {row('Uptime 1d', e.uptime == null ? '—' : `${e.uptime.toFixed(1)}%`)}
    </div>
  )
}

/** Waterfall in log points: each attribute term steps from the running total; the gap bar runs from zero. */
function Waterfall({ terms, unexplained, gap }: { terms: [string, number][]; unexplained: number; gap: number }) {
  const steps: { label: string; v: number; from: number; kind: 'attr' | 'resid' | 'total' }[] = []
  let run = 0
  for (const [g, v] of terms) { steps.push({ label: GROUP_LABEL[g] ?? g, v, from: run, kind: 'attr' }); run += v }
  steps.push({ label: 'Mispricing (unexplained)', v: unexplained, from: run, kind: 'resid' })
  steps.push({ label: 'Observed gap', v: gap, from: 0, kind: 'total' })
  const ends = steps.flatMap((s) => [s.from, s.from + s.v])
  const lo = Math.min(0, ...ends), hi = Math.max(0, ...ends)
  const span = hi - lo || 1
  const pos = (v: number) => ((v - lo) / span) * 100
  return (
    <div className="mt-3.5">
      <div className="mb-1 flex justify-between font-mono text-[9.5px] tracking-wider text-dim uppercase">
        <span>Log points, cheapest minus dearest</span>
      </div>
      {steps.map((s) => {
        const a = pos(Math.min(s.from, s.from + s.v)), b = pos(Math.max(s.from, s.from + s.v))
        return (
          <div key={s.label} className={cx('grid grid-cols-[minmax(0,9.5rem)_minmax(0,1fr)_3rem] items-center gap-x-2 py-[3px] text-[11px]', s.kind === 'total' && 'mt-1 border-t border-line pt-1.5')}>
            <span className={cx('truncate', s.kind === 'total' ? 'text-bone' : 'text-mute')}>{s.label}</span>
            <div className="relative h-3.5">
              <div className="absolute inset-y-0 w-px bg-line2" style={{ left: `${pos(0)}%` }} />
              <div
                className={cx('absolute inset-y-0.5', s.kind === 'attr' && 'bg-fct/70', s.kind === 'resid' && 'hatch-inf border border-inf', s.kind === 'total' && 'bg-bone/80')}
                style={{ left: `${a}%`, width: `max(1px, ${b - a}%)` }}
              />
            </div>
            <span className={cx('num text-right', s.kind === 'total' ? 'text-bone' : 'text-mute')}>{signed(s.v, 0)}</span>
          </div>
        )
      })}
      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[10.5px] text-mute">
        <span className="flex items-center gap-1.5"><span className="inline-block size-2.5 bg-fct/70" />attribute (explained)</span>
        <span className="flex items-center gap-1.5"><span className="hatch-inf inline-block size-2.5 border border-inf" />unexplained</span>
        <span className="flex items-center gap-1.5"><span className="inline-block size-2.5 bg-bone/80" />observed total</span>
      </div>
    </div>
  )
}

// ── Hedonic fit ──────────────────────────────────────────────────────────────

export function HedonicFit() {
  const within = Math.round(FIT.withinShare * 100)
  return (
    <Panel
      title="Hedonic fit"
      eyebrow={<><TruthTag truth="inf" compact /> Ridge on log blended $/M with model fixed effects</>}
      right={<span className="num text-[11px] text-mute">R² {FIT.r2.toFixed(3)} · {FIT.n} endpoints up</span>}
    >
      <div className="grid grid-cols-3 gap-3">
        <Figure label="In-sample R²" value={FIT.r2.toFixed(3)} sub="model + attributes" tone="text-inf" />
        <Figure label="Model identity alone" value={FIT.r2Model.toFixed(3)} sub="fixed effects only" />
        <Figure label="Within same weights" value={`${within}%`} sub="spread explained" tone="text-inf" />
      </div>
      <p className="mt-3 text-[12px] leading-relaxed text-mute">
        Each endpoint is compared only with the identical weights ({FIT.models} model fixed effects). Inside a model, the listed attributes explain {within}% of the price spread; the rest is the residual, which also holds whatever the snapshot cannot see, such as speed.
      </p>
      <div className="mt-3 overflow-x-auto">
        <table className="w-full text-[11.5px]">
          <thead>
            <tr className="border-b border-line">
              <th className={TH}>Attribute</th>
              <th className={cx(TH, 'text-right')}>Coef (log)</th>
              <th className={cx(TH, 'text-right')}>≈ listed price</th>
            </tr>
          </thead>
          <tbody>
            {FEATURES.map((f, j) => {
              const c = FIT.coef[j], p = pctEffect(c)
              return (
                <tr key={f.key} className="border-b border-line/50 last:border-0">
                  <td className={cx(TD, 'whitespace-normal text-bone')}>{f.label}</td>
                  <td className={cx(TD, 'num text-right text-mute')}>{signed(c, 3)}</td>
                  <td className={cx(TD, 'num text-right', Math.abs(p) < 0.5 ? 'text-dim' : p < 0 ? 'text-up' : 'text-down')}>{signedPct(p, Math.abs(p) < 10 ? 1 : 0)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <Foot>ridge on log(blended $/M) ~ model + precision + tier + context cap + uptime gap + tools + structured + caching + time-of-day pricing. Partial correlations in one snapshot, not causal effects.</Foot>
    </Panel>
  )
}

// ── Provider leaderboard ─────────────────────────────────────────────────────

export function ProviderLeaderboard() {
  const [view, setView] = useState<'5' | '1'>('5')
  const all = useMemo(() => leaderboard(FIT, view === '5' ? 5 : 1), [view])
  const rows = view === '5' && all.length > 18 ? [...all.slice(0, 9), ...all.slice(-9)] : all
  const max = Math.max(1, ...rows.map((r) => Math.abs(r.vsFair)))
  const below = all.filter((r) => r.vsFair < 0).length
  return (
    <Panel
      title="Provider leaderboard"
      eyebrow={<><TruthTag truth="inf" compact /> Median listed price vs fair value, by host</>}
      right={<Seg size="sm" value={view} onChange={setView} options={[{ id: '5', label: '5+ endpoints' }, { id: '1', label: 'All hosts' }]} />}
    >
      <div className={cx(view === '1' && 'max-h-[520px] overflow-y-auto pr-1')}>
        {rows.map((r, i) => {
          const w = (Math.abs(r.vsFair) / max) * 50
          const gapAfter = view === '5' && rows.length < all.length && i === 8
          return (
            <div key={r.provider} className={cx('grid grid-cols-[minmax(0,9rem)_minmax(0,1fr)_3rem] items-center gap-x-2 py-[2.5px] text-[11px]', gapAfter && 'mb-1.5 border-b border-dashed border-line2 pb-1.5')}>
              <span className="truncate text-bone">{r.provider} <span className="num text-dim">({r.n})</span></span>
              <div className="relative h-3">
                <div className="absolute inset-y-[-3px] left-1/2 w-px bg-line2" />
                <div
                  className={cx('absolute inset-y-0.5', r.vsFair < 0 ? 'bg-up/70' : 'bg-down/70')}
                  style={r.vsFair < 0 ? { right: '50%', width: `${w}%` } : { left: '50%', width: `${w}%` }}
                />
              </div>
              <span className={cx('num text-right', r.vsFair < 0 ? 'text-up' : 'text-down')}>{signedPct(r.vsFair)}</span>
            </div>
          )
        })}
      </div>
      <div className="mt-1 grid grid-cols-[minmax(0,9rem)_minmax(0,1fr)_3rem] gap-x-2 font-mono text-[9.5px] text-dim">
        <span />
        <div className="flex justify-between"><span>below fair</span><span>fair</span><span>above fair</span></div>
        <span />
      </div>
      <Foot>
        {view === '5' && rows.length < all.length ? `Cheapest 9 and dearest 9 of ${all.length} hosts` : `${all.length} hosts`} · {below} below fair · {all.length - below} at or above. (n) = endpoints; a host with one or two is an anecdote. Fast-hardware hosts carry a positive residual that is partly paid-for speed, which this snapshot does not observe.
      </Foot>
    </Panel>
  )
}

// ── Endpoint table ───────────────────────────────────────────────────────────

export function EndpointTable({ model }: { model: string }) {
  const eps = useMemo(() => SNAP.endpoints.filter((e) => e.model === model).sort((a, b) => blended(a) - blended(b)), [model])
  const s = spreadFor(model, SNAP.endpoints)
  const cols = ['Provider', 'Endpoint', 'Precision', 'Tier', 'Context', 'Uptime 1d', 'Tools', 'Prompt /M', 'Compl. /M', 'Blended /M', 'Fair /M', 'vs fair', 'Status']
  const right = new Set(['Context', 'Uptime 1d', 'Prompt /M', 'Compl. /M', 'Blended /M', 'Fair /M', 'vs fair'])
  return (
    <Panel
      title={`Endpoints · ${name(model)}`}
      eyebrow={<>Every listed host · price <TruthTag truth="obs" compact /> · fair and vs fair <TruthTag truth="inf" compact /></>}
    >
      <div className="mb-3 flex flex-wrap gap-x-4 gap-y-1 text-[11.5px] text-mute">
        <span><b className="num font-normal text-bone">{eps.length}</b> endpoints listed</span>
        {s ? (
          <>
            <span>cheapest <b className="num font-normal text-up">{usd(blended(s.cheapest))}</b> {s.cheapest.provider}</span>
            <span>median <b className="num font-normal text-bone">{usd(s.median)}</b></span>
            <span>dearest <b className="num font-normal text-down">{usd(blended(s.dearest))}</b> {s.dearest.provider}</span>
            <span>spread <b className="num font-normal text-bone">{times(s.spread)}</b>{s.samePrecision && <> · <b className="num font-normal text-bone">{times(s.samePrecision.spread)}</b> at {s.samePrecision.bits} bit</>}</span>
          </>
        ) : <span>fewer than two standard-tier endpoints up, no spread</span>}
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-[11.5px]">
          <thead>
            <tr className="border-b border-line">{cols.map((c) => <th key={c} className={cx(TH, right.has(c) && 'text-right')}>{c}</th>)}</tr>
          </thead>
          <tbody>
            {eps.map((e, i) => {
              const fr = FIT_ROW.get(e), vf = vsFair(e)
              const counted = standardLive(e)
              return (
                <tr key={i} className={cx('border-b border-line/50 last:border-0', !e.up && 'text-dim')}>
                  <td className={cx(TD, e.up ? 'text-bone' : 'text-dim')}>{e.provider}</td>
                  <td className={cx(TD, 'font-mono text-[10.5px] text-mute')}>{e.tag}</td>
                  <td className={cx(TD, 'text-mute')}>{bitsLabel(bits(e.quant))}</td>
                  <td className={cx(TD, e.tier === 'standard' ? 'text-mute' : 'text-dec')}>{e.tier}</td>
                  <td className={cx(TD, 'num text-right text-mute')}>{ctxLabel(e.context)}</td>
                  <td className={cx(TD, 'num text-right', e.uptime != null && e.uptime < 95 ? 'text-warn' : 'text-mute')}>{e.uptime == null ? '—' : `${e.uptime.toFixed(1)}%`}</td>
                  <td className={cx(TD, 'text-mute')}>{e.tools ? '✓' : '—'}</td>
                  <td className={cx(TD, 'num text-right text-mute')}>{usd(e.prompt)}</td>
                  <td className={cx(TD, 'num text-right text-mute')}>{usd(e.completion)}</td>
                  <td className={cx(TD, 'num text-right', !e.up ? 'text-dim' : counted && s && e === s.cheapest ? 'text-up' : counted && s && e === s.dearest ? 'text-down' : 'text-bone')}>{usd(blended(e))}</td>
                  <td className={cx(TD, 'num text-right text-mute')}>{fr ? usd(fr.fair) : '—'}</td>
                  <td className={cx(TD, 'num text-right', vf == null ? 'text-dim' : vf < 0 ? 'text-up' : 'text-down')}>{vf == null ? '—' : signedPct(vf)}</td>
                  <td className={cx(TD, e.up ? 'text-up' : 'text-dim')}>
                    {e.up ? 'up' : 'not up'}
                    <span className="text-dim">{e.discount > 0 && ` · disc ${+(e.discount * 100).toFixed(1)}%`}{e.timeOfDay && ' · time-of-day'}</span>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <Foot>Cheapest first. Endpoints that are not up are excluded from the fit, the spreads and routing. Fair /M is the hedonic model's price for this endpoint's attributes.</Foot>
    </Panel>
  )
}
