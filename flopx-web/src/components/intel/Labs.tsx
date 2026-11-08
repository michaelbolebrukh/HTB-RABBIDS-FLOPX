// Layer 1 panels (labs): what each lab lists, closed vs open, cheapest way into a tier, newest releases.
import { useState, type ReactNode } from 'react'
import { scaleLog } from 'd3-scale'
import { Fn, Panel, Seg, TruthTag, cx } from '@/components/ui'
import { beeswarm, useWidth } from '@/components/charts'
import { useChartStyle } from '@/lib/chartStyle'
import { TIERS, bits, bitsLabel, blended, closedVsOpen, labLadder, median, standardLive, tierModels, type EndpointRow, type Model } from '@/lib/tokenmarket'
import { CLOSED, NOTES, OPEN, SNAP, fmtDate, times, usd } from '@/components/intel/shared'

const LADDER = labLadder(SNAP.models)
const CVO = closedVsOpen(SNAP.models)
const PRICES = SNAP.models.map(blended).filter((p) => p > 0)
/** Shared log domain for the ladder and the strips, padded past the extremes. */
const DOMAIN: [number, number] = [Math.min(...PRICES) * 0.8, Math.max(...PRICES) * 1.25]
const TICKS = [0.03, 0.1, 0.3, 1, 3, 10, 30].filter((t) => t >= DOMAIN[0] && t <= DOMAIN[1])
const tickLabel = (t: number) => `$${t}`

/** Standard-tier endpoints that are up for one model, cheapest first. */
function hostsFor(model: string, eps: EndpointRow[] = SNAP.endpoints): EndpointRow[] {
  return eps.filter((e) => e.model === model && standardLive(e)).sort((a, b) => blended(a) - blended(b))
}
const extremes = (ms: Model[]) => {
  const s = [...ms].sort((a, b) => blended(a) - blended(b))
  return { lo: s[0], hi: s[s.length - 1] }
}
const pctBelow = (v: number, ref: number) => Math.round((1 - v / ref) * 100)

// ── Price ladder ─────────────────────────────────────────────────────────────

const ROW_H = 28
/** First and third quartiles, interpolated in log space to match the log axis. */
function logQuartiles(prices: number[]): [number, number] {
  const ls = prices.filter((p) => p > 0).map(Math.log).sort((a, b) => a - b)
  const q = (f: number) => { const i = (ls.length - 1) * f, a = Math.floor(i), b = Math.ceil(i); return Math.exp(ls[a] + (ls[b] - ls[a]) * (i - a)) }
  return [q(0.25), q(0.75)]
}
const LADDER_COLS = 'grid grid-cols-[minmax(72px,112px)_minmax(0,1fr)_48px_40px_64px] items-center gap-x-3'

