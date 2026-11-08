import { useEffect, useRef, useState, type ReactNode } from 'react'
import { ArrowRight, Bot, ChevronDown, ChevronUp, GitCompareArrows, Pause, Play, RotateCcw, ScanLine, X } from 'lucide-react'
import { ANOMALY_CARDS, AS_OF, fmtPct, GPUS, horizonLabel, MONTHS, NOW_IDX, PRESSURE_MODEL, REGIONS, regionById, regionEvidence, type Region } from '@/data/core'
import { PROJECTS, projectById } from '@/data/infra'
import { lastPx } from '@/data/fund'
import { WorldMap } from '@/components/WorldMap'
import { Drawer, EdgeToggle } from '@/components/collapse'
import { SCALED_SHORT_LABEL, TimeChart } from '@/components/charts'
import { Stat, Timeline } from '@/components/Inspector'
import { AIBlock, Btn, cx, Delta, dominantFresh, EntityLink, Fresh, Meter, Seg, Source, Spark, TruthGlyph, TruthTag } from '@/components/ui'
import { PRESSURE_BAND_WORDS, pressureAt, pressureColor, pressureWord, scenarioImpact, uncAt } from '@/lib/model'
import { useStore, type XLayer } from '@/lib/store'
import { derived, SNAPSHOT, XRAY_CAPACITY as XC } from '@/data/handoff'

const LAYERS: { id: XLayer; label: string; truth: 'obs' | 'inf' | 'dec'; desc: string }[] = [
  { id: 'pressure', label: 'Compute pressure', truth: 'inf', desc: 'Inferred scarcity with uncertainty rings' },
  { id: 'rental', label: 'Rental economics', truth: 'obs', desc: 'Median H100 quote · premium vs global' },
  { id: 'power', label: 'Power constraints', truth: 'obs', desc: 'Day-ahead $/MWh · grid stress' },
  { id: 'pipeline', label: 'Infrastructure pipeline', truth: 'obs', desc: 'Facilities by reported stage' },
  { id: 'effective', label: 'Effective capacity', truth: 'inf', desc: 'Announced (dotted) vs usable (hatched)' },
  { id: 'exposure', label: 'Financial exposure', truth: 'dec', desc: 'Documented company ties' },
]

/** Every panel around the map collapses with a chevron on its edge and comes back the same way; panels stay mounted, so their state survives. */
type Pane = 'header' | 'brief' | 'rail' | 'time' | 'side'

export default function XRay() {
  const s = useStore()
  const { xray, setXray } = s
  const [collapsed, setCollapsed] = useState<Pane[]>([])
  const open = (p: Pane) => !collapsed.includes(p)
  const toggle = (p: Pane) => setCollapsed((c) => (c.includes(p) ? c.filter((x) => x !== p) : [...c, p]))
  const region = xray.region ? regionById(xray.region)! : null

  return (
    <div className="flex h-full flex-col">
      {/* header */}
      <div className={cx('relative z-30 flex shrink-0 items-center justify-between gap-4 border-b border-line px-5 transition-[padding] duration-300 ease-out motion-reduce:transition-none', open('header') ? 'py-3' : 'py-1.5')}>
        <EdgeToggle edge="bottom" open={open('header')} label="header" onClick={() => toggle('header')} />
        <div className="flex min-w-0 items-center gap-3">
          <ScanLine size={open('header') ? 18 : 14} className="shrink-0 text-inf" />
          <div className="min-w-0">
            <h1 className={cx('truncate font-serif leading-none transition-[font-size] duration-300 ease-out motion-reduce:transition-none', open('header') ? 'text-[24px]' : 'text-[15px]')}>Compute Economy X-Ray</h1>
            <Drawer open={open('header')}><div className="pt-1 font-mono text-[10px] text-dim">Inferred hidden state of global compute · model {PRESSURE_MODEL.method ?? 'v0.7 (illustrative)'} · as of {AS_OF}</div></Drawer>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Btn size="sm" variant={open('brief') ? 'subtle' : 'ghost'} aria-expanded={open('brief')} onClick={() => toggle('brief')}>{open('brief') ? <ChevronUp size={12} /> : <ChevronDown size={12} />} 60-second brief</Btn>
          <Btn size="sm" variant={xray.compareOpen ? 'primary' : 'outline'} onClick={() => setXray((x) => ({ compareOpen: !x.compareOpen, compare: x.compare.length ? x.compare : [x.region ?? 'us-tx', 'no-nordics'].filter((v, i, a) => a.indexOf(v) === i).slice(0, 2) }))}><GitCompareArrows size={13} /> Compare</Btn>
          <Btn size="sm" variant="ghost" onClick={() => { setCollapsed([]); setXray({ layers: ['pressure', 'pipeline'], reveal: 100, t: NOW_IDX, region: null, project: null, scenario: null, compareOpen: false, tab: 'brief' }) }}><RotateCcw size={12} /> Reset</Btn>
        </div>
      </div>
      <Drawer open={open('brief')}><JudgeBrief /></Drawer>

      <div className="flex min-h-0 flex-1">
        <SideDrawer side="left" width={232} label="Controls" open={open('rail')} onToggle={() => toggle('rail')}><LeftRail /></SideDrawer>
        <div className="relative flex min-w-0 flex-1 flex-col">
          <div className="relative min-h-0 flex-1 bg-[radial-gradient(ellipse_at_center,var(--color-ocean)_0%,var(--color-ocean-edge)_75%)]">
            <WorldMap
              layers={xray.layers}
              reveal={xray.reveal}
              t={xray.t}
              selected={xray.region}
              selectedProject={xray.project}
              compare={xray.compareOpen ? xray.compare : []}
              scenario={xray.scenario}
              onRegion={(id) => {
                if (xray.compareOpen) setXray((x) => ({ compare: x.compare.includes(id) ? x.compare : [x.compare[1] ?? x.compare[0], id].filter(Boolean).slice(-2) as string[] }))
                else setXray((x) => ({ region: id, project: null, tab: x.region === id ? x.tab : 'brief' }))
              }}
              onProject={(id) => setXray({ region: projectById(id)!.region, project: id, tab: 'pipeline' })}
            />
            <MapOverlays status />
          </div>
          {xray.compareOpen && <ComparePanel />}
          <div className="relative z-20 shrink-0">
            <EdgeToggle edge="top" open={open('time')} label="timeline" onClick={() => toggle('time')} />
            <Drawer open={open('time')}><TimeScrubber /></Drawer>
          </div>
        </div>
        <SideDrawer side="right" width={420} label="Regions" open={open('side')} onToggle={() => toggle('side')}><RightPanel region={region} /></SideDrawer>
      </div>
    </div>
  )
}

/** Slides a side column shut to zero width and back. Content stays mounted at full width and is clipped; the edge chevron stays visible. */
function SideDrawer({ side, width, label, open, onToggle, children }: { side: 'left' | 'right'; width: number; label: string; open: boolean; onToggle: () => void; children: ReactNode }) {
  return (
    <div className="relative z-20 shrink-0">
      <div className={cx('h-full overflow-hidden transition-[width] duration-300 ease-out motion-reduce:transition-none', side === 'left' ? 'border-r border-line' : 'border-l border-line', !open && 'border-transparent')} style={{ width: open ? width : 0 }}>
        <div inert={!open} className={cx('flex h-full transition-opacity duration-200', side === 'right' && 'float-right', open ? 'opacity-100' : 'opacity-0')} style={{ width }}>{children}</div>
      </div>
      <EdgeToggle edge={side === 'left' ? 'right' : 'left'} open={open} label={label.toLowerCase()} onClick={onToggle} />
    </div>
  )
}

/** Header-strip ranks: top three live regions by headline cpx score (with ± the region's uncertainty), else the hand-off's design rows. */
export function rankRows(live: Region[], design: readonly { region: string; cpi: number; lo?: number; hi?: number }[]): { live: boolean; rows: { region: string; cpi: number; lo?: number; hi?: number; band?: string }[] } {
  if (live.length) {
    return { live: true, rows: live.slice(0, 3).map((r) => { const s = r.live!.pressure!.score; return { region: r.short, cpi: Math.round(s), lo: r.unc ? Math.max(0, Math.round(s - r.unc)) : undefined, hi: r.unc ? Math.min(100, Math.round(s + r.unc)) : undefined, band: r.live!.pressure!.band } }) }
  }
  return { live: false, rows: design.map((d) => ({ ...d })) }
}

