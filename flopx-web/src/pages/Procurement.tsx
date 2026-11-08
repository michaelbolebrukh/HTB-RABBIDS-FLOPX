import { Fragment, useMemo, useState } from 'react'
import { CircleCheck } from 'lucide-react'
import { GPUS, NOW_IDX, OFFERS, providerById, PROVIDERS, regionById, REGIONS } from '@/data/core'
import { SIGNALS } from '@/data/fund'
import { Distribution } from '@/components/charts'
import { Row } from '@/components/collapse'
import { Btn, cx, dominantFresh, Empty, Fresh, PageHeader, Panel, Seg } from '@/components/ui'
import { PROCUREMENT, SNAPSHOT, TIMING_GATE } from '@/data/handoff'
import { pressureAt } from '@/lib/model'
import { useStore } from '@/lib/store'
import { GPU_MODEL, offerRegionName } from '@/live/gpu'
import { freshOf } from '@/live/provenance'
import { uncertaintyOf, useProcurePlan, type ProcureSpec, type RankedOption, type ScenarioRow } from '@/live/adapt-w5'

const AREAS: Record<string, string[]> = { Any: [], US: ['us-tx', 'us-va', 'us-az', 'us-or'], EU: ['ie-dub', 'uk-lon', 'nl-ams', 'de-fra', 'no-nordics'], 'North America': ['us-tx', 'us-va', 'us-az', 'us-or', 'ca-qc'], APAC: ['sg', 'jp-tyo', 'in-mum', 'au-syd'] }

/** Category hue per provider, so repeated providers in the ranking group by eye. Skips blue (cat1) and orange (cat2). */
const PROVIDER_CATS = [3, 4, 5, 6]
const providerHue = (id: string) => `var(--color-cat${PROVIDER_CATS[Math.max(0, PROVIDERS.findIndex((p) => p.id === id)) % PROVIDER_CATS.length]})`

/** "$41.8k" above ten thousand dollars, "$414.93" below: the hand-off job cost hundreds, a 12-week job costs tens of thousands. */
const fmtCost = (v: number) => (v >= 10_000 ? `$${(v / 1000).toFixed(1)}k` : `$${v.toFixed(2)}`)

/** Design rows (the hand-off snapshot) in the live row shape. */
const SNAPSHOT_ROWS: ScenarioRow[] = PROCUREMENT.plans.map((p) => ({ ...p, live: false }))

function PlanBars({ rows, median, title }: { rows: ScenarioRow[]; median: number | null; title: string }) {
  // The design scaled to a fixed $500 for a $415 to $465 job; the live job's costs set the scale.
  const max = Math.max(...rows.map((r) => r.cost), median ?? 0) * 1.1 || 1
  const x = (v: number) => `${(v / max) * 100}%`
  const rec = rows.find((p) => p.recommended)
  const vsMedian = (v: number) => (median ? (v / median - 1) * 100 : 0)
  const saving = rec && median ? 1 - rec.cost / median : null
  return (
    <div title={title}>
      {rec ? (
        <>
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <span className="text-[11px] text-dim">Recommended</span>
            <span className="text-[14px] font-semibold text-bone">{rec.label}</span>
            <span className="num text-[20px] leading-none text-bone">{fmtCost(rec.cost)}</span>
            {saving != null && saving > 0.0005 && <span className="num rounded-full bg-up/10 px-2 py-0.5 text-[11px] text-up">−{(saving * 100).toFixed(1)}% vs median</span>}
          </div>
          <div className="mt-1 text-[11.5px] text-dim">{rec.note}</div>
        </>
      ) : <div className="text-[11.5px] text-dim">No feasible plan for this job</div>}

      <div className="mt-5 grid grid-cols-[9rem_minmax(0,1fr)_5.5rem] gap-x-4 max-md:grid-cols-[7.5rem_minmax(0,1fr)_4.75rem] max-md:gap-x-2.5">
        <div />
        <div className="relative h-4">
          {median != null && (
            <div className="absolute bottom-0 -translate-x-1/2 whitespace-nowrap text-[10px] text-dim max-md:text-[12px]" style={{ left: x(median) }}>
              median <span className="num">{fmtCost(median).replace(/\.\d\d$/, '')}</span>
            </div>
          )}
        </div>
        <div />
        {rows.map((p) => {
          const d = vsMedian(p.cost)
          return (
            <Fragment key={p.id}>
              <div className="min-w-0 border-t border-line-soft py-2.5" title={p.note}>
                <div className={cx('truncate text-[12.5px]', p.recommended ? 'font-medium text-bone' : p.feasible ? 'text-bone/90' : 'text-mute')}>{p.label}</div>
              </div>
              <div className="relative border-t border-line-soft">
                {median != null && <div className="absolute inset-y-0 border-l border-dashed border-line2" style={{ left: x(median) }} />}
                <div
                  className={cx('absolute left-0 top-1/2 h-2 -translate-y-1/2 rounded-r-full', !p.feasible && 'hatch-inf border border-l-0 border-down/60 text-down/70')}
                  style={{ width: x(p.cost), background: p.feasible ? (p.recommended ? 'var(--color-up)' : 'var(--color-line2)') : undefined }}
                />
              </div>
              <div className="border-t border-line-soft py-2.5 text-right leading-tight">
                <div className={cx('num text-[12.5px]', p.feasible ? 'text-bone' : 'text-mute line-through decoration-dim')}>{fmtCost(p.cost)}</div>
                {p.feasible
                  ? median != null && <div className={cx('num text-[10.5px] max-md:text-[12px]', p.recommended ? 'text-up' : 'text-dim')}>{d < 0 ? '−' : '+'}{Math.abs(d).toFixed(1)}%</div>
                  : <div className="text-[10.5px] text-down max-md:text-[12px]">not allowed</div>}
              </div>
            </Fragment>
          )
        })}
      </div>
    </div>
  )
}

