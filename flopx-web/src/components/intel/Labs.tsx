// Layer 1 panels (labs): what each lab lists, closed vs open, cheapest way into a tier, newest releases.
import { useState, type ReactNode } from 'react'
import { scaleLog } from 'd3-scale'
import { Panel, Seg, TruthTag, cx } from '@/components/ui'
import { beeswarm, useWidth } from '@/components/charts'
import { useChartStyle } from '@/lib/chartStyle'
import { TIERS, bits, bitsLabel, blended, closedVsOpen, labLadder, median, standardLive, tierModels, type EndpointRow, type Model } from '@/lib/tokenmarket'
import { CLOSED, OPEN, SNAP, fmtDate, times, usd } from '@/components/intel/shared'

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

const LADDER_COLS = 'grid grid-cols-[minmax(72px,112px)_minmax(0,1fr)_88px_64px] items-center gap-x-3'

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
      eyebrow="Every model a lab lists · blended $/M · log scale"
      right={<Seg size="sm" value={sort} onChange={setSort} options={[{ id: 'median', label: 'By median' }, { id: 'models', label: 'By models' }]} />}
    >
      <div className="overflow-x-auto">
        <div className="min-w-[420px]">
          <div className={cx(LADDER_COLS, 'mb-1 font-mono text-[9.5px] tracking-wider text-dim uppercase')}>
            <span>Lab</span>
            <div ref={ref}>
              <svg width={w} height={12} className="block overflow-visible">
                {TICKS.map((t) => <text key={t} x={x(t)} y={9} textAnchor="middle" className={`fill-dim ${fontClass}`} fontSize={9}>{tickLabel(t)}</text>)}
              </svg>
            </div>
            <span className="text-right whitespace-nowrap">Models · open</span>
            <span className="text-right">Median /M</span>
          </div>
          {rows.map((r) => {
            const { lo, hi } = extremes(r.models)
            const on = r.lab === sel
            return (
              <button
                key={r.lab}
                onClick={() => setSel(on ? null : r.lab)}
                aria-pressed={on}
                className={cx(LADDER_COLS, 'h-[24px] w-full cursor-pointer rounded-[3px] text-left', on ? 'bg-raise' : 'hover:bg-raise/60')}
              >
                <span className={cx('truncate pl-1 text-[11.5px]', on ? 'text-bone' : 'text-mute')}>{r.lab}</span>
                <svg width={w} height={24} className="block overflow-visible">
                  {TICKS.map((t) => <line key={t} x1={x(t)} x2={x(t)} y1={0} y2={24} stroke="var(--color-line)" strokeOpacity={0.6} />)}
                  <line x1={x(r.min)} x2={x(r.max)} y1={12} y2={12} stroke="var(--color-line2)" />
                  {r.models.map((m) => (
                    <circle key={m.id} cx={x(blended(m))} cy={12} r={3} fill={m.open ? OPEN : CLOSED} fillOpacity={0.75}>
                      <title>{`${m.name} · ${usd(blended(m))}/M${m.open ? ' · open weights' : ''}`}</title>
                    </circle>
                  ))}
                  <circle cx={x(blended(lo))} cy={12} r={5.5} fill="none" stroke="var(--color-up)" strokeWidth={1.2} />
                  <circle cx={x(blended(hi))} cy={12} r={5.5} fill="none" stroke="var(--color-down)" strokeWidth={1.2} />
                  <line x1={x(r.median)} x2={x(r.median)} y1={4} y2={20} stroke="var(--color-bone)" strokeWidth={1.5} />
                </svg>
                <span className="num text-right text-[11px] text-mute">{r.models.length} · {r.open}</span>
                <span className="num text-right text-[11px] text-bone">{usd(r.median)}</span>
              </button>
            )
          })}
        </div>
      </div>
      <div className="mt-2 min-h-[18px] text-[11.5px] text-mute">
        {picked ? (() => {
          const { lo, hi } = extremes(picked.models)
          return <><b className="font-medium text-bone">{picked.lab}</b>: cheapest {lo.name} at <span className="num text-bone">{usd(blended(lo))}</span>/M · dearest {hi.name} at <span className="num text-bone">{usd(blended(hi))}</span>/M · {times(blended(hi) / blended(lo))} apart</>
        })() : <span className="text-dim">Click a lab to see its cheapest and dearest model.</span>}
      </div>
      <Legend items={[
        { mark: <Dot color={CLOSED} />, label: 'closed model' },
        { mark: <Dot color={OPEN} />, label: 'open weights' },
        { mark: <Ring color="var(--color-up)" />, label: 'cheapest' },
        { mark: <Ring color="var(--color-down)" />, label: 'dearest' },
        { mark: <span className="inline-block h-3 w-[1.5px] bg-bone" />, label: 'median' },
        { mark: null, label: 'click a lab' },
      ]} />
      <p className="mt-2 text-[11px] leading-relaxed text-dim">Labs with three or more text models. {SNAP.aliases} "latest" alias routes (such as ~anthropic/claude-opus-latest) are left out because they point at models already listed.</p>
    </Panel>
  )
}