export function PriceLadder() {
  const [sort, setSort] = useState<'median' | 'models'>('median')
  const [sel, setSel] = useState<string | null>(null)
  const [ref, w] = useWidth<HTMLDivElement>()
  const { fontClass } = useChartStyle()
  const x = scaleLog().domain(DOMAIN).range([5, Math.max(10, w - 5)])
  const rows = sort === 'median' ? LADDER : [...LADDER].sort((a, b) => b.models.length - a.models.length || b.median - a.median)
  const picked = LADDER.find((r) => r.lab === sel)
  return (
    <Panel
      title="Price ladder by lab"
      eyebrow="Blended $/M · log scale"
      right={<Seg size="sm" value={sort} onChange={setSort} options={[{ id: 'median', label: 'By median' }, { id: 'models', label: 'By models' }]} />}
    >
      <div className="overflow-x-auto">
        <div className="min-w-[420px] max-md:min-w-[560px]">
          <div className={cx(LADDER_COLS, 'mb-1 text-[11px] font-medium text-dim')}>
            <span>Lab</span>
            <div ref={ref}>
              <svg width={w} height={12} className="block overflow-visible">
                {TICKS.map((t, i) => <text key={t} x={x(t)} y={9} textAnchor="middle" className={`fill-dim ${fontClass}${i % 2 ? ' max-md:hidden' : ''}`} fontSize={9}>{tickLabel(t)}</text>)}
              </svg>
            </div>
            <span className="text-right">Models</span>
            <span className="text-right">Open</span>
            <span className="text-right">Median /M</span>
          </div>
          {rows.map((r) => {
            const { lo, hi } = extremes(r.models)
            const on = r.lab === sel
            return (
              <button
                key={r.lab}
                onClick={() => setSel(on ? null : r.lab)}
                title={`${r.lab}: show cheapest and dearest model`}
                aria-pressed={on}
                className={cx(LADDER_COLS, 'w-full cursor-pointer rounded-[3px] border-b border-line-soft text-left transition-colors last:border-0 max-md:min-h-11', on ? 'bg-raise' : 'hover:bg-hover')}
                style={{ height: ROW_H }}
              >
                <span className={cx('truncate pl-1 text-[11.5px]', on ? 'text-bone' : 'text-mute')}>{r.lab}</span>
                <svg width={w} height={ROW_H} className="block overflow-visible">
                  {TICKS.map((t) => <line key={t} x1={x(t)} x2={x(t)} y1={0} y2={ROW_H} stroke="var(--color-line-soft)" />)}
                  {(() => {
                    const [q1, q3] = logQuartiles(r.models.map(blended))
                    const cy = ROW_H / 2
                    const share = r.models.length ? r.open / r.models.length : 0
                    const ink = on ? 'var(--color-bone)' : 'var(--color-mute)'
                    // Box tint: grey for an all-closed lab, toward the open-weights hue by open share.
                    const tint = `color-mix(in srgb, ${OPEN} ${Math.round(share * 100)}%, var(--color-mute))`
                    return (
                      <g>
                        <line x1={x(r.min)} x2={x(r.max)} y1={cy} y2={cy} stroke={ink} strokeOpacity={0.55} />
                        {([[lo, 'cheapest', 'var(--color-up)'], [hi, 'dearest', 'var(--color-down)']] as const).map(([m, k, c]) => (
                          <line key={k} x1={x(blended(m))} x2={x(blended(m))} y1={cy - 3.5} y2={cy + 3.5} stroke={c} strokeOpacity={0.75} strokeWidth={1.5}>
                            <title>{`${k}: ${m.name} · ${usd(blended(m))}/M${m.open ? ' · open weights' : ''}`}</title>
                          </line>
                        ))}
                        <rect x={x(q1)} y={cy - 4} width={Math.max(2, x(q3) - x(q1))} height={8} rx={1} fill={tint} fillOpacity={on ? 0.6 : 0.42}>
                          <title>{`${r.lab}: middle 50% ${usd(q1)}–${usd(q3)}/M · ${Math.round(share * 100)}% open weights`}</title>
                        </rect>
                        <line x1={x(r.median)} x2={x(r.median)} y1={cy - 6} y2={cy + 6} stroke="var(--color-bone)" strokeWidth={1.5} />
                      </g>
                    )
                  })()}
                </svg>
                <span className="num text-right text-[11px] text-mute">{r.models.length}</span>
                <span className={cx('num text-right text-[11px]', !r.open && 'text-dim')} style={r.open ? { color: OPEN } : undefined}>{r.open || '–'}</span>
                <span className="num text-right text-[11px] text-bone">{usd(r.median)}</span>
              </button>
            )
          })}
        </div>
      </div>
      {picked && <div className="mt-2 text-[11.5px] text-mute">
        {(() => {
          const { lo, hi } = extremes(picked.models)
          return <><b className="font-medium text-bone">{picked.lab}</b>: cheapest {lo.name} at <span className="num text-bone">{usd(blended(lo))}</span>/M · dearest {hi.name} at <span className="num text-bone">{usd(blended(hi))}</span>/M · {times(blended(hi) / blended(lo))} apart</>
        })()}
      </div>}
      <Legend items={[
        { mark: <svg width="24" height="8" aria-hidden="true"><line x1="0.5" x2="23.5" y1="4" y2="4" stroke="var(--color-mute)" strokeOpacity={0.55} /><line x1="0.75" x2="0.75" y1="0.5" y2="7.5" stroke="var(--color-up)" strokeOpacity={0.75} strokeWidth={1.5} /><line x1="23.25" x2="23.25" y1="0.5" y2="7.5" stroke="var(--color-down)" strokeOpacity={0.75} strokeWidth={1.5} /></svg>, label: 'cheapest to dearest model' },
        { mark: <span className="inline-block h-2 w-8 rounded-[1px] opacity-60" style={{ background: `linear-gradient(to right, var(--color-mute), ${OPEN})` }} />, label: 'middle 50%, tinted by open-weight share' },
        { mark: <span className="inline-block h-3 w-[1.5px] bg-bone" />, label: 'median' },
      ]} />
      <p className="mt-2 text-[11px] leading-relaxed text-dim">Labs with 3+ text models. {SNAP.aliases} "latest" alias routes (e.g. ~anthropic/claude-opus-latest) are left out as duplicates.</p>
    </Panel>
  )
}