export default function Procurement() {
  const { route, toast, live } = useStore()
  const [gpu, setGpu] = useState(route.params.gpu ?? 'h100')
  const [n, setN] = useState(+(route.params.n ?? 64))
  const [weeks, setWeeks] = useState(+(route.params.weeks ?? 12))
  const [deadline, setDeadline] = useState(30)
  const [area, setArea] = useState('Any')
  const [ib, setIb] = useState(false)
  const [minRel, setMinRel] = useState(80)
  const [budget, setBudget] = useState(1500)
  const [picked, setPicked] = useState<string | null>(null)
  const [requests, setRequests] = useState<Record<string, 'requested' | 'held'>>({})
  const hours = weeks * 168
  const spec: ProcureSpec = { gpu, n, weeks, deadlineDays: deadline, area, ib, minRel, budgetK: budget }
  // The backend's costed plan for this spec (POST /flopx/procure/plan, three scenario variants) and
  // the index forecast for the GPU. Until it lands, and if it fails, the design's own ranking below
  // stays on screen with each offer's own badge.
  const planned = useProcurePlan(spec)
  const planLive = planned.status === 'live' && planned.plan != null
  // The design's client-side ranking over OFFERS (W0's live offers, or the design's illustrative
  // ones for a GPU with no coverage), in the live row shape, as the fallback.
  const fallback = useMemo((): RankedOption[] => OFFERS.filter((o) => o.gpu === gpu && (!ib || o.interconnect === 'InfiniBand 3.2T') && (area === 'Any' || AREAS[area].includes(o.region)) && (!Number.isFinite(providerById(o.provider)?.reliability ?? NaN) || providerById(o.provider)!.reliability >= minRel) && o.contract !== 'Spot')
    .map((o) => {
      const pr = providerById(o.provider)
      const r = regionById(o.region)!
      const p = pressureAt(r, NOW_IDX) ?? 50
      const fixed = o.contract !== 'On-demand'
      const drift = fixed ? 0 : -0.012 * Math.min(3, weeks / 4)
      const total = o.price * n * hours * (1 + drift)
      const spread = fixed ? 0.02 : 0.09 + Math.max(0, p - 60) / 300
      const availRisk = o.available === 'Available' ? 'Low' : o.available === 'Limited' ? 'Medium' : 'High'
      const risks = [o.available !== 'Available' && `Capacity ${o.available.toLowerCase()} · regional pressure ${p.toFixed(0)}`, o.contract === '1-yr reserved' && weeks < 52 && 'Commit exceeds workload, so you pay for idle time', !fixed && 'On-demand price may change', pr?.type === 'Hyperscaler' && 'List price: negotiated rate likely lower', o.egress !== 'Free' && o.egress !== 'n/a' && `Egress ${o.egress}`].filter(Boolean) as string[]
      const commitTotal = o.contract === '1-yr reserved' ? o.price * n * 8760 : o.contract === '1-mo reserved' ? o.price * n * Math.ceil(weeks / 4.3) * 730 : total
      return {
        id: o.id, providerId: o.provider, providerName: pr?.name ?? o.provider, regionName: offerRegionName(o), contract: o.contract, interconnect: o.interconnect, sla: o.sla, price: o.price, observed: o.observed, freshness: o.freshness,
        reliability: pr?.reliability ?? NaN, availRisk, available: o.available, risks, tradeOffs: [], marketReading: o.note ? `Vast.ai asks: a current ${o.note}` : undefined,
        total: Math.max(total, commitTotal), lo: total * (1 - spread), hi: Math.max(total, commitTotal) * (1 + spread / 2), live: !!o.live, interruptible: false, meetsDeadline: null, sourceUrl: o.sourceUrl,
      } satisfies RankedOption
    })
    .sort((a, b) => a.total - b.total), [gpu, n, weeks, area, ib, minRel, hours, live])
  const options: RankedOption[] = planLive ? planned.plan!.options : fallback
  // The status most ranked rows share is said once in the eyebrow; only a row that differs keeps a chip.
  const optionsFresh = dominantFresh(options.slice(0, 7).map((o) => o.freshness))
  const over = (x: number) => x / 1000 > budget
  const sig = SIGNALS.find((s) => s.id === 'sig-h100')!
  const ref = options.length ? options.reduce((a, b) => (b.total < a.total ? b : a)) : undefined
  const gpuRow = GPUS.find((g) => g.id === gpu)
  const gpuName = gpuRow?.name ?? gpu
  /** "H100", as the design's GPU buttons and the panel eyebrow abbreviate it. */
  const gpuShort = gpuName.replace(' SXM', '').replace(' 80GB', '')
  // The 1M uncertainty panel: the backend forecast when it answers, else the design's example, badged.
  const fc = planned.forecast
  const fcOk = fc?.kind === 'ok' ? fc.data : null
  const dist = fcOk && gpuRow?.live ? uncertaintyOf(fcOk, gpuRow.live.current) : null
  const fcReason = !GPU_MODEL[gpu] ? `FLOPX has no index for ${gpuName}` : fc?.kind === 'refused' ? `FLOPX refuses a ${fc.detail.horizon ?? 30}-day forecast for ${gpuName}: ${fc.detail.history_points ?? 0} of ${fc.detail.required_points ?? 60} points of history` : fc?.kind === 'ok' && !dist ? `No current ${gpuName} index level to anchor the forecast` : fc?.kind === 'failed' ? `Forecast unavailable (${fc.error})` : 'Forecast pending'
  const scenarios = planLive ? planned.scenarios : null
  const gate = planned.gate
  const eyebrow = planLive
    ? planned.plan!.tier === 'live'
      ? `${options.length} live asks can run this job · expected total cost · FLOPX planner ${planned.plan!.asOf} UTC`
      : planned.plan!.tier === 'sample'
        ? `No live ask lists ${n} × ${gpuName}; ${options.length} FLOPX sample asks shown, illustrative · expected total cost`
        : `No ask can run this job · FLOPX planner ${planned.plan!.asOf} UTC`
    : `${options.length} observed offers meet constraints · expected total cost`

  const top = options.slice(0, 7)
  const sel = top.some((x) => x.id === picked) ? picked : top[0]?.id
  /** An option's expanded detail: the desktop table's second row and the phone card's open body share it. */
  const detail = (x: RankedOption, st: 'requested' | 'held' | undefined) => (
    <>
    <div className="flex items-start justify-between gap-4 max-md:flex-col max-md:gap-2">
      <div>Observed {x.observed} · {x.interconnect} · SLA {x.sla} · est. range <span className="text-bone">${(x.lo / 1000).toFixed(0)}–{(x.hi / 1000).toFixed(0)}k</span>{over(x.total) && <span className="text-down"> · over budget</span>}</div>
      <div className="shrink-0 max-md:w-full">
        {st === 'requested' ? (
          <span className="text-dec">Quote requested</span>
        ) : st === 'held' ? (
          <span className="text-up"><CircleCheck size={11} className="mr-1 inline" />48h hold (simulated)</span>
        ) : (
          <Btn size="sm" variant="primary" className="max-md:h-11 max-md:w-full max-md:text-[13px]" disabled={x.available === 'Sold out'} onClick={() => { setRequests((r) => ({ ...r, [x.id]: 'requested' })); toast(`Quote request sent to ${x.providerName} (simulated)`, 'info') }}>{x.available === 'Sold out' ? 'Sold out' : 'Request quote'}</Btn>
        )}
      </div>
    </div>
    {x.risks.length > 0 && <ul className="mt-1 space-y-0.5 text-dim">{x.risks.map((r) => <li key={r}>– {r}</li>)}</ul>}
    {st === 'requested' && <div className="mt-1.5 text-dec">Not reserved · awaiting provider · <button className="cursor-pointer text-mute underline max-md:inline-flex max-md:min-h-11 max-md:items-center" onClick={() => { setRequests((r) => ({ ...r, [x.id]: 'held' })); toast(`${x.providerName}: capacity hold confirmed (simulated, 48h)`, 'ok') }}>Simulate provider reply</button></div>}
    {st === 'held' && <div className="mt-1.5 text-up">Not purchased</div>}
    </>
  )

  return (
    <div>
      <PageHeader title="Compute procurement" sub="Observed offers for one workload, ranked by expected total cost. Nothing is reserved until a provider confirms." />
      <Row cols={['320px', 1]} at="xl">
        <Panel title="Workload requirements" className="self-start" bodyClass="!p-0">
          <Section title="Hardware">
            <div className="grid grid-cols-3 gap-1.5" role="radiogroup" aria-label="GPU">
              {GPUS.map((g) => (
                <button key={g.id} role="radio" aria-checked={gpu === g.id} onClick={() => setGpu(g.id)}
                  className={cx('cursor-pointer rounded-[4px] border px-2 py-1.5 text-left transition-colors max-md:min-h-12 max-md:py-2', gpu === g.id ? 'border-act bg-act/10' : 'border-line hover:border-line2 hover:bg-hover')}>
                  <div className={cx('text-[12.5px] font-medium', gpu === g.id ? 'text-act' : 'text-mute')}>{g.name.replace(' SXM', '').replace(' 80GB', '')}</div>
                  <div className="num text-[10px] text-dim max-md:text-[12px]">{g.memory.split(' ').slice(0, 2).join(' ')}</div>
                </button>
              ))}
            </div>
            <Range label="GPU count" value={n} display={`${n} GPUs`} min={8} max={512} step={8} onChange={setN} />
          </Section>
          <Section title="Schedule">
            <Range label="Duration" value={weeks} display={`${weeks} wk`} min={1} max={52} onChange={setWeeks} />
            <Range label="Must start within" value={deadline} display={`${deadline} d`} min={3} max={120} onChange={setDeadline} />
          </Section>
          <Section title="Constraints">
            <div>
              <div className="mb-1.5 text-[12px] text-mute">Location</div>
              <Seg className="flex w-full [&>button]:flex-auto [&>button]:whitespace-nowrap max-md:h-11 max-md:[&>button]:text-[13px]" size="sm" value={area} onChange={setArea} options={Object.keys(AREAS).map((a) => ({ id: a, label: a === 'North America' ? 'N. Am.' : a, title: a }))} />
            </div>
            <Switch label="Require InfiniBand" hint="Needed for multi-node training" checked={ib} onChange={setIb} />
            <Range label="Min. provider reliability" value={minRel} display={`${minRel}/100`} min={60} max={97} onChange={setMinRel} />
            <Range label="Budget" value={budget} display={budget >= 1000 ? `$${(budget / 1000).toFixed(2)}M` : `$${budget}k`} min={100} max={5000} step={50} onChange={setBudget} />
          </Section>
          <dl className="grid grid-cols-2 border-t border-line bg-raise/60">
            <Stat label="GPU-hours" value={(n * hours).toLocaleString()} />
            <Stat label="Cheapest" value={ref ? `$${Math.round(ref.total / 1000)}k` : '—'} tone={ref ? (over(ref.total) ? 'down' : 'up') : undefined} sub={ref ? (over(ref.total) ? 'over budget' : 'within budget') : 'no match'} />
          </dl>
        </Panel>

        <div className="space-y-5">
          <Panel title="Ranked options" truth="obs" eyebrow={<span title={eyebrow}>{`${Math.min(7, options.length)} of ${options.length} offers`}{optionsFresh && <Fresh quiet className="ml-2" f={optionsFresh} label={planLive && planned.plan!.tier === 'sample' ? 'Illustrative · sample tier' : undefined} />}</span>} bodyClass="p-0 max-md:p-0">
            {options.length === 0 ? (
              <Empty className="m-4" tone="warn" title="No observed offers match" body="Relax the location, fabric or reliability constraints. FLOPX will not invent capacity that hasn't been quoted." />
            ) : (
              <>
              {/* Phone: the ranked options as cards; tap one to open the same detail the table's expanded row shows. */}
              <div className="divide-y divide-line-soft md:hidden">
                {top.map((x, i) => {
                  const st = requests[x.id]
                  const open = sel === x.id
                  return (
                    <div key={x.id} className={cx(open && 'bg-warn/[0.07] shadow-[inset_2px_0_0_var(--color-warn)]')}>
                      <button type="button" onClick={() => setPicked(x.id)} aria-expanded={open} className="block w-full cursor-pointer px-4 py-3 text-left">
                        <div className="flex items-start justify-between gap-3">
                          <div className="min-w-0">
                            <div className="flex items-center gap-2 text-[13.5px]"><span className="num text-[12px] text-dim">{i + 1}</span><span className="inline-block size-[8px] shrink-0 rounded-[1.5px]" style={{ background: providerHue(x.providerId) }} /><span className="font-medium text-bone">{x.providerName}</span></div>
                            <div className="mt-0.5 text-[12px] text-mute">{x.regionName}{x.freshness !== optionsFresh && <Fresh quiet f={x.freshness} className="ml-1.5" />}</div>
                          </div>
                          <div className="shrink-0 text-right"><div className="text-[11px] text-dim">Total ($k)</div><div className={cx('num text-[17px]', over(x.total) ? 'text-down' : 'text-bone')}>{(x.total / 1000).toFixed(0)}</div></div>
                        </div>
                        <div className="mt-2 grid grid-cols-4 gap-2 text-[12px]">
                          <div className="col-span-2 min-w-0"><div className="text-[11px] text-dim">Contract</div><div className="truncate text-mute">{x.contract}</div></div>
                          <div><div className="text-[11px] text-dim">$/GPU-hr</div><div className="num text-bone">{x.price.toFixed(2)}</div></div>
                          <div><div className="text-[11px] text-dim">Reliability</div><div className="[&>span]:text-left"><Reliability v={x.reliability} /></div></div>
                          <div className="col-span-4"><span className="mr-2 text-[11px] text-dim">Avail. risk</span><RiskDot risk={x.availRisk} /></div>
                        </div>
                      </button>
                      {open && <div className="px-4 pb-3 text-[12px] text-mute tabular-nums">{detail(x, st)}</div>}
                    </div>
                  )
                })}
              </div>
              <div className="overflow-x-auto max-md:hidden">
                <table className="w-full text-[12px]">
                  <thead>
                    <tr className="h-8 border-b border-line text-[11px] whitespace-nowrap text-dim">
                      <th className="w-8 px-3 text-right font-medium">#</th>
                      <th className="px-3 text-left font-medium" title="Quotes are live unless marked">Provider · region</th>
                      <th className="px-3 text-left font-medium">Contract</th>
                      <th className="px-3 text-right font-medium">$/GPU-hr</th>
                      <th className="px-3 text-right font-medium">Reliability</th>
                      <th className="px-3 text-left font-medium">Avail. risk</th>
                      <th className="px-3 text-right font-medium">Total ($k)</th>
                    </tr>
                  </thead>
                  <tbody>
                    {top.map((x, i) => {
                      const st = requests[x.id]
                      const open = sel === x.id
                      return (
                        <Fragment key={x.id}>
                          <tr onClick={() => setPicked(x.id)} aria-selected={open} className={cx('h-9 cursor-pointer border-b border-line-soft', open ? 'bg-warn/[0.07] shadow-[inset_2px_0_0_var(--color-warn)]' : 'hover:bg-hover')}>
                            <td className="num px-3 text-right text-dim">{i + 1}</td>
                            <td className="px-3 whitespace-nowrap"><span className="mr-2 inline-block size-[7px] rounded-[1.5px] align-[1px]" style={{ background: providerHue(x.providerId) }} /><span className="font-medium text-bone">{x.providerName}</span><span className="text-mute"> · {x.regionName}</span>{x.freshness !== optionsFresh && <Fresh quiet f={x.freshness} className="ml-1.5" />}</td>
                            <td className="px-3 whitespace-nowrap text-mute">{x.contract}</td>
                            <td className="num px-3 text-right text-bone">{x.price.toFixed(2)}</td>
                            <td className="px-3"><Reliability v={x.reliability} /></td>
                            <td className="px-3"><RiskDot risk={x.availRisk} /></td>
                            <td className="px-3 text-right whitespace-nowrap"><span className={cx('num text-[13px]', over(x.total) ? 'text-down' : 'text-bone')}>{(x.total / 1000).toFixed(0)}</span></td>
                          </tr>
                          {open && (
                            <tr className="border-b border-line-soft bg-warn/[0.07] shadow-[inset_2px_0_0_var(--color-warn)]">
                              <td />
                              <td colSpan={6} className="px-3 pt-0.5 pb-3 text-[11.5px] text-mute tabular-nums">
                                {detail(x, st)}
                              </td>
                            </tr>
                          )}
                        </Fragment>
                      )
                    })}
                  </tbody>
                </table>
              </div>
              </>
            )}
          </Panel>

          {(ref || scenarios) && (
            <Row cols={[1, '320px']} at="lg">
              <Panel title="Scheduling scenarios" truth="fct" right={scenarios ? <Fresh quiet f={scenarios.freshness} label={scenarios.freshness === 'illustrative' ? 'Illustrative · sample tier' : `${scenarios.freshness === 'live' ? 'Live asks' : 'Asks'} ${scenarios.asOf} UTC`} /> : <Fresh quiet f="historical" label={SNAPSHOT.short} />}>
                {scenarios ? <PlanBars rows={scenarios.rows} median={scenarios.median} title={`${scenarios.job}. Median over ${scenarios.n} feasible ${scenarios.freshness === 'illustrative' ? 'FLOPX sample' : 'live'} asks`} /> : <PlanBars rows={SNAPSHOT_ROWS} median={PROCUREMENT.median} title={`${PROCUREMENT.job} (hand-off example). ${PROCUREMENT.jobNote}`} />}
                {gate ? (
                  <div className="mt-4 border-t border-line-soft pt-3 text-[11.5px] text-mute tabular-nums" title={gate.note}>
                    Timing gate · <span className={gate.open ? 'text-up' : 'text-down'}>{gate.open ? 'Open' : 'Closed'}</span> · {gate.historyPoints}/{gate.historyNeeded} daily {gpuShort} index points · {gate.skillPct == null ? 'skill not yet measurable' : `${gate.skillPct >= 0 ? '+' : ''}${gate.skillPct.toFixed(1)}% of +${gate.skillNeeded}% skill`}
                  </div>
                ) : (
                  <div className="mt-4 border-t border-line-soft pt-3 text-[11.5px] text-mute tabular-nums" title={TIMING_GATE.note}>
                    Timing gate · <span className={TIMING_GATE.open ? 'text-up' : 'text-down'}>{TIMING_GATE.open ? 'Open' : 'Closed'}</span> · {TIMING_GATE.historyHours}/{TIMING_GATE.historyNeeded} h history · +{TIMING_GATE.skillPct}% of +{TIMING_GATE.skillNeeded}% skill <span className="text-dim">({SNAPSHOT.short})</span>
                  </div>
                )}
              </Panel>
              <Panel title={`${gpuShort} 1M forecast`} truth="fct" right={dist && fcOk ? <Fresh quiet f={freshOf(fcOk.provenance).freshness} label={fcOk.exploratory ? 'Exploratory' : undefined} /> : <span title={fcReason}><Fresh quiet f="illustrative" /></span>}>
                {dist ? (
                  <Distribution p10={dist.p10} mean={dist.mean} p90={dist.p90} height={160} label={`${fcOk!.horizon}-day change in the index (${fcOk!.chosen})`} />
                ) : (
                  <>
                    <Distribution p10={sig.horizons['1M'].p10} mean={sig.horizons['1M'].mean} p90={sig.horizons['1M'].p90} height={160} />
                    <div className="font-mono text-[10px] text-dim max-md:text-[12px]">{fcReason}. Design example shown, not a FLOPX forecast.</div>
                  </>
                )}
              </Panel>
            </Row>
          )}
          <p className="text-[11px] text-dim">{coverageNote()}{planLive && planned.plan!.caveats.length > 0 && <> FLOPX planner: {planned.plan!.caveats.join(' · ')}.</>}</p>
        </div>
      </Row>
    </div>
  )
}