function JudgeBrief() {
  const live = REGIONS.filter((r) => r.live?.pressure).sort((a, b) => b.live!.pressure!.score - a.live!.pressure!.score)
  const top = live[0]
  const exploratory = live.filter((r) => r.live!.pressure!.label === 'exploratory').length
  const supplyLive = REGIONS.filter((r) => r.live?.supply)
  const ranks = rankRows(live, XC.ranks)
  const cols: { k: string; truth: 'obs' | 'inf' | 'fct'; title: string; body: ReactNode }[] = [
    { k: 'Invisible', truth: 'obs', title: 'Where compute is actually scarce', body: 'Nobody publishes GPU utilisation or usable capacity. Prices, power, permits and press releases each show only a fragment.' },
    PRESSURE_MODEL.method
      ? { k: 'How FLOPX inferred it', truth: 'inf', title: '6 factors → one pressure score', body: `${PRESSURE_MODEL.method} weighs comparable-ask premium, advertised availability, index momentum, power-price stress, facility pipeline relief and demand signals on a 0 to 100 scale. It infers pressure; it does not measure utilisation.` }
      : { k: 'How FLOPX inferred it', truth: 'inf', title: '5 observation types → one latent state', body: <>Rental premia, sold-out rates, power scarcity, queue slippage and energisation lags are combined in a state-space model with explicit uncertainty. <Fresh quiet f="illustrative" /></> },
    top
      ? { k: 'Visible now', truth: 'inf', title: `${plainName(top.name)} reads tightest`, body: `H100 pressure ${top.live!.pressure!.score.toFixed(0)} (${top.live!.pressure!.band}) across ${live.length} FLOPX regions; ${PRESSURE_MODEL.anomaliesLive ? `${ANOMALY_CARDS.length} open anomalies` : 'anomaly scan not loaded yet'}. ${exploratory} of ${live.length} regions are exploratory: most inputs are FLOPX sample data.` }
      : { k: 'Visible now', truth: 'fct', title: 'Texas is the binding bottleneck', body: <>TX pressure jumped +14 pts in 6 weeks as 600 MW slipped to Q4. That affects H100 premia, power names and the fund’s VST position. <Fresh quiet f="illustrative" /></> },
  ]
  return (
    <div className="grid shrink-0 grid-cols-3 border-b border-line bg-panel/60">
      {cols.map((c, i) => (
        <div key={c.k} className={cx('relative px-5 py-3', i < 2 && 'border-r border-line')}>
          <div className="mb-1 flex items-center gap-2"><span className="num text-[10px] text-dim">0{i + 1}</span><TruthTag truth={c.truth} compact /><span className="eyebrow">{c.k}</span></div>
          <div className="text-[13px] font-semibold text-bone">{c.title}</div>
          <div className="mt-0.5 text-[11.5px] leading-snug text-mute">{c.body}</div>
        </div>
      ))}
      <div className="col-span-3 flex flex-wrap items-center gap-x-6 gap-y-1 border-t border-line px-5 py-2">
        <span className="flex items-center gap-2"><TruthTag truth="obs" compact /><span className="eyebrow">Hidden capacity</span><Fresh quiet f="historical" label={SNAPSHOT.short} /></span>
        <span className="flex min-w-[220px] flex-1 items-center gap-2" title={`${XC.trackedGW} GW frontier AI data-centre capacity across ${XC.sites} sites. ${XC.source}`}>
          <span className="num text-[11px] text-bone">{XC.energizedGW} GW</span>
          <span className="flex h-2 flex-1 overflow-hidden rounded-sm">
            <span title={`Energized ${XC.energizedGW} GW`} style={{ width: `${derived.energizedShare * 100}%`, background: 'var(--color-cat1)' }} />
            <span title={`Not yet energized ${(XC.trackedGW - XC.energizedGW).toFixed(1)} GW`} className="hatch-inf flex-1 border" style={{ borderColor: 'var(--color-dim)' }} />
          </span>
          <span className="num text-[11px] text-dim">{(XC.trackedGW - XC.energizedGW).toFixed(1)} GW</span>
          <span className="text-[11px] text-dim"><span className="num text-bone">{Math.round(derived.energizedShare * 100)}%</span> of <span className="num">{XC.trackedGW} GW</span></span>
        </span>
        {supplyLive.length > 0 && (
          <span className="flex items-center gap-1.5 whitespace-nowrap text-[11px] text-dim" title={`FLOPX hubs now: ${Math.round(supplyLive.reduce((s, r) => s + r.live!.supply!.effectiveMw, 0)).toLocaleString()} MW effective (effective-supply-v1) of ${Math.round(supplyLive.reduce((s, r) => s + r.opMW + r.conMW, 0)).toLocaleString()} MW operating or under construction across ${supplyLive.length} hubs, ${supplyLive.reduce((s, r) => s + r.live!.supply!.facilities, 0)} sample facility records. The Epoch figure to the left is a different tracker and basis.`}>
            FLOPX hubs <span className="num text-bone">{Math.round(supplyLive.reduce((s, r) => s + r.live!.supply!.effectiveMw, 0)).toLocaleString()}</span> of <span className="num">{Math.round(supplyLive.reduce((s, r) => s + r.opMW + r.conMW, 0)).toLocaleString()} MW</span> eff. <Fresh quiet f={supplyLive[0].live!.supply!.freshness} />
          </span>
        )}
        {/* Region ranks: the live cpx-v1 scores (same cells as the table below) once /flopx/pressure has landed; the 7 Nov hand-off's Epoch-region figures, badged, until then. */}
        <span className="flex items-center gap-3" title={ranks.live ? `Top FLOPX regions by ${PRESSURE_MODEL.method ?? 'cpx-v1'} H100 pressure, ${live.length} regions covered` : `7 Nov hand-off: Compute Pressure Index, ${XC.pillars} pillars across ${XC.regions} Epoch regions (not the FLOPX hubs in the table below)`}>
          {!ranks.live && <Fresh quiet f="illustrative" label="7 Nov hand-off regions" />}
          {ranks.rows.map((r) => (
            <span key={r.region} className="flex items-center gap-1.5" title={`${r.region}: ${r.cpi}${r.lo != null && r.hi != null ? ` (${r.lo}–${r.hi})` : ''}${r.band ? ` · ${r.band}` : ''}`}>
              <span className="text-[10.5px] text-dim">{r.region.split(' ')[0]}</span>
              <svg width="64" height="12" aria-hidden>
                <line x1="0" x2="64" y1="6" y2="6" stroke="var(--color-line)" />
                {r.lo != null && r.hi != null && <><line x1={r.lo * 0.64} x2={r.hi * 0.64} y1="6" y2="6" stroke="var(--color-dim)" strokeWidth="1.5" /><line x1={r.lo * 0.64} x2={r.lo * 0.64} y1="3" y2="9" stroke="var(--color-dim)" /><line x1={r.hi * 0.64} x2={r.hi * 0.64} y1="3" y2="9" stroke="var(--color-dim)" /></>}
                <circle cx={r.cpi * 0.64} cy="6" r="3" fill="var(--color-cat2)" />
              </svg>
              <span className="num text-[11px] text-bone">{r.cpi}</span>
            </span>
          ))}
        </span>
      </div>
    </div>
  )
}

