import { useMemo, useState } from 'react'
import { energyStripCounts } from '@/lib/energyStrip'
import { AS_OF, MONTHS, NOW_IDX, regionById } from '@/data/core'
import { ENERGY, PROJECTS, type Stage } from '@/data/infra'
import { secByTicker } from '@/data/finance'
import { METER_H, METER_W, PowerMeter, StagePip, WorldMap } from '@/components/WorldMap'
import { POWER_HOT, STAGE_ORDER } from '@/lib/powerMap'
import { SCALED_HISTORY_LABEL, TimeChart } from '@/components/charts'
import { Foldable, Row } from '@/components/collapse'
import { cx, Delta, dominantFresh, EntityLink, Fresh, PageHeader, Panel, Seg, Spark, TruthGlyph, TruthTag } from '@/components/ui'
import { useStore } from '@/lib/store'
import { axisAnchor, ym } from '@/live/dates'
import { ENERGY_SUMMARY as ES } from '@/data/handoff'

// Gantt axis: Jan 2024 → Dec 2028
const yrs = ['2024', '2025', '2026', '2027', '2028']
const parseWhen = (s: string): number | null => {
  const q = s.match(/Q([1-4])\s*(20\d\d)/)
  if (q) return (+q[2] - 2024) * 12 + (+q[1] - 1) * 3 + 1
  const h = s.match(/H([12])\s*(20\d\d)/)
  if (h) return (+h[2] - 2024) * 12 + (+h[1] - 1) * 6 + 3
  const y = s.match(/(20\d\d)/)
  if (y) return (+y[1] - 2024) * 12 + 6
  return null
}
const buildCols = 'grid grid-cols-[minmax(0,1.6fr)_118px_minmax(100px,0.8fr)_80px_minmax(0,1.8fr)] gap-4'
/** Build stage is encoded once, by the timeline bar: one hue (cat3) that fades and loosens as the project gets less certain. */
const STAGE_BAR: Record<Stage, string> = {
  operational: 'bg-cat3',
  construction: 'border border-cat3 bg-cat3/20',
  proposed: 'border border-dashed border-cat3/60',
  announced: 'border border-dotted border-dim',
}