/** Form group: a plain heading over its controls. */
function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="space-y-4 border-b border-line-soft px-4 py-4 last:border-b-0">
      <div className="text-[12.5px] font-semibold text-bone">{title}</div>
      {children}
    </div>
  )
}

/** Labelled slider: thin neutral track with a filled portion, value on the right. */
function Range({ label, value, display, min, max, step = 1, onChange }: { label: string; value: number; display: string; min: number; max: number; step?: number; onChange: (v: number) => void }) {
  const pct = ((value - min) / (max - min)) * 100
  // "64 GPUs" → value in the display face, unit dimmed.
  const [, num = display, unit = ''] = display.match(/^(\S+)\s*(.*)$/) ?? []
  return (
    <label className="block">
      <div className="mb-1.5 flex items-baseline justify-between">
        <span className="text-[12px] text-mute">{label}</span>
        <span className="num text-[14px] text-bone">{num}{unit && <span className="ml-1 font-sans text-[11px] font-normal text-dim">{unit}</span>}</span>
      </div>
      <input type="range" className="range max-md:h-11" min={min} max={max} step={step} value={value} onChange={(e) => onChange(+e.target.value)} style={{ '--pct': `${pct}%` } as React.CSSProperties} />
    </label>
  )
}

function Switch({ label, hint, checked, onChange }: { label: string; hint?: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <button type="button" role="switch" aria-checked={checked} onClick={() => onChange(!checked)} className="flex w-full cursor-pointer items-center justify-between gap-3 text-left max-md:min-h-11">
      <span>
        <span className="block text-[12px] text-mute">{label}</span>
        {hint && <span className="block text-[10.5px] text-dim max-md:text-[12px]">{hint}</span>}
      </span>
      <span className={cx('relative h-[18px] w-[32px] shrink-0 rounded-full border transition-colors', checked ? 'border-act bg-act' : 'border-line2 bg-ink')}>
        <span className={cx('absolute top-[2px] h-3 w-3 rounded-full transition-all', checked ? 'left-[15px] bg-ink' : 'left-[2px] bg-dim')} />
      </span>
    </button>
  )
}