function LeftRail() {
  const { xray, setXray } = useStore()
  const animRef = useRef<number | null>(null)
  const revealAnim = () => {
    if (animRef.current) cancelAnimationFrame(animRef.current)
    const from = xray.reveal > 50 ? 100 : 0
    const to = from === 100 ? 0 : 100
    if (to === 100 && !xray.layers.includes('pressure')) setXray((x) => ({ layers: [...x.layers, 'pressure'] }))
    const start = performance.now()
    const tick = (now: number) => {
      const k = Math.min(1, (now - start) / 1400)
      const e = k < 0.5 ? 2 * k * k : 1 - (-2 * k + 2) ** 2 / 2
      setXray({ reveal: Math.round(from + (to - from) * e) })
      if (k < 1) animRef.current = requestAnimationFrame(tick)
    }
    animRef.current = requestAnimationFrame(tick)
  }
  const counts = { good: REGIONS.filter((r) => r.live && r.quality === 'good').length, stale: REGIONS.filter((r) => r.live && r.quality === 'stale').length, sparse: REGIONS.filter((r) => r.live && (r.quality === 'sparse' || r.quality === 'experimental')).length, none: REGIONS.filter((r) => r.quality === 'none').length, illustrative: REGIONS.filter((r) => !r.live && r.quality !== 'none').length }
  return (
    <div className="flex w-[232px] shrink-0 flex-col overflow-y-auto border-r border-line bg-ink">
      <div className="border-b border-line p-4">
        <div className="eyebrow mb-2">Reveal</div>
        <button onClick={revealAnim} className="group relative mb-3 w-full cursor-pointer overflow-hidden rounded-[5px] border border-inf/40 bg-inf/[0.06] px-3 py-2.5 text-left hover:bg-inf/10">
          <div className="flex items-center gap-2 text-[12.5px] font-semibold text-inf"><ScanLine size={14} />{xray.reveal > 50 ? 'Show raw observations' : 'Reveal hidden pressure'}</div>
          <div className="mt-0.5 text-[10.5px] text-mute">{xray.reveal > 50 ? 'Fade inferred state; see what is directly observable' : 'Combine fragments into the inferred state'}</div>
        </button>
        <div className="flex items-center justify-between font-mono text-[9.5px] text-dim"><span className="flex items-center gap-1"><TruthGlyph truth="obs" size={6} />RAW</span><span className="flex items-center gap-1">INFERRED<TruthGlyph truth="inf" size={6} /></span></div>
        <input type="range" min={0} max={100} value={xray.reveal} onChange={(e) => setXray({ reveal: +e.target.value, layers: xray.layers.includes('pressure') ? xray.layers : [...xray.layers, 'pressure'] })} className="mt-1 w-full accent-[var(--color-inf)]" aria-label="Raw to inferred" />
      </div>
      <div className="border-b border-line p-4">
        <div className="eyebrow mb-2">Perspectives · combine freely</div>
        <div className="space-y-1">
          {LAYERS.map((l) => {
            const on = xray.layers.includes(l.id)
            return (
              <button key={l.id} onClick={() => setXray((x) => ({ layers: on ? x.layers.filter((y) => y !== l.id) : [...x.layers, l.id] }))} className={cx('flex w-full cursor-pointer items-start gap-2.5 rounded-[4px] border px-2.5 py-2 text-left transition-colors', on ? 'border-line2 bg-raise' : 'border-transparent hover:bg-panel')}>
                <span className={cx('mt-0.5 flex size-3.5 shrink-0 items-center justify-center rounded-[3px] border', on ? 'border-bone bg-bone' : 'border-line2')}>{on && <span className="size-1.5 bg-ink" />}</span>
                <span className="min-w-0 flex-1">
                  <span className="flex items-center justify-between gap-1"><span className={cx('text-[12px]', on ? 'text-bone' : 'text-mute')}>{l.label}</span><TruthGlyph truth={l.truth} size={7} /></span>
                  <span className="block text-[10.5px] leading-tight text-dim">{l.desc}</span>
                </span>
              </button>
            )
          })}
          {(() => {
            const on = xray.t > NOW_IDX
            return (
              <button onClick={() => setXray({ t: on ? NOW_IDX : Math.min(38, NOW_IDX + 6) })} aria-pressed={on} className={cx('flex w-full cursor-pointer items-start gap-2.5 rounded-[4px] border px-2.5 py-2 text-left transition-colors', on ? 'border-line2 bg-raise' : 'border-transparent hover:bg-panel')}>
                <span className={cx('mt-0.5 flex size-3.5 shrink-0 items-center justify-center rounded-[3px] border', on ? 'border-bone bg-bone' : 'border-line2')}>{on && <span className="size-1.5 bg-ink" />}</span>
                <span className="min-w-0 flex-1">
                  <span className="flex items-center justify-between gap-1"><span className={cx('text-[12px]', on ? 'text-bone' : 'text-mute')}>Forecast</span><TruthGlyph truth="fct" size={7} /></span>
                  <span className="block text-[10.5px] leading-tight text-dim">{on ? <>Projected to <span className="num">{MONTHS[xray.t]}</span>, +{xray.t - NOW_IDX}M</> : 'Model projection, set horizon on the timeline'}</span>
                </span>
              </button>
            )
          })()}
        </div>
      </div>
      <div className="border-b border-line p-4">
        <div className="eyebrow mb-2">Legend</div>
        <div className="mb-1 h-2 rounded-full" style={{ background: `linear-gradient(90deg, ${[30, 45, 55, 65, 75, 85, 95].map((p) => pressureColor(p)).join(',')})` }} />
        <div className="mb-3 flex justify-between font-mono text-[9px] text-dim">{PRESSURE_BAND_WORDS().map((w) => <span key={w}>{w}</span>)}</div>
        <div className="space-y-1.5 text-[10.5px] text-mute">
          <LegendRow icon={<svg width="16" height="16"><circle cx="8" cy="8" r="6.5" fill="none" stroke="var(--color-inf)" strokeDasharray="1 2.5" /></svg>}>Uncertainty ring (wider = less sure)</LegendRow>
          <LegendRow icon={<svg width="16" height="16"><circle cx="8" cy="8" r="6.5" fill="none" stroke="var(--color-dec)" strokeDasharray="4 2" /></svg>}>What-if scenario applied</LegendRow>
          <LegendRow icon={<svg width="16" height="16"><rect x="3" y="3" width="10" height="10" fill="var(--color-bone)" /></svg>}>Operational</LegendRow>
          <LegendRow icon={<svg width="16" height="16"><rect x="3" y="3" width="10" height="10" fill="none" stroke="var(--color-bone)" /><rect x="3" y="8" width="10" height="5" fill="var(--color-bone)" opacity=".55" /></svg>}>Under construction</LegendRow>
          <LegendRow icon={<svg width="16" height="16"><rect x="3" y="3" width="10" height="10" fill="none" stroke="var(--color-bone)" /></svg>}>Proposed</LegendRow>
          <LegendRow icon={<svg width="16" height="16"><rect x="3" y="3" width="10" height="10" fill="none" stroke="var(--color-bone)" strokeDasharray="1.5 2" /></svg>}>Announced only</LegendRow>
          <LegendRow icon={<svg width="16" height="16"><circle cx="8" cy="8" r="5" fill="none" stroke="var(--color-dim)" strokeDasharray="2 2" /><line x1="4" y1="12" x2="12" y2="4" stroke="var(--color-dim)" /></svg>}>No data — no estimate</LegendRow>
        </div>
      </div>
      <div className="p-4">
        <div className="eyebrow mb-2">Coverage</div>
        <div className="space-y-1 font-mono text-[10.5px]">
          <div className="flex justify-between text-mute"><span>Full</span><span className="text-bone">{counts.good}</span></div>
          <div className="flex justify-between text-mute"><span className="text-warn">Stale feed</span><span className="text-bone">{counts.stale}</span></div>
          <div className="flex justify-between text-mute"><span>Sparse / experimental</span><span className="text-bone">{counts.sparse}</span></div>
          <div className="flex justify-between text-mute"><span>Illustrative (no FLOPX data)</span><span className="text-bone">{counts.illustrative}</span></div>
          <div className="flex justify-between text-mute"><span>No data</span><span className="text-bone">{counts.none}</span></div>
        </div>
      </div>
    </div>
  )
}
const LegendRow = ({ icon, children }: { icon: ReactNode; children: ReactNode }) => <div className="flex items-center gap-2">{icon}<span>{children}</span></div>

function MapOverlays({ status }: { status: boolean }) {
  const { xray, setXray } = useStore()
  const future = xray.t > NOW_IDX
  return (
    <>
      <div className="pointer-events-none absolute top-3 left-3 flex flex-col gap-1.5">
        <div aria-hidden={!status} className={cx('border-l-2 bg-ink/85 py-1 pr-3 pl-2.5 backdrop-blur transition-opacity duration-300', status ? 'pointer-events-auto' : 'opacity-0', future ? 'border-fct' : 'border-line2')}>
          <div className={cx('eyebrow', future && 'text-fct')}>{future ? 'Forecast' : xray.t === NOW_IDX ? 'Latest' : 'Historical'}</div>
          <div className="num text-[12.5px] text-bone">{MONTHS[xray.t]}{future && <span className="ml-1.5 text-dim">+{xray.t - NOW_IDX}M</span>}</div>
          <div className="text-[10.5px] text-dim">{future ? 'Model projection, not observed' : xray.t === NOW_IDX ? 'Observed and inferred' : 'As inferred at the time'}</div>
        </div>
        {xray.scenario && (
          <div className="pointer-events-auto flex items-center gap-2 rounded-[4px] border border-dashed border-dec/50 bg-dec/10 px-2.5 py-1.5 font-mono text-[10.5px] text-dec backdrop-blur">
            WHAT-IF · {regionById(xray.scenario.region)!.short} grid upgrade +{xray.scenario.slipMonths}mo{xray.scenario.powerShock ? ` · power ${xray.scenario.powerShock > 0 ? '+' : ''}${xray.scenario.powerShock}%` : ''}
            <button onClick={() => setXray({ scenario: null })} className="cursor-pointer hover:text-white" aria-label="Clear scenario"><X size={11} /></button>
          </div>
        )}
      </div>
      <div aria-hidden={!status} className={cx('pointer-events-none absolute right-3 bottom-3 flex items-center gap-3 rounded-[4px] border border-line bg-ink/80 px-2.5 py-1.5 font-mono text-[9.5px] text-dim backdrop-blur transition-opacity duration-300', !status && 'opacity-0')}>
        <span className="flex items-center gap-1.5"><TruthGlyph truth="obs" size={7} />Observed {Math.round(25 + 0.75 * (100 - xray.reveal))}%</span>
        <span className="flex items-center gap-1.5"><TruthGlyph truth="inf" size={7} />Inferred {xray.layers.includes('pressure') ? xray.reveal : 0}%</span>
      </div>
    </>
  )
}