function Dot({ color }: { color: string }) {
  return <svg width={8} height={8} className="shrink-0"><circle cx={4} cy={4} r={3} fill={color} /></svg>
}
function Ring({ color }: { color: string }) {
  return <svg width={12} height={12} className="shrink-0"><circle cx={6} cy={6} r={5} fill="none" stroke={color} strokeWidth={1.2} /></svg>
}
function Legend({ items }: { items: { mark: ReactNode; label: string }[] }) {
  return (
    <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[10px] text-dim">
      {items.map((it) => <span key={it.label} className="inline-flex items-center gap-1.5">{it.mark}{it.label}</span>)}
    </div>
  )
}

// ── Closed vs open ───────────────────────────────────────────────────────────

const BAR_LABS = ['Anthropic', 'OpenAI', 'Google', 'xAI', 'Mistral', 'Meta', 'Qwen (Alibaba)', 'Moonshot AI', 'Z.ai', 'DeepSeek', 'NVIDIA']

function labBars() {
  return BAR_LABS.flatMap((lab) => {
    const ms = SNAP.models.filter((m) => m.lab === lab)
    if (!ms.length) return []
    const open = ms.filter((m) => m.open).length
    const color = open === 0 ? CLOSED : open === ms.length ? OPEN : 'var(--color-mute)'
    return [{ lab, n: ms.length, median: median(ms.map(blended)), color }]
  })
}

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
  const bars = labBars()
  const barMax = Math.max(...bars.map((b) => b.median))
  return (
    <Panel title="Closed frontier vs open weights" eyebrow={`${CVO.closed.length} closed · ${CVO.open.length} open-weight models · log scale`}>
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
          {TICKS.map((t) => <text key={t} x={x(t)} y={axisY + 12} textAnchor="middle" className={`fill-dim ${fontClass}`} fontSize={9}>{tickLabel(t)}</text>)}
        </svg>
      </div>
      <p className="mt-2 text-[12px] leading-relaxed text-mute">
        At the median a closed model lists at <span className="num text-bone">{times(CVO.ratio)}</span> an open-weight one, but the ranges overlap: OpenAI itself spans <span className="num text-bone">{usd(blended(oa.lo))}</span> ({oa.lo.name}) to <span className="num text-bone">{usd(blended(oa.hi))}</span> ({oa.hi.name}) per million tokens.
      </p>
      <div className="mt-4 mb-2 font-mono text-[9.5px] tracking-wider text-dim uppercase">Lab median, $/M</div>
      <div className="space-y-1">
        {bars.map((b) => (
          <div key={b.lab} className="grid h-[20px] grid-cols-[minmax(0,140px)_minmax(0,1fr)_56px] items-center gap-3" title={`${b.lab}: ${b.n} models, median ${usd(b.median)}/M`}>
            <span className="truncate text-[11.5px] text-mute">{b.lab} <span className="num text-dim">· {b.n}</span></span>
            <div className="relative h-2 rounded-sm bg-line/60">
              <div className="absolute inset-y-0 left-0 rounded-sm" style={{ width: `${(b.median / barMax) * 100}%`, background: b.color }} />
            </div>
            <span className="num text-right text-[11px] text-bone">{usd(b.median)}</span>
          </div>
        ))}
      </div>
      <p className="mt-2 text-[11px] text-dim">Bar colour: all closed (bone) · all open (cyan) · mixed (grey). Linear scale.</p>
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
      eyebrow={<><TruthTag truth="dec" compact /> One tier, three ways in: a closed lab, an open model's default route, or the cheapest host for it</>}
      right={<Seg size="sm" value={tier} onChange={setTier} options={[{ id: 'large', label: 'Large' }, { id: 'small', label: 'Small and fast' }]} />}
    >
      <div className="overflow-x-auto">
        <table className="w-full min-w-[620px] text-[11.5px]">
          <thead>
            <tr className="border-b border-line text-left font-mono text-[9.5px] tracking-wider text-dim uppercase">
              <th className="py-1.5 pr-3 font-normal">Model</th>
              <th className="py-1.5 pr-3 text-right font-normal">Lab list /M</th>
              <th className="py-1.5 pr-3 font-normal">Cheapest host</th>
              <th className="py-1.5 pr-3 text-right font-normal">Best /M</th>
              <th className="py-1.5 pr-3 text-right font-normal">100M tokens</th>
              <th className="py-1.5 text-right font-normal">vs cheapest</th>
            </tr>
          </thead>
          <tbody>
            {lines.map((l) => (
              <tr
                key={l.m.id}
                onClick={l.m.open ? () => onRoute(l.m.id) : undefined}
                title={l.m.open ? `Route ${l.m.name} below` : undefined}
                className={cx('border-b border-line/60 last:border-0', l.m.open && 'cursor-pointer hover:bg-raise')}
              >
                <td className="py-1.5 pr-3">
                  <div className="flex items-center gap-1.5">
                    <span className="text-bone">{l.m.name}</span>
                    <span className={cx('rounded-[3px] border px-1 font-mono text-[9px] leading-[13px]', l.m.open ? 'border-fct/40 text-fct' : 'border-line2 text-dim')}>{l.m.open ? 'open' : 'closed'}</span>
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
      <p className="mt-2 text-[11px] leading-relaxed text-dim">Tier = each lab's product line by name, latest release. FLOPX does not measure capability, so this compares list prices, not quality. 100M tokens at the 3:1 blend; output-heavy work costs more. Click an open model to route it below.</p>
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
      eyebrow="Latest models in the catalog · list price"
      right={<Seg size="sm" value={n} onChange={setN} options={[{ id: '10', label: 'Show 10' }, { id: '25', label: 'Show 25' }]} />}
    >
      <ul>
        {NEWEST.slice(0, Number(n)).map((m) => {
          const hs = m.open ? hostsFor(m.id) : []
          return (
            <li key={m.id} className="grid grid-cols-[48px_minmax(0,1fr)_auto] items-baseline gap-x-3 border-b border-line/60 py-1.5 last:border-0">
              <span className="num text-[10.5px] text-dim">{fmtDate(m.created)}</span>
              <div className="min-w-0">
                <div className="truncate text-[12px] text-bone">{m.name}</div>
                <div className="truncate text-[10.5px] text-dim">
                  {m.lab} · <span className={m.open ? 'text-fct' : undefined}>{m.open ? 'open weights' : 'closed'}</span>
                  {hs.length > 0 && <> · {hs.length} {hs.length === 1 ? 'host' : 'hosts'} from <span className="num">{usd(blended(hs[0]))}</span></>}
                </div>
              </div>
              <span className="num text-right text-[11.5px] text-bone">{usd(blended(m))}<span className="text-dim">/M</span></span>
            </li>
          )
        })}
      </ul>
    </Panel>
  )
}
