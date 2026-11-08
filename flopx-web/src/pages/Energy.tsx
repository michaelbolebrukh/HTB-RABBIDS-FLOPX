import { useMemo, useState } from 'react'
import { Zap } from 'lucide-react'
import { energyStripCounts } from '@/lib/energyStrip'
import { AS_OF, MONTHS, NOW_IDX, regionById } from '@/data/core'
import { ENERGY, PROJECTS, type Stage } from '@/data/infra'
import { secByTicker } from '@/data/finance'
import { WorldMap } from '@/components/WorldMap'
import { SCALED_HISTORY_LABEL, TimeChart } from '@/components/charts'
import { cx, Delta, dominantFresh, EntityLink, Fresh, PageHeader, Panel, Seg, Spark, TruthGlyph, TruthTag } from '@/components/ui'
import { useStore } from '@/lib/store'
import { axisAnchor, ym } from '@/live/dates'
import { ENERGY_SUMMARY as ES } from '@/data/handoff'

const STAGE_ORDER: Stage[] = ['operational', 'construction', 'proposed', 'announced']
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

export default function Energy() {
  const { route, inspect, navigate, setXray, live } = useStore()
  const [market, setMarket] = useState(route.params.market ?? 'ercot')
  const [stage, setStage] = useState<'all' | Stage>('all')
  const m = ENERGY.find((e) => e.id === market)!
  const projects = PROJECTS.filter((p) => stage === 'all' || p.stage === stage).sort((a, b) => STAGE_ORDER.indexOf(a.stage) - STAGE_ORDER.indexOf(b.stage))
  const linkedProjects = PROJECTS.filter((p) => m.regions.includes(p.region))
  const linkedTickers = Array.from(new Set(m.regions.flatMap((r) => regionById(r)!.documented.map((d) => d.t))))
  const cur = (e: { currency: string }) => (e.currency === 'EUR' ? '€' : '$')
  const curOf = cur
  const geo = geographyExample()
  const strip = useMemo(() => energyStripCounts(ENERGY), [live])
  // The markets table names its feed status once in the eyebrow; only a row that differs keeps a chip.
  const feedFresh = dominantFresh(ENERGY.map((e) => e.freshness))
  return (
    <div>
      <PageHeader eyebrow={<><Zap size={12} /> Energy & infrastructure · {AS_OF}</>} title="Power, grids and the build-out" sub="Electricity prices, grid constraints and data-centre construction, and how each connects to compute economics and company exposure. A facility’s location, its grid zone and the marketplace region its capacity is sold in are three different things." />

      {/* Summary strip counted from the rows in the table below (src/lib/energyStrip.ts), not the 7 Nov hand-off's 27/28. */}
      <div className="mb-5 rounded-md border border-line bg-panel px-4 py-3">
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
            <span className="text-[10.5px] text-dim">{strip.label}</span>
          </span>
          <span className="flex min-w-[260px] flex-1 items-center gap-2" title={strip.cheapest && strip.dearest ? `${strip.cheapest.name} ${curOf(strip.cheapest)}${strip.cheapest.priceNow.toFixed(0)} to ${strip.dearest.name} ${curOf(strip.dearest)}${strip.dearest.priceNow.toFixed(0)} per MWh, over the markets with a FLOPX series` : 'No market with a FLOPX series yet'}>
            <span className="text-[11px] text-mute">{strip.cheapest ? <>{strip.cheapest.name.split(' (')[0]} <span className="num text-bone">{curOf(strip.cheapest)}{strip.cheapest.priceNow.toFixed(0)}</span></> : 'no series'}</span>
            <span className="relative h-px flex-1" style={{ background: 'var(--color-line2)' }}>
              <span className="absolute -top-[3px] left-0 h-[7px] w-[7px] rounded-full" style={{ background: 'var(--color-up)' }} />
              <span className="absolute -top-[3px] right-0 h-[7px] w-[7px] rounded-full" style={{ background: 'var(--color-warn)' }} />
            </span>
            <span className="text-[11px] text-mute">{strip.dearest ? <><span className="num text-bone">{curOf(strip.dearest)}{strip.dearest.priceNow.toFixed(0)}</span> {strip.dearest.name.split(' (')[0]}</> : 'no series'}</span>
            <span className="num text-[10px] text-dim">/MWh{strip.cheapest && strip.dearest && strip.cheapest.id === strip.dearest.id ? ' · one zone with a series' : ' · markets with a FLOPX series'}</span>
          </span>
          <Fresh quiet f="simulated" label="ERCOT simulated" className="shrink-0" />
          <span className="cursor-help font-mono text-[10.5px] text-dim underline decoration-dotted" title={ES.licences}>Licences</span>
        </div>
      </div>

      <Panel className="mb-5" title={m.name} eyebrow={m.operator} right={<span className="flex items-center gap-3"><Fresh quiet f={m.freshness} />{histNote(m) && <Fresh quiet f="illustrative" label={histNote(m)!} />}<button onClick={() => inspect({ kind: 'energy', id: m.id, label: m.name })} className="cursor-pointer font-mono text-[10.5px] text-mute hover:text-bone">Inspect →</button></span>}>
        <TimeChart labels={MONTHS.slice(0, NOW_IDX + 1)} height={150} yFmt={(v) => `${cur(m)}${v.toFixed(0)}`} series={[{ id: 'p', label: `${m.name} ${cur(m)}/MWh`, values: m.price, color: 'var(--color-warn)', area: true }]} />
        <div className="mt-3 rounded-md border border-line bg-ink p-3 text-[12px]"><span className="eyebrow mr-2">Constraint</span>{m.constraint}</div>
        <div className="mt-4 space-y-3 text-[12px]">
          <div><div className="eyebrow mb-1">→ Compute regions</div>{m.regions.map((r) => <button key={r} onClick={() => { setXray({ region: r, layers: ['pressure', 'power', 'pipeline'] }); navigate('xray') }} className="mr-2 cursor-pointer rounded-[3px] border border-line2 px-2 py-0.5 hover:bg-hover">{regionById(r)!.name} · CPI <span className="text-inf">{regionById(r)!.pressure[NOW_IDX].toFixed(0)}</span></button>)}</div>
          <div><div className="eyebrow mb-1">→ Projects on this grid</div>{linkedProjects.length ? linkedProjects.map((p) => <div key={p.id}><EntityLink r={{ kind: 'project', id: p.id, label: p.name }} /> <span className="font-mono text-[10.5px] text-dim">{p.stage} · {p.reportedMW} MW</span></div>) : <span className="text-mute">None tracked</span>}</div>
          <div><div className="eyebrow mb-1">→ Companies (documented)</div>{linkedTickers.length ? linkedTickers.map((t) => <EntityLink key={t} r={{ kind: 'security', id: t, label: t }} className="mr-2 font-mono" />) : <span className="text-mute">None documented</span>}</div>
        </div>
      </Panel>

      <div className="mb-5 grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <Panel title="Power & pipeline map" eyebrow="Observed layers only" bodyClass="p-0">
          <div className="h-[360px]"><WorldMap callouts={false} layers={['power', 'pipeline']} reveal={0} onRegion={(id) => inspect({ kind: 'region', id, label: regionById(id)!.name })} onProject={(id) => inspect({ kind: 'project', id, label: PROJECTS.find((p) => p.id === id)!.name })} minHeight={360} /></div>
          <MapKey />
        </Panel>
        <Panel title="Three geographies, kept separate" eyebrow="Facility location · grid zone · marketplace region">
          <div className="grid grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)] gap-2 border-b border-line pb-1.5 text-[11px] text-dim font-medium"><span>Facility</span><span>Location</span><span>Grid zone</span><span>Sold as</span></div>
          {PROJECTS.slice(0, 8).map((p) => (
            <div key={p.id} className="grid grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)] gap-2 border-b border-line/60 py-1.5 text-[11.5px]">
              <EntityLink r={{ kind: 'project', id: p.id, label: p.name }} className="truncate" />
              <span className="num text-[10.5px] text-mute">{p.lat.toFixed(1)}, {p.lon.toFixed(1)}</span>
              <span className="font-mono text-[10.5px]">{p.gridZone}</span>
              <span className="font-mono text-[10.5px] text-mute">{p.marketplaceRegion}</span>
            </div>
          ))}
          <div className="mt-3 text-[10.5px] leading-relaxed text-dim">{geo ? `Example: ${geo.a.name} sits in the ${geo.a.gridZone} grid zone, but its capacity is sold in the ${geo.a.marketplaceRegion} marketplace region alongside ${geo.b.name} in ${geo.b.gridZone}. ` : ''}Power-price exposure follows the grid zone; rental competition follows the marketplace region.</div>
        </Panel>
      </div>

      <div className="mb-5">
        <Panel title="Energy markets" eyebrow={<><TruthGlyph truth="obs" size={7} />Day-ahead · USD unless marked €{feedFresh && <Fresh quiet f={feedFresh} label={ENERGY.some((e) => e.freshness !== feedFresh) ? `Feed ${feedFresh} unless a row says otherwise` : `Feed ${feedFresh}`} />}</>} bodyClass="p-0">
          <table className="w-full text-[12px]">
            <thead>
              <tr className="border-b border-line text-[11px] text-dim font-medium">
                {['Market', '24M', 'Per MWh', '30d', 'Scarcity h', 'Reserve', 'LL queue', 'Feed'].map((h, i) => <th key={h} className={cx('px-3 py-2 font-normal', i >= 2 && i <= 6 ? 'text-right' : 'text-left', (i === 5 || i === 6) && 'hidden lg:table-cell')}>{h}</th>)}
              </tr>
            </thead>
            <tbody>
              {ENERGY.map((e) => (
                <tr key={e.id} onClick={() => setMarket(e.id)} className={cx('cursor-pointer border-b border-line/60', market === e.id ? 'bg-raise' : 'hover:bg-hover')}>
                  <td className="px-3 py-2"><span className="font-medium">{e.name}</span></td>
                  <td className="px-3 py-2"><Spark values={e.price} width={64} height={16} color="var(--color-warn)" /></td>
                  <td className="num px-3 py-2 text-right">{e.currency === 'EUR' ? '€' : ''}{e.priceNow.toFixed(1)}</td>
                  <td className="px-3 py-2 text-right">{Number.isFinite(e.chg30) ? <Delta v={e.chg30} invert /> : <span className="num text-dim">—</span>}</td>
                  <td className={cx('num px-3 py-2 text-right', e.scarcityHours != null && e.scarcityHours > 20 && 'text-down', e.scarcityHours == null && 'text-dim')}>{e.scarcityHours ?? '—'}</td>
                  <td className="num hidden px-3 py-2 text-right text-mute lg:table-cell">{e.reserveMargin != null ? `${e.reserveMargin}%` : '—'}</td>
                  <td className="num hidden px-3 py-2 text-right whitespace-nowrap text-mute lg:table-cell">{e.queueGW != null ? `${e.queueGW} GW` : '—'}</td>
                  <td className="px-3 py-2">{e.freshness !== feedFresh ? <Fresh f={e.freshness} /> : <span className="font-mono text-[10px] text-dim">{e.freshness}</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      </div>

      <Panel title="Data-centre build status" eyebrow={<><TruthTag truth="obs" compact /> reported · <TruthTag truth="inf" compact /> usable estimate</>} right={<Seg size="sm" value={stage} onChange={setStage} options={[{ id: 'all', label: 'All' }, { id: 'operational', label: 'Operational' }, { id: 'construction', label: 'Construction' }, { id: 'proposed', label: 'Proposed' }, { id: 'announced', label: 'Announced' }]} />} bodyClass="p-0">
        <div className="grid grid-cols-[minmax(0,1.6fr)_100px_90px_120px_minmax(0,1.8fr)] gap-3 border-b border-line px-4 py-2 text-[11px] text-dim font-medium">
          <span>Project</span><span>Stage</span><span className="text-right">MW rep/enrg</span><span className="text-right" title={`Usable MW estimate 12 months out (${MONTHS[NOW_IDX + 12]})`}>Usable {MONTHS[NOW_IDX + 12]}</span>
          <span className="grid grid-cols-5">{yrs.map((y) => <span key={y}>{y}</span>)}</span>
        </div>
        {projects.map((p) => {
          const end = parseWhen(p.expectedOnline)
          const start = parseWhen(p.milestones[0]?.date ?? '') ?? 0
          // Months from Jan 2024 to the real current month (the month axis is anchored on the backend's time).
          const nowPct = (Math.min(60, Math.max(0, ym(axisAnchor()) - 2024 * 12)) / 60) * 100
          return (
            <div key={p.id} onClick={() => inspect({ kind: 'project', id: p.id, label: p.name })} className="grid cursor-pointer grid-cols-[minmax(0,1.6fr)_100px_90px_120px_minmax(0,1.8fr)] items-center gap-3 border-b border-line/60 px-4 py-2 hover:bg-hover">
              <span className="min-w-0"><span className="block truncate text-[12px]">{p.name}</span><span className="block truncate font-mono text-[10px] text-dim">{regionById(p.region)!.short} · {p.developer}{p.developerTicker && secByTicker(p.developerTicker) ? ` · ${p.developerTicker}` : ''}</span></span>
              <span className={cx('text-[11.5px] font-medium capitalize', p.stage === 'operational' ? 'text-bone' : p.stage === 'construction' ? 'text-mute' : 'text-dim')}>{p.stage}</span>
              <span className="num text-right text-[11.5px]">{p.reportedMW}/{p.energizedMW}</span>
              <span className="num text-right text-[11.5px] text-inf">{p.effectiveRange[0]}–{p.effectiveRange[1]}</span>
              <span className="relative h-4">
                <span className="absolute inset-y-0 w-px bg-bone/60" style={{ left: `${nowPct}%` }} />
                {end != null ? (
                  <span className={cx('absolute top-1 h-2 rounded-sm', p.stage === 'operational' ? 'bg-bone/80' : p.stage === 'construction' ? 'border border-bone/70 bg-bone/20' : 'border border-dashed border-mute')} style={{ left: `${(Math.max(0, start) / 60) * 100}%`, width: `${Math.max(2, ((Math.min(60, end) - Math.max(0, start)) / 60) * 100)}%` }} />
                ) : <span className="absolute top-0 left-0 font-mono text-[9.5px] text-warn">timing unknown</span>}
                {p.milestones.some((x) => x.status === 'slipped') && end != null && <span className="absolute top-0 font-mono text-[9px] text-down" style={{ left: `${(Math.min(60, end) / 60) * 100 + 1}%` }}>slipped</span>}
              </span>
            </div>
          )
        })}
      </Panel>
    </div>
  )
}

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

/** Key for the power & pipeline map: what bar height/colour and each facility glyph mean. */
function MapKey() {
  const glyph = (stage: Stage) => (
    <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true" className="shrink-0">
      {stage === 'operational' && <rect x="1" y="1" width="8" height="8" fill="var(--color-bone)" fillOpacity={0.85} />}
      {stage === 'construction' && <><rect x="1" y="1" width="8" height="8" fill="none" stroke="var(--color-bone)" strokeWidth="1.2" /><rect x="1" y="5" width="8" height="4" fill="var(--color-bone)" fillOpacity={0.55} /></>}
      {stage === 'proposed' && <rect x="1" y="1" width="8" height="8" fill="none" stroke="var(--color-bone)" />}
      {stage === 'announced' && <rect x="1" y="1" width="8" height="8" fill="none" stroke="var(--color-bone)" strokeDasharray="1.5 2" />}
    </svg>
  )
  const item = 'flex items-center gap-1.5 text-[11px] text-mute'
  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-2 border-t border-line px-4 py-2.5">
      <span className={item}>
        <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><rect x="2" y="6" width="3" height="8" fill="var(--color-warn)" fillOpacity={0.85} /><rect x="8" y="1" width="3" height="13" fill="var(--color-down)" fillOpacity={0.85} /></svg>
        Day-ahead power $/MWh · taller = pricier · <span className="text-down">red &gt; $90</span>
      </span>
      {(['operational', 'construction', 'proposed', 'announced'] as Stage[]).map((st) => (
        <span key={st} className={item}>{glyph(st)}{st === 'construction' ? 'Under construction' : st[0].toUpperCase() + st.slice(1)}</span>
      ))}
      <span className={item}><span className="inline-block size-[6px] rounded-full bg-bone" />Covered region</span>
    </div>
  )
}