function TimeScrubber() {
  const { xray, setXray } = useStore()
  const [playing, setPlaying] = useState(false)
  useEffect(() => {
    if (!playing) return
    const id = setInterval(() => setXray((x) => { if (x.t >= 38) { setPlaying(false); return {} } return { t: x.t + 1 } }), 450)
    return () => clearInterval(id)
  }, [playing, setXray])
  const pct = (i: number) => (i / 38) * 100
  return (
    <div className="@container shrink-0 border-t border-line bg-ink px-4 pt-2.5 pb-3">
      <div className="mb-2 flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2 whitespace-nowrap">
          <button onClick={() => { if (xray.t >= 38) setXray({ t: 0 }); setPlaying((p) => !p) }} className="flex size-7 cursor-pointer items-center justify-center rounded-full border border-line2 text-bone hover:bg-hover" aria-label={playing ? 'Pause' : 'Play timeline'}>{playing ? <Pause size={12} /> : <Play size={12} />}</button>
          <span className="num shrink-0 text-[13px] text-bone">{MONTHS[xray.t]}</span>
          {xray.t > NOW_IDX ? (
            <span className="flex min-w-0 items-center gap-3 text-[11px] text-mute">
              <span className="flex shrink-0 items-center gap-1.5"><TruthGlyph truth="fct" size={7} /><span className="hidden @xl:inline">Forecast</span><span className="num">+{xray.t - NOW_IDX}M</span></span>
              <span className="hidden h-3 w-px bg-line2 @2xl:block" />
              {PRESSURE_MODEL.globalReal
                ? <span className="hidden @2xl:inline" title="One real month of FLOPX pressure: no scored CPI forecast exists yet. The projection is the design's curve scaled to today's reading.">1M hit rate <span className="num text-bone">n/a</span> <span className="text-dim">1 real month</span> <Fresh quiet f="illustrative" label="Projection scaled to today, illustrative" /></span>
                : <span className="hidden @2xl:inline" title="FLOPX pressure has not loaded: no scored CPI forecast exists. The projection is the design's curve.">1M hit rate <span className="num text-bone">n/a</span> <span className="text-dim">not loaded</span> <Fresh quiet f="illustrative" label="Projection illustrative" /></span>}
              <span className="h-3 w-px bg-line2" /><span className="shrink-0 cursor-help text-warn" title={PRESSURE_MODEL.globalReal ? 'No CPI forecast has been validated: FLOPX has one real month of pressure history' : 'No CPI forecast has been validated: FLOPX pressure has not loaded'}>Unvalidated</span>
            </span>
          ) : (
            <span className="text-[11px] text-dim">{xray.t === NOW_IDX ? 'Latest' : `${NOW_IDX - xray.t}M ago`}</span>
          )}
        </div>
        <Seg size="sm" value={String(xray.t - NOW_IDX)} onChange={(v) => setXray({ t: NOW_IDX + +v })} options={[{ id: '-12', label: '−12M' }, { id: '-6', label: '−6M' }, { id: '0', label: 'NOW' }, { id: '1', label: '+1M' }, { id: '3', label: '+3M' }, { id: '6', label: '+6M' }, { id: '12', label: '+12M' }]} />
      </div>
      <div className="relative h-8">
        <div className="absolute inset-x-0 top-3.5 h-1 bg-line" />
        <div className="absolute top-3.5 h-1 bg-mute/45" style={{ left: 0, width: `${pct(NOW_IDX)}%` }} />
        <div className="absolute top-3.5 h-1 bg-fct/25" style={{ left: `${pct(NOW_IDX)}%`, right: 0 }} />
        <div className="absolute top-2 h-4 w-px bg-bone/70" style={{ left: `${pct(NOW_IDX)}%` }} />
        <span className="absolute -top-1 -translate-x-1/2 font-mono text-[8.5px] tracking-wider text-mute" style={{ left: `${pct(NOW_IDX)}%` }}>NOW</span>
        {MONTHS.map((m, i) => (i % 3 === 0 ? <span key={i} className="absolute top-6 -translate-x-1/2 font-mono text-[8.5px] whitespace-nowrap text-dim" style={{ left: `${pct(i)}%` }}>{m}</span> : null))}
        <input type="range" min={0} max={38} value={xray.t} onChange={(e) => setXray({ t: +e.target.value })} className="absolute inset-x-0 top-1.5 h-5 w-full cursor-pointer opacity-0" aria-label="Time" />
        <div className={cx('pointer-events-none absolute top-1.5 h-5 w-[3px] -translate-x-1/2 rounded-[1px]', xray.t > NOW_IDX ? 'bg-fct' : 'bg-bone')} style={{ left: `${pct(xray.t)}%` }} />
      </div>
    </div>
  )
}

function RightPanel({ region }: { region: Region | null }) {
  const { xray, setXray, ask } = useStore()
  if (!region) return <GlobalPanel />
  const p = pressureAt(region, xray.t, xray.scenario)
  const u = uncAt(region, xray.t, xray.scenario)
  const tabs: { id: typeof xray.tab; label: string }[] = [
    { id: 'brief', label: region.anomaly ? 'Anomaly' : 'Brief' },
    { id: 'evidence', label: 'Evidence' },
    { id: 'pipeline', label: 'Pipeline' },
    { id: 'exposure', label: 'Exposure' },
    { id: 'whatif', label: 'What-if' },
  ]
  return (
    <div className="flex w-[420px] shrink-0 flex-col border-l border-line bg-panel">
      <div className="border-b border-line px-5 pt-4 pb-3">
        <div className="flex items-start justify-between">
          <div>
            <button onClick={() => setXray({ region: null, project: null })} className="eyebrow mb-1 cursor-pointer hover:text-mute">← Global</button>
            <h2 className="font-serif text-[26px] leading-none">{region.name}</h2>
            <div className="mt-1.5 flex flex-wrap items-center gap-1.5 font-mono text-[10px] text-dim">{region.marketplace} · grid {region.grid.toUpperCase()}{region.quality !== 'good' && <Fresh f={region.quality === 'stale' ? 'stale' : region.quality === 'none' ? 'unavailable' : 'experimental'} label={`${region.quality} coverage`} />}{!region.live ? <Fresh quiet f="illustrative" label="Illustrative · no FLOPX data" /> : !region.live.pressure?.realSlots[xray.t] && <Fresh quiet f="illustrative" label={region.live.pressure?.scaled ? `${MONTHS[xray.t]} scaled to today's reading, illustrative` : `${MONTHS[xray.t]} illustrative`} />}</div>
          </div>
          <button onClick={() => ask(`Why is compute pressure ${pressureWord(p).toLowerCase()} in ${region.name}?`)} className="cursor-pointer rounded-[4px] border border-fct/30 p-1.5 text-fct hover:bg-fct/10" aria-label="Ask about region"><Bot size={14} /></button>
        </div>
        {p != null && (
          <div className="mt-3 flex items-end gap-4">
            <div>
              <TruthTag truth={xray.t > NOW_IDX ? 'fct' : 'inf'} />
              <div className="num mt-0.5 text-[34px] leading-none" style={{ color: pressureColor(p) }}>{p.toFixed(0)}<span className="ml-1.5 text-[13px] text-mute" title={region.live ? `The ± band is illustrative: ${PRESSURE_MODEL.method} publishes coverage (${region.live.pressure?.label}), not a numeric band` : undefined}>± {u.toFixed(0)}</span></div>
              <div className="mt-0.5 text-[11px] text-mute">{pressureWord(p)} · {MONTHS[xray.t]}</div>
            </div>
            <TimeChart labels={MONTHS} height={70} nowIdx={NOW_IDX} hideForecastZone series={[{ id: 'p', label: 'CPI', values: region.pressure.map((v, i) => (i <= NOW_IDX ? v : null)), color: 'var(--color-inf)' }, { id: 'f', label: 'Forecast', values: region.pressure.map((v, i) => (i >= NOW_IDX ? pressureAt(region, i, xray.scenario) : null)), color: 'var(--color-fct)', style: 'dashed', band: { lo: region.pressure.map((_, i) => (i >= NOW_IDX ? pressureAt(region, i, xray.scenario)! - uncAt(region, i, xray.scenario) : null)), hi: region.pressure.map((_, i) => (i >= NOW_IDX ? pressureAt(region, i, xray.scenario)! + uncAt(region, i, xray.scenario) : null)) } }]} cursor={xray.t} onCursor={(i) => setXray({ t: i })} className="flex-1 [&_text]:hidden" />
          </div>
        )}
      </div>
      <div className="flex gap-4 border-b border-line px-5">
        {tabs.map((t) => (
          <button key={t.id} onClick={() => setXray({ tab: t.id })} className={cx('-mb-px cursor-pointer border-b-2 py-2 text-[12px] font-medium', xray.tab === t.id ? 'border-bone text-bone' : 'border-transparent text-dim hover:text-mute')}>
            {t.label}{t.id === 'brief' && region.anomaly && <span className="ml-1 inline-block size-1.5 rounded-full bg-inf align-middle" />}
          </button>
        ))}
      </div>
      <div className="flex-1 overflow-y-auto px-5 py-4">
        {region.quality === 'none' ? (
          <div className="rounded-md border border-dashed border-line2 p-5 text-center">
            <div className="mb-1 text-[13px] font-semibold">No data region</div>
            <p className="text-[12px] leading-relaxed text-mute">FLOPX has no usable rental quotes, power-market data or queue data for {region.name}. We show <b className="text-bone">no estimate</b> instead of extrapolating.{region.opMW > 0 && <> {region.opMW} MW of facilities are reported (observed, from press). <Fresh quiet f="illustrative" /></>}</p>
            <Btn size="sm" className="mt-3" onClick={() => ask(`What do we know about ${region.name}?`)}>Ask what is known</Btn>
          </div>
        ) : (
          <>
            {xray.tab === 'brief' && <BriefTab r={region} />}
            {xray.tab === 'evidence' && <EvidenceTab r={region} />}
            {xray.tab === 'pipeline' && <PipelineTab r={region} />}
            {xray.tab === 'exposure' && <ExposureTab r={region} />}
            {xray.tab === 'whatif' && <WhatIfTab r={region} />}
          </>
        )}
      </div>
    </div>
  )
}