function Legend({ items }: { items: { mark: ReactNode; label: string }[] }) {
  return (
    <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-dim">
      {items.map((it) => <span key={it.label} className="inline-flex items-center gap-1.5">{it.mark}{it.label}</span>)}
    </div>
  )
}

// ── Closed vs open ───────────────────────────────────────────────────────────

export function ClosedVsOpen() {
  const [ref, w] = useWidth<HTMLDivElement>()
  const { fontClass } = useChartStyle()
  const pad = { l: 48, r: 8 }
  const x = scaleLog().domain(DOMAIN).range([pad.l, Math.max(pad.l + 10, w - pad.r)])
  const strip = 64, top = 16, axisY = top + 2 * strip + 4, h = axisY + 16
  const rowsCfg = [
    { label: 'Closed', xs: CVO.closed.filter((p) => p > 0), med: CVO.closedMedian, color: CLOSED },
    { label: 'Open', xs: CVO.open.filter((p) => p > 0), med: CVO.openMedian, color: OPEN },
  ]
  const oa = extremes(SNAP.models.filter((m) => m.lab === 'OpenAI'))
  return (
    <Panel title="Closed frontier vs open weights" eyebrow={`${CVO.closed.length} closed · ${CVO.open.length} open · log $/M`}>
      <div ref={ref}>
        <svg width={w} height={h} className="block">
          {TICKS.map((t) => <line key={t} x1={x(t)} x2={x(t)} y1={top - 4} y2={axisY} stroke="var(--color-line)" strokeDasharray="2 4" />)}
          {rowsCfg.map((r, k) => {
            const cy = top + k * strip + strip / 2
            const px = r.xs.map((p) => x(p))
            const off = beeswarm(px, 2.2, strip / 2 - 13)
            return (
              <g key={r.label}>
                <text x={0} y={cy + 3} className={`fill-mute ${fontClass}`} fontSize={10}>{r.label}</text>
                {px.map((p, i) => <circle key={i} cx={p} cy={cy + off[i]} r={2.2} fill={r.color} fillOpacity={0.7} />)}
                <line x1={x(r.med)} x2={x(r.med)} y1={cy - strip / 2 + 6} y2={cy + strip / 2 - 2} stroke="var(--color-bone)" strokeWidth={1.5} />
                <text x={x(r.med) + 4} y={cy - strip / 2 + 10} className={`fill-bone ${fontClass}`} fontSize={9.5} stroke="var(--color-panel)" strokeWidth={3} paintOrder="stroke">median {usd(r.med)}/M</text>
              </g>
            )
          })}
          <line x1={pad.l} x2={w - pad.r} y1={axisY} y2={axisY} stroke="var(--color-line2)" />
          {TICKS.map((t, i) => <text key={t} x={x(t)} y={axisY + 12} textAnchor="middle" className={`fill-dim ${fontClass}${i % 2 ? ' max-md:hidden' : ''}`} fontSize={9}>{tickLabel(t)}</text>)}
        </svg>
      </div>
      <p className="mt-2 text-[12px] leading-relaxed text-mute">
        At the median a closed model lists at <span className="num text-bone">{times(CVO.ratio)}</span> an open-weight one, but the ranges overlap: OpenAI itself spans <span className="num text-bone">{usd(blended(oa.lo))}</span> ({oa.lo.name}) to <span className="num text-bone">{usd(blended(oa.hi))}</span> ({oa.hi.name}) per million tokens.
      </p>
    </Panel>
  )
}

// ── Cheapest way to buy ──────────────────────────────────────────────────────

interface TierLine { m: Model; list: number; host: EndpointRow | null; hosts: number; best: number }

function tierLines(tier: keyof typeof TIERS): TierLine[] {
  return tierModels(SNAP.models, TIERS[tier])
    .map((m) => {
      const hs = m.open ? hostsFor(m.id) : []
      const host = hs[0] ?? null
      const list = blended(m)
      return { m, list, host, hosts: hs.length, best: host ? Math.min(list, blended(host)) : list }
    })
    .sort((a, b) => a.best - b.best)
}