function Stat({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: 'up' | 'down' }) {
  return (
    <div className="border-r border-line px-3 py-2.5 last:border-r-0">
      <dt className="text-[10px] text-dim max-md:text-[12px]">{label}</dt>
      <dd className={cx('num text-[14px]', tone === 'up' ? 'text-up' : tone === 'down' ? 'text-down' : 'text-bone')}>{value}</dd>
      {sub && <dd className="text-[9.5px] text-dim max-md:text-[12px]">{sub}</dd>}
    </div>
  )
}

/** Reliability score; only scores below 85 are flagged. */
function Reliability({ v }: { v: number }) {
  if (!Number.isFinite(v)) return <span className="block text-right text-dim">not loaded</span>
  return <span className={cx('num block text-right', v < 85 ? 'text-warn' : 'text-bone')}>{v}</span>
}

function RiskDot({ risk }: { risk: string }) {
  const c = risk === 'High' ? 'var(--color-down)' : risk === 'Medium' ? 'var(--color-warn)' : 'var(--color-up)'
  return <span className="inline-flex items-center gap-1.5 whitespace-nowrap" style={{ color: c }}><span className="size-1.5 rounded-full" style={{ background: c }} />{risk}</span>
}

/** "9 regions fully covered. Sydney quotes stale; Johannesburg has none." composed from the region list's own quality flags. */
function coverageNote(): string {
  const good = REGIONS.filter((r) => r.quality === 'good').length
  const stale = REGIONS.filter((r) => r.quality === 'stale').map((r) => r.short)
  const none = REGIONS.filter((r) => r.quality === 'none').map((r) => r.short)
  const parts = [`${good} regions fully covered.`]
  if (stale.length) parts.push(`${stale.join(', ')} quotes stale${none.length ? ';' : '.'}`)
  if (none.length) parts.push(`${none.join(', ')} ${none.length === 1 ? 'has' : 'have'} none.`)
  return parts.join(' ')
}