function GlobalPanel() {
  const { xray, setXray } = useStore()
  // Δ2M compares two months; with only the current month real, a covered region has no honest Δ2M yet.
  const ranked = REGIONS.filter((r) => r.quality !== 'none').map((r) => ({ r, p: pressureAt(r, xray.t, xray.scenario)!, d: !r.live || (r.live.pressure?.realSlots[NOW_IDX - 2] && r.live.pressure?.realSlots[NOW_IDX]) ? r.pressure[NOW_IDX] - r.pressure[NOW_IDX - 2] : null })).sort((a, b) => b.p - a.p)
  const cards = ANOMALY_CARDS.slice(0, 2)
  return (
    <div className="flex w-[420px] shrink-0 flex-col border-l border-line bg-panel">
      <div className="border-b border-line px-5 py-4">
        <div className="eyebrow mb-1 flex items-center gap-2">Global view · {MONTHS[xray.t]}{PRESSURE_MODEL.globalReal && <Fresh quiet f="illustrative" label={PRESSURE_MODEL.globalScaled ? SCALED_SHORT_LABEL : `History before ${MONTHS[NOW_IDX]} illustrative`} />}</div>
        <h2 className="font-serif text-[24px] leading-tight">Where is compute scarce?</h2>
        <p className="mt-1 text-[12px] text-mute">Select a region on the map or below to drill into observed evidence and inferred pressure.</p>
      </div>
      <div className="border-b border-line px-5 py-3">
        <div className="eyebrow mb-2 flex items-center gap-2"><span className="size-1.5 rounded-full bg-inf" />Anomalies detected{!PRESSURE_MODEL.anomaliesLive && <Fresh quiet f="illustrative" />}{PRESSURE_MODEL.anomaliesLive && ANOMALY_CARDS.length > cards.length && <span className="font-mono text-[9.5px] normal-case tracking-normal text-dim">showing {cards.length} of {ANOMALY_CARDS.length}, most severe first</span>}</div>
        {PRESSURE_MODEL.anomaliesLive ? (
          cards.length ? cards.map((c, i) => (
            <button key={c.id} onClick={() => setXray({ region: c.region, tab: 'brief' })} className={cx('w-full cursor-pointer rounded-md border p-3 text-left', i === 0 ? 'border-inf/30 bg-inf/[0.05] hover:bg-inf/10' : 'mt-1.5 border-line hover:bg-hover')}>
              <div className="flex items-center justify-between"><span className="text-[13px] font-semibold">{c.title}</span><ArrowRight size={13} className={i === 0 ? 'text-inf' : 'text-dim'} /></div>
              <div className="mt-0.5 text-[11.5px] text-mute">{c.sub}</div>
            </button>
          )) : <div className="rounded-md border border-line p-3 text-[11.5px] text-mute">{PRESSURE_MODEL.anomaliesLive ? 'No open anomalies in the FLOPX regions.' : 'Anomaly scan not loaded yet.'}</div>
        ) : (
          <>
            <button onClick={() => setXray({ region: 'us-tx', tab: 'brief' })} className="w-full cursor-pointer rounded-md border border-inf/30 bg-inf/[0.05] p-3 text-left hover:bg-inf/10">
              <div className="flex items-center justify-between"><span className="text-[13px] font-semibold">Texas: pressure +14 pts in 6 weeks</span><ArrowRight size={13} className="text-inf" /></div>
              <div className="mt-0.5 flex items-center gap-1.5 text-[11.5px] text-mute">Largest move since coverage began · confidence moderate (0.64)</div>
            </button>
            <button onClick={() => setXray({ region: 'no-nordics', tab: 'brief' })} className="mt-1.5 w-full cursor-pointer rounded-md border border-line p-3 text-left hover:bg-hover">
              <div className="flex items-center justify-between"><span className="text-[13px] font-semibold">Nordics: loosening −7 pts YoY</span><ArrowRight size={13} className="text-dim" /></div>
              <div className="mt-0.5 flex items-center gap-1.5 text-[11.5px] text-mute">New supply energised on schedule</div>
            </button>
          </>
        )}
      </div>
      <div className="flex-1 overflow-y-auto px-5 py-3">
        <div className="eyebrow mb-2 grid grid-cols-[1fr_60px_56px_44px] gap-2"><span>Region</span><span>24M</span><span className="text-right">CPI</span><span className="text-right">Δ2M</span></div>
        {ranked.map(({ r, p, d }) => (
          <button key={r.id} onClick={() => setXray({ region: r.id, tab: 'brief' })} className="grid w-full cursor-pointer grid-cols-[1fr_60px_56px_44px] items-center gap-2 border-b border-line/60 py-2 text-left hover:bg-hover">
            <span className="flex min-w-0 items-center gap-1.5 text-[12px]"><span className="truncate">{r.name}</span>{!r.live ? <span className="shrink-0 text-[11px] text-dim font-medium">Illustrative</span> : r.quality !== 'good' && <span className="shrink-0 text-[11px] text-warn font-medium capitalize">{r.quality}</span>}</span>
            <Spark values={r.pressure.slice(0, NOW_IDX + 1)} width={60} height={16} color="var(--color-inf)" />
            <span className="num text-right text-[12px]" style={{ color: pressureColor(p) }}>{p.toFixed(0)}<span className="text-[9.5px] text-dim">±{uncAt(r, xray.t).toFixed(0)}</span></span>
            {d == null ? <span className="num text-right text-[11px] text-dim" title="Only the current month is a FLOPX reading so far">n/a</span> : <Delta v={d} d={0} suffix="" className="text-right text-[11px]" />}
          </button>
        ))}
        <button onClick={() => setXray({ region: 'za-jnb' })} className="grid w-full cursor-pointer grid-cols-[1fr_auto] py-2 text-left text-[12px] text-dim hover:bg-hover"><span>Johannesburg</span><span className="font-mono text-[10px]">NO DATA</span></button>
      </div>
    </div>
  )
}