export function CheapestWay({ onRoute }: { onRoute: (modelId: string) => void }) {
  const [tier, setTier] = useState<keyof typeof TIERS>('large')
  const lines = tierLines(tier)
  const lo = lines[0], hi = lines[lines.length - 1]
  const med = median(lines.map((l) => l.best))
  return (
    <Panel
      title="Cheapest way to buy"
      eyebrow={<><TruthTag truth="dec" compact /> One tier, three ways in<Fn n={2} note={NOTES[1]} /></>}
      right={<Seg size="sm" value={tier} onChange={setTier} options={[{ id: 'large', label: 'Large' }, { id: 'small', label: 'Small and fast' }]} />}
    >
      <div className="overflow-x-auto">
        <table className="w-full min-w-[620px] text-[11.5px]">
          <thead>
            <tr className="h-8 border-b border-line text-left text-[11px] text-dim">
              <th className="pr-3 font-medium">Model</th>
              <th className="pr-3 text-right font-medium">Lab list /M<Fn n={1} note={NOTES[0]} /></th>
              <th className="pr-3 font-medium">Cheapest host · precision · of N</th>
              <th className="pr-3 text-right font-medium">Best /M</th>
              <th className="pr-3 text-right font-medium">100M tokens</th>
              <th className="text-right font-medium">vs cheapest</th>
            </tr>
          </thead>
          <tbody>
            {lines.map((l) => (
              <tr
                key={l.m.id}
                onClick={l.m.open ? () => onRoute(l.m.id) : undefined}
                title={l.m.open ? `Route ${l.m.name} below` : undefined}
                className={cx('border-b border-line-soft last:border-0', l.m.open && 'cursor-pointer hover:bg-hover')}
              >
                <td className="py-1.5 pr-3">
                  <div className="flex items-center gap-1.5">
                    <span className="text-bone">{l.m.name}</span>
                    <span className="text-[10.5px] text-dim">{l.m.open ? 'open' : 'closed'}</span>
                  </div>
                  <div className="text-[10.5px] text-dim">{l.m.lab} · {fmtDate(l.m.created)}</div>
                </td>
                <td className="num py-1.5 pr-3 text-right text-mute">{usd(l.list)}</td>
                <td className="py-1.5 pr-3 text-mute">
                  {l.host
                    ? <>{l.host.provider} <span className="text-dim">· {bitsLabel(bits(l.host.quant))} · of {l.hosts}</span> <span className="num text-bone">{usd(blended(l.host))}</span></>
                    : <span className="text-dim">{l.m.open ? 'no live host' : 'lab or its resellers only'}</span>}
                </td>
                <td className="num py-1.5 pr-3 text-right text-bone">{usd(l.best)}</td>
                <td className="num py-1.5 pr-3 text-right text-mute">${(l.best * 100).toLocaleString('en-US', { maximumFractionDigits: l.best * 100 >= 100 ? 0 : 2, minimumFractionDigits: l.best * 100 >= 100 ? 0 : 2 })}</td>
                <td className={cx('num py-1.5 text-right', l === lo ? 'text-up' : 'text-mute')}>{l === lo ? 'cheapest' : times(l.best / lo.best)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {lo && hi && (
        <p className="mt-3 text-[12px] leading-relaxed text-mute">
          The cheapest line in this tier, <b className="font-medium text-bone">{lo.m.name}</b> at <span className="num text-bone">{usd(lo.best)}</span>/M, lists <span className="num text-up">{pctBelow(lo.best, med)}%</span> below the tier's median (<span className="num">{usd(med)}</span>/M over {lines.length} lines) and <span className="num text-up">{pctBelow(lo.best, hi.best)}%</span> below its dearest.
        </p>
      )}
          </Panel>
  )
}

// ── Newest releases ──────────────────────────────────────────────────────────

const NEWEST = [...SNAP.models].sort((a, b) => b.created - a.created)

export function NewestReleases() {
  const [n, setN] = useState<'10' | '25'>('10')
  return (
    <Panel
      title="Newest releases"
      variant="rail"
      eyebrow="List price, newest first"
      right={<Seg size="sm" value={n} onChange={setN} options={[{ id: '10', label: 'Show 10' }, { id: '25', label: 'Show 25' }]} />}
    >
      {NEWEST.slice(0, Number(n)).map((m) => {
          const hs = m.open ? hostsFor(m.id) : []
          return (
            <div key={m.id} className="grid grid-cols-[48px_minmax(0,1fr)_auto] items-baseline gap-x-3 py-1.5">
              <span className="num text-[10.5px] text-dim">{fmtDate(m.created)}</span>
              <div className="min-w-0">
                <div className="truncate text-[12px] text-bone">{m.name}</div>
                <div className="truncate text-[10.5px] text-dim">
                  {m.lab} · {m.open ? 'open weights' : 'closed'}
                  {hs.length > 0 && <> · {hs.length} {hs.length === 1 ? 'host' : 'hosts'} from <span className="num">{usd(blended(hs[0]))}</span></>}
                </div>
              </div>
              <span className="num text-right text-[11.5px] text-bone">{usd(blended(m))}<span className="text-dim">/M</span></span>
            </div>
          )
        })}
    </Panel>
  )
}