export default function Energy() {
  const { route, inspect, navigate, setXray, live } = useStore()
  const [market, setMarket] = useState(route.params.market ?? 'ercot')
  const [stage, setStage] = useState<'all' | Stage>('all')
  const [view, setView] = useState<'markets' | 'build'>('markets')
  const m = ENERGY.find((e) => e.id === market)!
  const projects = PROJECTS.filter((p) => stage === 'all' || p.stage === stage).sort((a, b) => STAGE_ORDER.indexOf(a.stage) - STAGE_ORDER.indexOf(b.stage))
  const linkedProjects = PROJECTS.filter((p) => m.regions.includes(p.region))
  const linkedTickers = Array.from(new Set(m.regions.flatMap((r) => regionById(r)!.documented.map((d) => d.t))))
  const cur = (e: { currency: string }) => (e.currency === 'EUR' ? '€' : '$')
  const geo = geographyExample()
  // Summary strip counted from the rows in the table below (src/lib/energyStrip.ts), not the 7 Nov hand-off's 27/28.
  const strip = useMemo(() => energyStripCounts(ENERGY), [live])
  // The markets table names its feed status once in the eyebrow; only a row that differs keeps a feed icon.
  const feedFresh = dominantFresh(ENERGY.map((e) => e.freshness))
  const maxPrice = Math.max(...ENERGY.map((e) => e.priceNow))
  // Months from Jan 2024 to the real current month (the month axis is anchored on the backend's time).
  const nowPct = (Math.min(60, Math.max(0, ym(axisAnchor()) - 2024 * 12)) / 60) * 100
  return (
    <div>
      <PageHeader eyebrow={<span title={`Grid operators, permits and developer filings · as of ${AS_OF}`}>Grid operators · permits · filings</span>} title="Power & infrastructure" sub="Power prices, grid constraints and data-centre build-out."
        about="How electricity prices, grid constraints and data-centre construction connect to compute economics and company exposure." />

      <Foldable label="Coverage" className="mb-5">
        <div className="rounded-md border border-line bg-panel px-4 py-3">
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
            <Fresh f={strip.real ? 'historical' : 'illustrative'} label={strip.real ? `FLOPX series · ${AS_OF}` : 'No FLOPX energy series yet'} />
            <span className="flex items-center gap-2" title={strip.label}>
              <span className="flex gap-[2px]">
                {strip.kinds.map((k, i) => k === 'real'
                  ? <span key={i} className="h-3 w-1.5 rounded-[1px]" style={{ background: 'var(--color-cat1)' }} title={`${ENERGY[i].name}: real series`} />
                  : k === 'sample'
                    ? <span key={i} className="hatch-inf h-3 w-1.5 rounded-[1px] border" style={{ borderColor: 'var(--color-dim)' }} title={`${ENERGY[i].name}: FLOPX sample series`} />
                    : <span key={i} className="h-3 w-1.5 rounded-[1px] border border-dashed" style={{ borderColor: 'var(--color-line2)' }} title={`${ENERGY[i].name}: no FLOPX series (design value)`} />)}
              </span>
              <span className="num text-[11.5px] text-bone">{strip.real}/{strip.total}</span>
            </span>
            <span className="flex min-w-[260px] flex-1 items-center gap-2" title={strip.cheapest && strip.dearest ? `${strip.cheapest.name} ${cur(strip.cheapest)}${strip.cheapest.priceNow.toFixed(0)} to ${strip.dearest.name} ${cur(strip.dearest)}${strip.dearest.priceNow.toFixed(0)} per MWh, over the markets with a FLOPX series` : 'No market with a FLOPX series yet'}>
              <span className="text-[11px] text-mute">{strip.cheapest ? <>{strip.cheapest.name.split(' (')[0]} <span className="num text-bone">{cur(strip.cheapest)}{strip.cheapest.priceNow.toFixed(0)}</span></> : 'no series'}</span>
              <span className="relative h-px flex-1" style={{ background: 'var(--color-line2)' }}>
                <span className="absolute -top-[3px] left-0 h-[7px] w-[7px] rounded-full" style={{ background: 'var(--color-up)' }} />
                <span className="absolute -top-[3px] right-0 h-[7px] w-[7px] rounded-full" style={{ background: 'var(--color-warn)' }} />
              </span>
              <span className="text-[11px] text-mute">{strip.dearest ? <><span className="num text-bone">{cur(strip.dearest)}{strip.dearest.priceNow.toFixed(0)}</span> {strip.dearest.name.split(' (')[0]}</> : 'no series'}</span>
              <span className="num text-[10px] text-dim">/MWh</span>
            </span>
            <Fresh quiet f="simulated" label="ERCOT simulated" className="shrink-0" />
            <span className="cursor-help text-[11px] text-dim underline decoration-dotted" title={ES.licences}>Licences</span>
          </div>
        </div>
      </Foldable>

      <Panel className="mb-5" id="Selected market" title={m.name} eyebrow={m.operator} right={<span className="flex items-center gap-3"><Fresh quiet f={m.freshness} />{histNote(m) && <Fresh quiet f="illustrative" label={histNote(m)!} />}<button onClick={() => inspect({ kind: 'energy', id: m.id, label: m.name })} className="cursor-pointer text-[11px] text-mute hover:text-bone">Inspect →</button></span>}>
        <TimeChart labels={MONTHS.slice(0, NOW_IDX + 1)} height={150} yFmt={(v) => `${cur(m)}${v.toFixed(0)}`} series={[{ id: 'p', label: `${m.name} ${cur(m)}/MWh`, values: m.price, color: 'var(--color-warn)', area: true }]} />
        <div className="mt-3 rounded-md bg-ink p-3 text-[12px]"><span className="eyebrow mr-2">Constraint</span>{m.constraint}</div>
        <div className="mt-4 space-y-3 text-[12px]">
          <div><div className="eyebrow mb-1">→ Compute regions</div>{m.regions.map((r) => <button key={r} onClick={() => { setXray({ region: r, layers: ['pressure', 'power', 'pipeline'] }); navigate('xray') }} className="mr-2 cursor-pointer rounded-[3px] border border-line2 px-2 py-0.5 hover:bg-hover">{regionById(r)!.name} · CPI <span className="text-inf">{regionById(r)!.pressure[NOW_IDX].toFixed(0)}</span></button>)}</div>
          <div><div className="eyebrow mb-1">→ Projects on this grid</div>{linkedProjects.length ? linkedProjects.map((p) => <div key={p.id}><EntityLink r={{ kind: 'project', id: p.id, label: p.name }} /> <span className="text-[11px] text-dim">{p.stage} · <span className="num">{p.reportedMW} MW</span></span></div>) : <span className="text-mute">None tracked</span>}</div>
          <div><div className="eyebrow mb-1">→ Companies (documented)</div>{linkedTickers.length ? linkedTickers.map((t) => <EntityLink key={t} r={{ kind: 'security', id: t, label: t }} className="mr-2 font-mono" />) : <span className="text-mute">None documented</span>}</div>
        </div>
      </Panel>

      <Row cols={[1, 1]} at="xl" className="mb-5">
        <Panel title="Power & pipeline map" eyebrow="Observed layers only" bodyClass="p-0">
          <div className="h-[360px]"><WorldMap callouts={false} layers={['power', 'pipeline']} facilities="summary" reveal={0} onRegion={(id) => inspect({ kind: 'region', id, label: regionById(id)!.name })} onProject={(id) => inspect({ kind: 'project', id, label: PROJECTS.find((p) => p.id === id)!.name })} minHeight={360} /></div>
          <MapKey />
        </Panel>
        <Panel title="Three geographies, kept separate" eyebrow="Facility location · grid zone · marketplace region">
          <div className="grid grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)] gap-2 border-b border-line pb-1.5 text-[11px] text-dim font-medium"><span>Facility</span><span>Location</span><span>Grid zone</span><span>Sold as</span></div>
          {PROJECTS.slice(0, 8).map((p) => (
            <div key={p.id} className="grid grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)] gap-2 border-b border-line-soft py-1.5 text-[11.5px]">
              <EntityLink r={{ kind: 'project', id: p.id, label: p.name }} className="truncate" />
              <span className="num text-[10.5px] text-mute">{p.lat.toFixed(1)}, {p.lon.toFixed(1)}</span>
              <span className="font-mono text-[10.5px]">{p.gridZone}</span>
              <span className="font-mono text-[10.5px] text-mute">{p.marketplaceRegion}</span>
            </div>
          ))}
          <div className="mt-3 text-[10.5px] leading-relaxed text-dim">{geo ? `E.g. ${geo.a.name}: ${geo.a.gridZone} grid zone, sold in ${geo.a.marketplaceRegion} with ${geo.b.name} in ${geo.b.gridZone}. ` : ''}Power price follows grid zone; rental competition follows marketplace region.</div>
        </Panel>
      </Row>

      <Panel className="mb-5" id="Markets and build status" bodyClass="p-0"
        title={<Seg value={view} onChange={setView} options={[{ id: 'markets', label: 'Energy markets' }, { id: 'build', label: 'Data-centre build status' }]} />}
        eyebrow={view === 'markets' ? <span title="Day-ahead, 30-day average, USD unless marked €" className="inline-flex items-center gap-1.5"><TruthGlyph truth="obs" size={7} />Day-ahead · USD{feedFresh && <Fresh quiet f={feedFresh} label={ENERGY.some((e) => e.freshness !== feedFresh) ? `Feed ${feedFresh} unless a row says otherwise` : `Feed ${feedFresh}`} />}</span> : <span title="Reported MW is observed; usable MW by Mar 27 is a FLOPX estimate. Developers marked (illustr.) are illustrative."><TruthTag truth="obs" compact /> reported · <TruthTag truth="inf" compact /> usable</span>}
        right={view === 'build' ? <Seg size="sm" value={stage} onChange={setStage} options={[{ id: 'all', label: 'All' }, { id: 'operational', label: 'Operational' }, { id: 'construction', label: 'Construction' }, { id: 'proposed', label: 'Proposed' }, { id: 'announced', label: 'Announced' }]} /> : undefined}>
        <div className="grid">
        <div className={cx('[grid-area:1/1]', view !== 'markets' && 'invisible')} aria-hidden={view !== 'markets'}>
          <div className="h-full overflow-x-auto"><table className="h-full w-full text-[12px]">
            <thead>
              <tr className="h-8 border-b border-line text-[10.5px] tracking-wide text-dim uppercase">
                {[['Market', 'Feed icon: delayed or historical'], ['24 mo', 'Day-ahead price, last 24 months'], ['$/MWh', `Bar scaled to the dearest market; red above $${POWER_HOT}`], ['30d', '30-day change'], ['Scarcity', 'Scarcity hours, last 30 days'], ['Reserve', 'Reserve margin'], ['Queue', 'Large-load interconnection queue']].map(([h, t], i) => (
                  <th key={h} title={t} className={cx('px-3 font-normal', i === 3 || i >= 5 ? 'text-right' : 'text-left', i >= 5 && 'hidden lg:table-cell')}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {ENERGY.map((e) => {
                const hot = e.currency !== 'EUR' && e.priceNow > POWER_HOT
                const heat = Math.min(1, (e.scarcityHours ?? 0) / 40)
                return (
                  <tr key={e.id} onClick={() => setMarket(e.id)} className={cx('h-9 cursor-pointer border-b border-line-soft last:border-0', market === e.id && view === 'markets' ? 'bg-act/[0.06] shadow-[inset_2px_0_0_var(--color-act)]' : 'hover:bg-hover')}>
                    <td className="px-3 py-1.5"><span className="flex items-center gap-2 whitespace-nowrap"><span className="font-medium">{e.name}</span>{e.freshness !== feedFresh && <span title={`Feed: ${e.freshness}`}><Fresh f={e.freshness} label="" className="border-0 px-0" /></span>}</span></td>
                    <td className="px-3 py-1.5"><Spark values={e.price} width={88} height={18} color={hot ? 'var(--color-down)' : 'var(--color-warn)'} /></td>
                    <td className="px-3 py-1.5">
                      <span className="flex items-center gap-2.5">
                        <span className={cx('num w-11 shrink-0 text-right', hot ? 'text-down' : 'text-bone')}>{e.currency === 'EUR' ? '€' : ''}{e.priceNow.toFixed(1)}</span>
                        <span className="h-1.5 w-full max-w-[160px] min-w-[60px] rounded-[1px] bg-line"><span className="block h-full rounded-[1px]" style={{ width: `${(e.priceNow / maxPrice) * 100}%`, background: hot ? 'var(--color-down)' : 'var(--color-warn)', opacity: 0.85 }} /></span>
                      </span>
                    </td>
                    <td className="px-3 py-1.5 text-right">{Number.isFinite(e.chg30) ? <Delta v={e.chg30} invert /> : <span className="num text-dim">—</span>}</td>
                    <td className="px-3 py-1.5">
                      <span className={cx('num inline-grid h-5 min-w-8 place-items-center rounded-[3px] px-1.5 text-[11px]', heat > 0.4 ? 'text-bone' : heat > 0 ? 'text-mute' : 'text-dim')}
                        style={{ background: heat > 0 ? `color-mix(in srgb, var(--color-down) ${Math.round(8 + heat * 50)}%, transparent)` : undefined }} title={e.scarcityHours != null ? `${e.scarcityHours} scarcity hours` : 'Scarcity hours not loaded'}>{e.scarcityHours ?? '—'}</span>
                    </td>
                    <td className="num hidden px-3 py-1.5 text-right text-mute lg:table-cell">{e.reserveMargin != null ? `${e.reserveMargin}%` : <span className="text-dim">—</span>}</td>
                    <td className="num hidden px-3 py-1.5 text-right whitespace-nowrap text-mute lg:table-cell">{e.queueGW != null ? <>{e.queueGW}<span className="text-dim"> GW</span></> : <span className="text-dim">—</span>}</td>
                  </tr>
                )
              })}
            </tbody>
          </table></div>
        </div>
        <div className={cx('[grid-area:1/1] flex flex-col', view !== 'build' && 'invisible')} aria-hidden={view !== 'build'}>
        <div className={cx(buildCols, 'border-b border-line px-4 py-2 text-[11px] text-dim')}>
          <span>Project</span><span>Stage</span><span title="Energized share of reported MW">Energized / reported MW</span><span className="flex items-center justify-end gap-1.5" title="Usable MW by Mar 2027 (FLOPX estimate)"><TruthGlyph truth="inf" size={7} />Usable MW</span>
          <span className="relative h-4">
            {yrs.map((y, i) => <span key={y} className="absolute" style={{ left: `${i * 20}%` }}>{y}</span>)}
          </span>
        </div>
        {projects.map((p) => {
          const end = parseWhen(p.expectedOnline)
          const start = parseWhen(p.milestones[0]?.date ?? '') ?? 0
          const share = p.reportedMW ? p.energizedMW / p.reportedMW : 0
          const slipped = p.milestones.some((x) => x.status === 'slipped')
          const [lo, hi] = p.effectiveRange
          return (
            <div key={p.id} onClick={() => inspect({ kind: 'project', id: p.id, label: p.name })} className={cx(buildCols, 'cursor-pointer items-center border-b border-line-soft px-4 py-2 hover:bg-hover')}>
              <span className="min-w-0"><span className="block truncate text-[12px] text-bone">{p.name}</span><span className="block truncate text-[10.5px] text-dim">{regionById(p.region)!.short} · <span title={p.developer}>{p.developer.replace(' (illustr.)', '')}</span>{p.developerTicker && secByTicker(p.developerTicker) ? <span className="num text-mute"> · {p.developerTicker}</span> : ''}</span></span>
              <span className={cx('text-[11.5px] capitalize', p.stage === 'operational' ? 'text-bone' : p.stage === 'construction' ? 'text-mute' : 'text-dim')}>{p.stage}</span>
              <span className="flex items-center gap-2" title={`${p.energizedMW} of ${p.reportedMW} MW energized`}>
                <span className="h-1.5 flex-1 rounded-[1px] bg-line"><span className="block h-full rounded-[1px] bg-cat3" style={{ width: `${share * 100}%` }} /></span>
                <span className="num w-12 shrink-0 text-right text-[11px] text-mute">{p.reportedMW}</span>
              </span>
              <span className="num text-right text-[11.5px] text-mute" title={`Usable by Mar 2027: ${lo}–${hi} MW (estimate)`}>{hi === 0 ? <span className="text-dim">—</span> : lo === hi ? lo : `${lo}–${hi}`}</span>
              <span className="relative h-5">
                {yrs.slice(1).map((y, i) => <span key={y} className="absolute inset-y-0 w-px bg-line-soft" style={{ left: `${(i + 1) * 20}%` }} />)}
                <span className="absolute inset-y-0 w-px bg-mute/60" style={{ left: `${nowPct}%` }} />
                {end != null ? (
                  <span className={cx('absolute top-1/2 h-2 -translate-y-1/2 rounded-[1px]', STAGE_BAR[p.stage])} title={`${p.milestones[0]?.date ?? 'Start n/a'} → ${p.expectedOnline}`}
                    style={{ left: `${(Math.max(0, start) / 60) * 100}%`, width: `${Math.max(2, ((Math.min(60, end) - Math.max(0, start)) / 60) * 100)}%` }} />
                ) : <span className="absolute top-1/2 left-0 -translate-y-1/2 text-[10px] text-dim" title="Timing not disclosed">TBD</span>}
                {slipped && end != null && <span className="absolute top-1/2 size-[5px] -translate-y-1/2 rotate-45 bg-down" title="A milestone slipped" style={{ left: `calc(${(Math.min(60, end) / 60) * 100}% + 5px)` }} />}
              </span>
            </div>
          )
        })}
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-2 text-[10.5px] text-dim">
          {STAGE_ORDER.map((st) => <span key={st} className="flex items-center gap-1.5 capitalize"><span className={cx('h-2 w-4 rounded-[1px]', STAGE_BAR[st])} />{st}</span>)}
          <span className="flex items-center gap-1.5"><span className="size-[5px] rotate-45 bg-down" />slipped</span>
          <span className="flex items-center gap-1.5"><span className="h-2.5 w-px bg-mute/60" />now</span>
        </div>
        </div>
        </div>
      </Panel>
    </div>
  )
}

/** Key for the power & pipeline map: what bar height/colour and each facility glyph mean. */
/** "History before Nov 26 illustrative" when an observed market's month axis still holds design values. */
function histNote(m: { freshness: string; real?: boolean[]; scaled?: boolean }): string | null {
  if (m.freshness === 'illustrative' || !m.real) return null
  const first = m.real.findIndex(Boolean)
  if (first < 0) return 'History illustrative'
  // G14: months without a backend value are the design's curve scaled to join the backend months.
  if (m.scaled) return first > 0 ? `${SCALED_HISTORY_LABEL} before ${MONTHS[first]}` : SCALED_HISTORY_LABEL
  return first > 0 ? `History before ${MONTHS[first]} illustrative` : null
}

/** Two tracked facilities sold in one marketplace region from different grid zones, for the "three geographies" note. */
function geographyExample() {
  for (const a of PROJECTS) {
    const b = PROJECTS.find((x) => x.id !== a.id && x.marketplaceRegion === a.marketplaceRegion && x.gridZone !== a.gridZone && x.gridZone !== 'n/a')
    if (b && a.gridZone !== 'n/a') return { a, b }
  }
  return null
}

function MapKey() {
  const pip = (stage: Stage) => <svg width="8" height="8" viewBox="0 0 8 8" aria-hidden="true" className="shrink-0"><StagePip stage={stage} /></svg>
  const item = 'flex items-center gap-1.5 text-[11px] text-mute'
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 border-t border-line px-4 py-2.5">
      <span className={item}>
        <svg width={METER_W} height={METER_H} aria-hidden="true"><PowerMeter usd={58} /></svg>
        day-ahead $/MWh by region (full at $150), <span className="text-down">red above ${POWER_HOT}</span>
      </span>
      <span className={item}>Facilities:</span>
      {STAGE_ORDER.map((st) => (
        <span key={st} className={item}>{pip(st)}{st === 'construction' ? 'building' : st === 'operational' ? 'operating' : st}</span>
      ))}
    </div>
  )
}