function BriefTab({ r }: { r: Region }) {
  const { setXray, inspect } = useStore()
  const a = r.anomaly
  if (!a) {
    return (
      <div className="space-y-4">
        {r.note && <div className="rounded-md border border-line bg-ink p-3 text-[12.5px] text-bone/90">{r.note}</div>}
        <div className="grid grid-cols-2 gap-2">
          <Stat label="H100 median" value={r.h100 ? `$${r.h100.toFixed(2)}` : '—'} truth="obs" sub={r.h100 ? (Number.isFinite(r.h100Chg) ? `${fmtPct(r.h100Chg)} 30d` : 'no 30d history yet') : 'no quotes'} />
          <Stat label="Premium" value={fmtPct(r.premium, 0)} truth="obs" sub="vs global" />
          <Stat label="Operational" value={`${(r.opMW / 1000).toFixed(2)} GW`} truth="obs" sub="reported" />
          <Stat label={r.live?.supply ? 'Usable additions' : `Usable by ${horizonLabel(12)}`} value={`+${r.eff27[0]}–${r.eff27[1]}`} truth="inf" sub={r.live?.supply ? `MW of ${r.announced27} in pipeline (sample records)` : `MW vs ${r.announced27} announced`} />
        </div>
        <div>
          <div className="eyebrow mb-2">Top drivers of the inference</div>
          {r.contrib.map((c) => <div key={c.factor} className="mb-1.5"><div className="mb-0.5 flex justify-between text-[11.5px]"><span className="text-mute">{c.factor}</span><span className="num">{(c.value * 100).toFixed(0)}%</span></div><Meter value={c.value} hatched color="var(--color-inf)" /></div>)}
        </div>
        <Btn size="sm" variant="subtle" onClick={() => setXray({ tab: 'evidence' })}>See all evidence <ArrowRight size={11} /></Btn>
      </div>
    )
  }
  const Q = ({ n, q, children, truth }: { n: number; q: string; children: ReactNode; truth: 'obs' | 'inf' | 'fct' | 'dec' }) => (
    <div className="border-b border-line pb-4 last:border-0">
      <div className="mb-2 flex items-center justify-between"><div className="flex items-center gap-2"><span className="num text-[10px] text-dim">Q{n}</span><span className="text-[13px] font-semibold">{q}</span></div><TruthTag truth={truth} compact /></div>
      {children}
    </div>
  )
  return (
    <div className="space-y-4">
      <div className="rounded-md border border-inf/30 bg-inf/[0.05] p-3">
        <div className="flex items-center justify-between"><span className="text-[13px] font-semibold text-inf">{a.title}</span><span className="font-mono text-[10px] text-dim">detected {a.detected}</span></div>
      </div>
      <Q n={1} q="What changed?" truth="obs">
        <p className="mb-2 text-[12px] leading-relaxed text-bone/90">{a.changed}</p>
        <ul className="space-y-1">{a.changedPoints.map((c) => <li key={c} className="flex gap-2 text-[11.5px] text-mute"><TruthGlyph truth="obs" size={6} /><span className="-mt-0.5">{c}</span></li>)}</ul>
      </Q>
      <Q n={2} q="Why might it have changed?" truth="inf">
        <div className="space-y-2">
          {a.why.map((w) => (
            <div key={w.text} className="grid grid-cols-[64px_1fr] gap-2 text-[12px]">
              <span className={cx('h-fit rounded-[3px] border px-1 text-center text-[11px] font-medium', w.support === 'strong' ? 'border-inf/50 text-inf' : w.support === 'moderate' ? 'border-line2 text-mute' : 'border-dashed border-line2 text-dim')}>{w.support}</span>
              <span className="text-bone/90">{w.text}</span>
            </div>
          ))}
        </div>
        {a.conflicting && <div className="mt-3 rounded-[4px] border border-warn/30 bg-warn/[0.04] p-2.5 text-[11.5px]"><span className="mb-0.5 block text-[11px] text-warn font-medium">Conflicting evidence</span><span className="text-bone/85">{a.conflicting}</span></div>}
      </Q>
      <Q n={3} q="How confident are we?" truth="inf">
        <div className="mb-2 flex items-center gap-3"><span className="num text-[20px]">{a.confidence.score.toFixed(2)}</span><div className="flex-1"><Meter value={a.confidence.score} color="var(--color-inf)" /></div><span className="font-mono text-[11px] text-inf">{a.confidence.level}</span></div>
        <ul className="space-y-1">{a.confidence.notes.map((n) => <li key={n} className="text-[11.5px] text-mute">· {n}</li>)}</ul>
      </Q>
      <Q n={4} q="What does it affect?" truth="dec">
        <div className="space-y-1.5">
          {a.affects.map((x) => (
            <button key={x.id} onClick={() => inspect({ kind: x.kind, id: x.id, label: x.label })} className="flex w-full cursor-pointer items-start gap-2 rounded-[4px] border border-line p-2 text-left hover:bg-hover">
              <span className={cx('mt-2 inline-block w-3 shrink-0', x.link === 'documented' ? 'h-px bg-bone' : 'border-t border-dashed border-inf')} />
              <span className="flex-1"><span className="block text-[12px] text-bone">{x.label}</span><span className="block text-[11px] text-mute">{x.note}</span></span>
              <span className="text-[11px] text-dim font-medium">{x.link}</span>
            </button>
          ))}
        </div>
        <Btn size="sm" variant="decision" className="mt-3 w-full" onClick={() => setXray({ tab: 'exposure' })}>Follow into securities & fund exposure <ArrowRight size={12} /></Btn>
      </Q>
    </div>
  )
}

function EvidenceTab({ r }: { r: Region }) {
  const ev = regionEvidence(r)
  // Observations carry their status once; only a row that differs from the rest keeps a chip.
  const evFresh = dominantFresh(ev.map((e) => e.freshness))
  return (
    <div className="space-y-5">
      <div>
        <div className="eyebrow mb-2 flex items-center gap-2">Observations feeding the estimate{evFresh && <Fresh quiet f={evFresh} label={ev.some((e) => e.freshness !== evFresh) ? `${evFresh} unless a row says otherwise` : undefined} />}</div>
        <div className="space-y-1.5">
          {ev.map((e) => (
            <div key={e.id} className={cx('rounded-[4px] border p-2.5', e.conflict ? 'border-warn/35 bg-warn/[0.03]' : 'border-line')}>
              <div className="flex items-start justify-between gap-2">
                <div className="flex items-start gap-2"><span className="mt-1"><TruthGlyph truth={e.truth} size={7} /></span><span className="text-[12px] text-bone">{e.label}</span></div>
                <span className="num shrink-0 text-[12px]">{e.value}</span>
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-1.5 pl-[15px]">
                {e.freshness !== evFresh && <Fresh f={e.freshness} />}
                <Source source={e.source} time={e.time} />
                {e.conflict && <span className="text-[11px] text-warn font-medium">Conflicts</span>}
              </div>
              {e.note && <div className="mt-1 pl-[15px] text-[10.5px] text-dim">{e.note}</div>}
              <div className="mt-1.5 flex items-center gap-2 pl-[15px]"><span className="font-mono text-[9px] text-dim">weight</span><Meter value={e.weight} max={0.4} className="h-1" color="var(--color-mute)" /></div>
            </div>
          ))}
        </div>
      </div>
      <div>
        <div className="eyebrow mb-2 flex items-center gap-2"><TruthGlyph truth="inf" size={7} />How the pressure estimate is built</div>
        {r.contrib.map((c) => <div key={c.factor} className="mb-1.5"><div className="mb-0.5 flex justify-between text-[11.5px]"><span className="text-mute">{c.factor}</span><span className="num">{(c.value * 100).toFixed(0)}%</span></div><Meter value={c.value} hatched color="var(--color-inf)" /></div>)}
      </div>
      <div className="rounded-md border border-line bg-ink p-3">
        <div className="eyebrow mb-1.5">Assumptions</div>
        <ul className="space-y-1 text-[11.5px] text-mute">
          <li>· Rental premia reflect local scarcity after controlling for contract type and interconnect.</li>
          <li>· Utilisation is <b className="text-bone">not observed</b>. Sold-out rates are used as a proxy.</li>
          <li>· Announced MW is discounted by stage-specific historical completion rates.</li>
          <li>· Hyperscaler internal capacity is unobservable and excluded.</li>
        </ul>
      </div>
    </div>
  )
}

function PipelineTab({ r }: { r: Region }) {
  const { xray, setXray, inspect } = useStore()
  const projects = PROJECTS.filter((p) => p.region === r.id)
  const sel = xray.project ? projectById(xray.project) : null
  return (
    <div className="space-y-4">
      <div className="rounded-md border border-line bg-ink p-3">
        <div className="mb-2 flex justify-between font-mono text-[10px] text-dim"><span>{r.live?.supply ? `Capacity pipeline · FLOPX ${r.live.supply.method}` : `Capacity by ${horizonLabel(12)}`}</span><span className="flex items-center gap-1.5">{r.live?.supply ? <Fresh quiet f={r.live.supply.freshness} label="sample facility records" /> : <Fresh quiet f="illustrative" />}MW</span></div>
        <div className="space-y-2">
          <div><div className="mb-0.5 flex justify-between text-[11.5px]"><span className="flex items-center gap-1.5"><TruthGlyph truth="obs" size={7} />Announced additions</span><span className="num">{r.announced27.toLocaleString()}</span></div><div className="h-2 rounded-sm border border-dashed border-bone/50" /></div>
          <div><div className="mb-0.5 flex justify-between text-[11.5px]"><span className="flex items-center gap-1.5"><TruthGlyph truth="inf" size={7} />Estimated usable</span><span className="num">{r.eff27[0].toLocaleString()}–{r.eff27[1].toLocaleString()}</span></div>
            <div className="relative h-2 rounded-sm bg-line"><div className="hatch-inf absolute inset-y-0 rounded-sm border border-inf" style={{ left: `${(r.eff27[0] / r.announced27) * 100}%`, width: `${((r.eff27[1] - r.eff27[0]) / r.announced27) * 100}%` }} /><div className="absolute inset-y-0 left-0 rounded-l-sm bg-inf/60" style={{ width: `${(r.eff27[0] / r.announced27) * 100}%` }} /></div>
          </div>
        </div>
        <div className="mt-2 text-[10.5px] text-dim">Announced megawatts are not operational compute. The gap is FLOPX’s estimate of slippage and attrition.</div>
      </div>
      <div className="space-y-1.5">
        {projects.length === 0 && <div className="text-[12px] text-mute">No tracked projects in this region.</div>}
        {projects.map((p) => (
          <button key={p.id} onClick={() => setXray({ project: p.id })} className={cx('w-full cursor-pointer rounded-[4px] border p-2.5 text-left', xray.project === p.id ? 'border-fct/50 bg-fct/[0.04]' : 'border-line hover:bg-hover')}>
            <div className="flex justify-between"><span className="text-[12.5px] font-medium">{p.name}</span><span className="text-[11px] text-mute font-medium capitalize">{p.stage}</span></div>
            <div className="mt-0.5 flex justify-between font-mono text-[10.5px] text-dim"><span>{p.reportedMW} MW reported · {p.energizedMW} energised</span><span>P(on time) {Math.round(p.onTimeProb * 100)}%</span></div>
          </button>
        ))}
      </div>
      {sel && (
        <div className="fade-in rounded-md border border-line2 p-3.5">
          <div className="mb-3 flex items-start justify-between"><div><div className="eyebrow">Selected project</div><div className="text-[14px] font-semibold">{sel.name}</div><div className="font-mono text-[10px] text-dim">{sel.developer} · expected {sel.expectedOnline}</div></div><Btn size="sm" variant="ghost" onClick={() => inspect({ kind: 'project', id: sel.id, label: sel.name })}>Inspect</Btn></div>
          <Timeline p={sel} />
          <div className="mt-2 mb-3 space-y-1">
            <div className="eyebrow">Evidence</div>
            {sel.evidence.map((e) => <div key={e.id} className="flex items-start justify-between gap-2 text-[11.5px]"><span className="text-mute">{e.label}: <span className="text-bone">{e.value}</span></span><Source source={e.source} time={e.time} className="shrink-0 text-right" /></div>)}
          </div>
          {sel.uncertainties.length > 0 && <div className="mb-3"><div className="eyebrow mb-1">Uncertainties</div>{sel.uncertainties.map((u) => <div key={u} className="text-[11.5px] text-mute">· {u}</div>)}</div>}
          <div className="rounded-[4px] border border-inf/30 bg-inf/[0.04] p-2.5">
            <div className="mb-1 flex items-center gap-2"><TruthTag truth="inf" compact /><span className="text-[11px] text-dim font-medium">Plausible impact on local effective supply</span></div>
            <div className="text-[12px] text-bone/90">{sel.impact}</div>
            <div className="num mt-1.5 text-[11px] text-mute">Usable by {horizonLabel(12)}: {sel.effectiveRange[0]}–{sel.effectiveRange[1]} MW of {sel.reportedMW} reported</div>
          </div>
          {sel.stage === 'construction' && <Btn size="sm" variant="outline" className="mt-3 w-full" onClick={() => setXray({ tab: 'whatif', scenario: { region: sel.region, slipMonths: 6, powerShock: 0 } })}>What if this slips another 6 months?</Btn>}
        </div>
      )}
    </div>
  )
}

function ExposureTab({ r }: { r: Region }) {
  const { positions, navigate, setFund, inspect, nav } = useStore()
  const tickers = Array.from(new Set([...r.documented.map((d) => d.t), ...r.sensitivities.map((x) => x.t)])).filter((t) => lastPx(t))
  const held = positions.filter((p) => tickers.includes(p.ticker))
  const net = held.reduce((s, p) => s + p.qty * lastPx(p.ticker), 0)
  return (
    <div className="space-y-5">
      <div>
        <div className="eyebrow mb-2 flex items-center gap-2"><span className="inline-block h-px w-4 bg-bone" />Documented ties · observed facts</div>
        {r.documented.length === 0 && <div className="text-[12px] text-mute">None documented.</div>}
        {r.documented.map((d) => (
          <div key={d.t} className="mb-1.5 rounded-[4px] border border-line p-2.5">
            <div className="flex justify-between"><EntityLink r={{ kind: 'security', id: d.t, label: d.t }} className="font-mono text-[12px]" /><Source source={d.source} /></div>
            <div className="mt-0.5 text-[11.5px] text-mute">{d.tie}</div>
          </div>
        ))}
      </div>
      <div>
        <div className="eyebrow mb-2 flex items-center gap-2"><span className="inline-block w-4 border-t border-dashed border-inf" />Modelled sensitivities · estimated</div>
        {r.sensitivities.length === 0 && <div className="text-[12px] text-mute">No statistically meaningful sensitivity found.</div>}
        {r.sensitivities.map((x) => (
          <div key={x.t} className="mb-1.5 grid grid-cols-[52px_1fr_auto] items-center gap-3 rounded-[4px] border border-dashed border-inf/30 p-2.5">
            <EntityLink r={{ kind: 'security', id: x.t, label: x.t }} className="font-mono text-[12px]" />
            <div><Meter value={Math.abs(x.beta)} max={0.6} hatched color="var(--color-inf)" /></div>
            <span className="num text-[11px] text-mute">β {x.beta} · R² {Number.isFinite(x.r2) ? x.r2 : 'n/a'}</span>
          </div>
        ))}
        <div className="text-[10.5px] text-dim">{r.live?.exposure ? 'Sensitivity = FLOPX authored estimate per company role (percent equity move per percent compute-demand move), from sample facility and profile records. It is not a measured beta.' : 'Sensitivity = historical co-movement of excess returns with regional pressure. It is an association, not proof that pressure causes the returns.'}{!r.live?.exposure && <> <Fresh quiet f="illustrative" /></>}</div>
      </div>
      <div className="rounded-md border border-dec/30 bg-dec/[0.04] p-3.5">
        <div className="mb-2 flex items-center justify-between"><TruthTag truth="dec" /><span className="font-mono text-[10px] text-dim">CPX-1 paper portfolio</span></div>
        {held.length === 0 ? <div className="text-[12px] text-mute">The fund has no exposure to companies tied to {r.short}.</div> : (
          <>
            <div className="num mb-2 text-[20px]">{net >= 0 ? '+' : '−'}${Math.abs(Math.round(net)).toLocaleString()} <span className="text-[11px] text-mute">net · {((net / nav) * 100).toFixed(1)}% NAV</span></div>
            {held.map((p) => <div key={p.ticker} className="flex justify-between font-mono text-[11px]"><button className="cursor-pointer hover:underline" onClick={() => inspect({ kind: 'security', id: p.ticker, label: p.ticker })}>{p.ticker} · {p.strategy}</button><span>{p.qty > 0 ? 'Long' : 'Short'} ${Math.abs(Math.round(p.qty * lastPx(p.ticker))).toLocaleString()}</span></div>)}
            {r.id === 'us-tx' && !r.live && <div className="mt-2 rounded-[3px] border border-line p-2 text-[11.5px] text-bone/90">This anomaly <b>raised CPX-1’s VST target from 9.3% to 10.8%</b> (proposal p1, awaiting your approval).</div>}
          </>
        )}
        <Btn size="sm" variant="decision" className="mt-3 w-full" onClick={() => { setFund({ tab: r.id === 'us-tx' ? 'decisions' : 'portfolio' }); navigate('fund') }}>Open in Systematic Fund <ArrowRight size={12} /></Btn>
      </div>
    </div>
  )
}

function WhatIfTab({ r }: { r: Region }) {
  const { xray, setXray } = useStore()
  const sc = xray.scenario && xray.scenario.region === r.id ? xray.scenario : null
  const draft = sc ?? { region: r.id, slipMonths: 0, powerShock: 0 }
  const imp = scenarioImpact(r, draft)
  const base6 = r.pressure[Math.min(38, NOW_IDX + 6)]
  const base12 = r.pressure[38]
  const set = (p: Partial<typeof draft>) => setXray({ scenario: { ...draft, ...p }, t: xray.t <= NOW_IDX ? NOW_IDX + 6 : xray.t })
  const Row = ({ label, before, after, unit = '', truth }: { label: string; before: string; after: string; unit?: string; truth: 'inf' | 'fct' }) => (
    <div className="grid grid-cols-[1fr_70px_14px_70px] items-center gap-2 border-b border-line/60 py-2 text-[12px]">
      <span className="flex items-center gap-1.5 text-mute"><TruthGlyph truth={truth} size={7} />{label}</span>
      <span className="num text-right text-dim">{before}{unit}</span>
      <ArrowRight size={11} className="text-dim" />
      <span className={cx('num text-right', before !== after ? 'text-dec' : 'text-bone')}>{after}{unit}</span>
    </div>
  )
  return (
    <div className="space-y-4">
      <div className="hatch-sim rounded-md border border-dashed border-dec/40 p-3.5">
        <div className="mb-1 flex items-center gap-1.5 text-[11px] text-dec font-medium">Hypothetical scenario · not a forecast <Fresh quiet f="illustrative" label="illustrative model" /></div>
        <div className="text-[13px] font-semibold">What if {r.short}’s grid upgrade slips?</div>
        <div className="mt-3 space-y-3">
          <label className="block">
            <div className="flex justify-between text-[11.5px]"><span className="text-mute">Grid upgrade delay</span><span className="num">{draft.slipMonths} months</span></div>
            <input type="range" min={0} max={24} step={3} value={draft.slipMonths} onChange={(e) => set({ slipMonths: +e.target.value })} className="w-full accent-[var(--color-dec)]" />
          </label>
          <label className="block">
            <div className="flex justify-between text-[11.5px]"><span className="text-mute">Power price shock</span><span className="num">{draft.powerShock > 0 ? '+' : ''}{draft.powerShock}%</span></div>
            <input type="range" min={-30} max={60} step={5} value={draft.powerShock} onChange={(e) => set({ powerShock: +e.target.value })} className="w-full accent-[var(--color-dec)]" />
          </label>
          <div className="flex gap-1.5">
            <Btn size="sm" variant="subtle" onClick={() => set({ slipMonths: 12, powerShock: 0 })}>Slip 1 year</Btn>
            <Btn size="sm" variant="subtle" onClick={() => set({ slipMonths: 6, powerShock: 30 })}>Hot summer</Btn>
            {sc && <Btn size="sm" variant="ghost" onClick={() => setXray({ scenario: null })}>Clear</Btn>}
          </div>
        </div>
      </div>
      <div>
        <div className="eyebrow mb-1 grid grid-cols-[1fr_70px_14px_70px] gap-2"><span>Estimated quantity</span><span className="text-right">Base</span><span /><span className="text-right">Scenario</span></div>
        <Row label={`Usable MW by ${horizonLabel(12)} (low)`} before={r.eff27[0].toLocaleString()} after={imp.eff[0].toLocaleString()} truth="inf" />
        <Row label={`Usable MW by ${horizonLabel(12)} (high)`} before={r.eff27[1].toLocaleString()} after={imp.eff[1].toLocaleString()} truth="inf" />
        <Row label={`Pressure ${MONTHS[NOW_IDX + 6]}`} before={base6.toFixed(0)} after={(base6 + imp.d6).toFixed(0)} truth="fct" />
        <Row label={`Pressure ${MONTHS[38]}`} before={base12.toFixed(0)} after={(base12 + imp.d12).toFixed(0)} truth="fct" />
        <Row label="Regional H100 premium (6M)" before={fmtPct(r.premium, 0)} after={fmtPct(imp.premiumAfter, 0)} truth="fct" />
      </div>
      <AIBlock>
        {draft.slipMonths || draft.powerShock ? <>Removing ~{imp.slipMW.toLocaleString()} MW of 2027 usable supply in {r.short} would likely keep pressure {pressureWord(base6 + imp.d6).toLowerCase()} through {MONTHS[NOW_IDX + 6]}. Modelled sensitivities suggest power names (e.g. {r.sensitivities[0]?.t ?? '—'}) would see a larger estimated tailwind, and neocloud delivery timelines a headwind. Uncertainty bands widen by 25% because scenarios extrapolate beyond observed regimes.</> : 'Move a slider to run a scenario. Only estimated quantities change. Observed history is never altered.'}
      </AIBlock>
      <div className="rounded-md border border-line bg-ink p-3">
        <div className="eyebrow mb-1.5">Assumptions</div>
        <ul className="space-y-1 text-[11.5px] text-mute">
          <li>· {r.slipSensitivity.toLocaleString()} MW of {r.short} 2027 supply depends on the pending upgrade (project tracker mapping).</li>
          <li>· Demand path unchanged from base forecast.</li>
          <li>· Pressure response calibrated on 2024–25 slip events (n=9; wide error).</li>
          <li>· No second-order demand migration to neighbouring regions.</li>
        </ul>
      </div>
    </div>
  )
}

function ComparePanel() {
  const { xray, setXray } = useStore()
  const [mode, setMode] = useState<'region' | 'gpu'>('region')
  const [gpus, setGpus] = useState(['h100', 'b200'])
  const [a, b] = [regionById(xray.compare[0] ?? 'us-tx')!, regionById(xray.compare[1] ?? 'no-nordics')!]
  const opts = REGIONS.map((r) => ({ id: r.id, label: r.short }))
  return (
    <div className="slide-in absolute inset-x-3 bottom-[76px] z-20 rounded-lg border border-fct/30 bg-ink/95 shadow-2xl backdrop-blur">
      <div className="flex items-center justify-between border-b border-line px-4 py-2">
        <div className="flex items-center gap-3">
          <GitCompareArrows size={14} className="text-fct" />
          <Seg size="sm" value={mode} onChange={setMode} options={[{ id: 'region', label: 'Locations' }, { id: 'gpu', label: 'GPU categories' }]} />
          {mode === 'region' ? (
            <div className="flex items-center gap-2 font-mono text-[11px]">
              <span className="rounded-[2px] bg-fct px-1 text-[9px] font-bold text-ink">A</span>
              <select value={a.id} onChange={(e) => setXray({ compare: [e.target.value, b.id] })} className="rounded-[3px] border border-line2 bg-ink px-1 py-0.5">{opts.map((o) => <option key={o.id} value={o.id}>{regionById(o.id)!.name}</option>)}</select>
              <span className="rounded-[2px] bg-fct px-1 text-[9px] font-bold text-ink">B</span>
              <select value={b.id} onChange={(e) => setXray({ compare: [a.id, e.target.value] })} className="rounded-[3px] border border-line2 bg-ink px-1 py-0.5">{opts.map((o) => <option key={o.id} value={o.id}>{regionById(o.id)!.name}</option>)}</select>
              <span className="text-dim">or click the map</span>
            </div>
          ) : (
            <div className="flex items-center gap-2 font-mono text-[11px]">
              {[0, 1].map((k) => <select key={k} value={gpus[k]} onChange={(e) => setGpus((g) => g.map((x, i) => (i === k ? e.target.value : x)))} className="rounded-[3px] border border-line2 bg-ink px-1 py-0.5">{GPUS.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}</select>)}
            </div>
          )}
        </div>
        <button onClick={() => setXray({ compareOpen: false })} className="cursor-pointer text-dim hover:text-bone" aria-label="Close compare"><X size={14} /></button>
      </div>
      {mode === 'region' ? (
        <div className="grid grid-cols-[1fr_340px] gap-4 p-4">
          <TimeChart labels={MONTHS} height={150} nowIdx={NOW_IDX} legend yDomain={[20, 100]} series={[
            { id: 'a', label: `A · ${a.short}`, values: a.quality === 'none' ? [] : a.pressure.map((_, i) => pressureAt(a, i, xray.scenario)), color: 'var(--color-inf)', band: a.quality === 'none' ? undefined : { lo: a.pressure.map((_, i) => (i >= NOW_IDX ? pressureAt(a, i)! - uncAt(a, i) : null)), hi: a.pressure.map((_, i) => (i >= NOW_IDX ? pressureAt(a, i)! + uncAt(a, i) : null)), hatched: true } },
            { id: 'b', label: `B · ${b.short}`, values: b.quality === 'none' ? [] : b.pressure.map((_, i) => pressureAt(b, i, xray.scenario)), color: 'var(--color-fct)', style: 'dashed', band: b.quality === 'none' ? undefined : { lo: b.pressure.map((_, i) => (i >= NOW_IDX ? pressureAt(b, i)! - uncAt(b, i) : null)), hi: b.pressure.map((_, i) => (i >= NOW_IDX ? pressureAt(b, i)! + uncAt(b, i) : null)) } },
          ]} />
          <div className="text-[11.5px]">
            <div className="grid grid-cols-[1fr_70px_70px] gap-2 border-b border-line pb-1 text-[11px] text-dim font-medium"><span>Metric</span><span className="text-right">A {a.short}</span><span className="text-right">B {b.short}</span></div>
            {[
              ['Pressure (inf.)', pressureAt(a, NOW_IDX)?.toFixed(0) ?? '—', pressureAt(b, NOW_IDX)?.toFixed(0) ?? '—'],
              ['H100 median', a.h100 ? `$${a.h100.toFixed(2)}` : '—', b.h100 ? `$${b.h100.toFixed(2)}` : '—'],
              ['Premium', fmtPct(a.premium, 0), fmtPct(b.premium, 0)],
              ['Power $/MWh', a.power ? a.power.toFixed(0) : '—', b.power ? b.power.toFixed(0) : '—'],
              ['Operational GW', (a.opMW / 1000).toFixed(1), (b.opMW / 1000).toFixed(1)],
              ['Usable add. 27 (MW)', `${a.eff27[0]}–${a.eff27[1]}`, `${b.eff27[0]}–${b.eff27[1]}`],
              ['Coverage', a.quality, b.quality],
            ].map(([k, x, y]) => <div key={k} className="grid grid-cols-[1fr_70px_70px] gap-2 border-b border-line/50 py-1"><span className="text-mute">{k}</span><span className="num text-right">{x}</span><span className="num text-right">{y}</span></div>)}
            <div className="mt-2 text-[10.5px] text-dim">A price gap is not a risk-free arbitrage: latency, interconnect, egress and data residency differ.</div>
          </div>
        </div>
      ) : (
        <div className="p-4">
          <TimeChart labels={MONTHS.slice(0, NOW_IDX + 1)} height={150} legend yFmt={(v) => `$${v.toFixed(2)}`} series={gpus.map((g, k) => ({ id: g, label: gpuName(g), values: GPUS.find((x) => x.id === g)!.index.slice(0, NOW_IDX + 1), color: k === 0 ? 'var(--color-bone)' : 'var(--color-fct)', style: k === 1 ? 'dashed' as const : 'solid' as const }))} />
          <div className="mt-2 font-mono text-[10.5px] text-dim">Observed median on-demand $/GPU-hr · gaps = not yet launched. Inferred GPU-category pressure: {gpus.map((g) => `${gpuName(g)} ${GPUS.find((x) => x.id === g)!.pressure}`).join(' · ')}</div>
        </div>
      )}
    </div>
  )
}
const gpuName = (id: string) => GPUS.find((g) => g.id === id)!.name
/** "US East and Midwest (Ashburn hub)" -> "US East and Midwest" for sentences. */
const plainName = (name: string) => name.replace(/\s*\([^)]*\)\s*$/, '')
