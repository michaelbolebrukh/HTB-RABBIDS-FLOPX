import { ArrowRight, ArrowUpRight, ChartLine, ScanLine } from 'lucide-react'
import { ANOMALY_CARDS, AS_OF, GLOBAL_CPI, GPUS, horizonLabel, MONTHS, NOW_IDX, PRESSURE_MODEL, REGIONS, band } from '@/data/core'
import { GDH_URL, gpuCardFigures } from '@/live/gpu'
import { ENERGY } from '@/data/infra'
import { BASKETS, basketSeries, chg, NEWS, PRED, secByTicker } from '@/data/finance'
import { EQUITY, NAV0, SIGNALS } from '@/data/fund'
import { WorldMap } from '@/components/WorldMap'
import { SCALED_LABEL, TimeChart } from '@/components/charts'
import { Btn, cx, Delta, DirectionTag, Fresh, PageHeader, Panel, Spark, HorizonStatus, TruthGlyph, TruthTag } from '@/components/ui'
import { useStore } from '@/lib/store'

/** Driver phrase for a modelled reaction: the region's live cpx reading when FLOPX covers it, else the design phrase marked as an example. */
export function regionDriver(regionId: string, designPhrase: string): string {
  const r = REGIONS.find((x) => x.id === regionId)
  const p = r?.live?.pressure
  if (p) return `${r!.short} pressure ${Math.round(p.score)} (${p.band}) · modelled β`
  return `${designPhrase} (design example)`
}

export default function Overview() {
  const { navigate, setXray, setFund, nav, proposals, inspect } = useStore()
  const h100 = GPUS.find((g) => g.id === 'h100')!
  const b200 = GPUS.find((g) => g.id === 'b200')!
  // Regional sentences are composed from the backend's pressure cells once they land; until then
  // the design's briefing shows, badged illustrative.
  const ranked = REGIONS.filter((r) => r.live?.pressure).sort((a, b) => b.live!.pressure!.score - a.live!.pressure!.score)
  const live = PRESSURE_MODEL.globalReal && ranked.length >= 3
  const [hi1, hi2] = ranked
  const lo = ranked[ranked.length - 1]
  const exploratory = ranked.filter((r) => r.live!.pressure!.label === 'exploratory').length
  const supplyRegions = REGIONS.filter((r) => r.live?.supply)
  const gap = (live && supplyRegions.length ? supplyRegions : REGIONS).reduce((s, r) => s + (r.announced27 - (r.eff27[0] + r.eff27[1]) / 2), 0)
  const cpiNow = GLOBAL_CPI[NOW_IDX]
  const cpiPrev = PRESSURE_MODEL.globalPrev
  const cpiBand = band(GLOBAL_CPI, 2.4)
  const sigH100 = SIGNALS.find((s) => s.id === 'sig-h100')!
  const h100Fig = gpuCardFigures(h100)
  const b200Fig = gpuCardFigures(b200)
  const gdhCards = [h100, b200].filter((g) => g.live?.history)
  // KPI cards whose figure is observed or inferred from real inputs; the rest of the six are the design's.
  const realCards = [live && 'Compute Pressure', h100.live && 'H100 rental index', b200.live && 'B200 rental index', ENERGY[0].freshness !== 'illustrative' && 'ERCOT day-ahead'].filter((x): x is string => !!x)
  return (
    <div>
      <PageHeader
        eyebrow={<>Global intelligence · briefing · {AS_OF}{!live && <> <Fresh f="illustrative" label="Illustrative briefing" /></>}</>}
        title={live ? <>Compute pressure reads highest in <span className="text-inf italic">{plainName(hi1.name)}</span> and {plainName(hi2.name)},<br />and lowest in {plainName(lo.name)}.</> : <>Compute is tightening in <span className="text-inf italic">Texas</span> and Northern Virginia,<br />and loosening in the Nordics.</>}
        sub={live ? `${!PRESSURE_MODEL.anomaliesLive ? 'Anomaly scan not loaded yet.' : ANOMALY_CARDS.length ? `${ANOMALY_CARDS.length} open anomalies, led by ${ANOMALY_CARDS[0].title}.` : 'No open anomalies.'} FLOPX infers pressure from advertised asks, facility records, power prices and demand signals; ${exploratory} of ${ranked.length} regions are exploratory because most inputs are still sample data.` : 'Supply is arriving later than announced in US-Central while new Nordic capacity energises on schedule. H100 rental prices are diverging by region even as the global index drifts lower on Blackwell deliveries.'}
        right={<><Btn variant="primary" onClick={() => { setXray({ region: live ? hi1.id : 'us-tx', tab: 'brief' }); navigate('xray') }}><ScanLine size={14} /> Investigate {live ? plainName(hi1.name) : 'Texas'}</Btn><Btn onClick={() => navigate('fund')}><ChartLine size={14} /> Fund status</Btn></>}
      />

      <div className="mb-5 grid grid-cols-2 gap-px overflow-hidden rounded-md border border-line bg-line lg:grid-cols-3 2xl:grid-cols-6">
        {live
          ? <IndexCard label="FLOPX Compute Pressure" truth="inf" value={cpiNow.toFixed(1)} unit={`mean of ${PRESSURE_MODEL.regions} regions`} delta={cpiPrev == null ? null : cpiNow - cpiPrev} dUnit=" pts" spark={GLOBAL_CPI.slice(0, NOW_IDX + 1)} color="var(--color-inf)" onClick={() => navigate('xray')} note={`Inferred · ${PRESSURE_MODEL.method}, H100 cells, equal weight${PRESSURE_MODEL.globalScaled ? ' · spark: history scaled to today, illustrative' : ''}`} />
          : <IndexCard label="FLOPX Compute Pressure" truth="inf" value={GLOBAL_CPI[NOW_IDX].toFixed(1)} unit="± 2.4" delta={GLOBAL_CPI[NOW_IDX] - GLOBAL_CPI[NOW_IDX - 1]} dUnit=" pts" spark={GLOBAL_CPI.slice(0, NOW_IDX + 1)} color="var(--color-inf)" onClick={() => navigate('xray')} note="Inferred · global, MW-weighted" />}
        <IndexCard label="H100 rental index" truth="obs" value={h100.live ? `$${h100.live.current.toFixed(2)}` : `$${h100.index[NOW_IDX].toFixed(2)}`} unit="/GPU-hr" delta={h100Fig.delta} spark={h100.index.slice(0, NOW_IDX + 1)} onClick={() => inspect({ kind: 'gpu', id: 'h100', label: 'H100 SXM' })} note={h100Fig.note} />
        <IndexCard label="B200 rental index" truth="obs" value={b200.live ? `$${b200.live.current.toFixed(2)}` : `$${b200.index[NOW_IDX].toFixed(2)}`} unit="/GPU-hr" delta={b200Fig.delta} spark={b200.index.slice(15, NOW_IDX + 1)} onClick={() => inspect({ kind: 'gpu', id: 'b200', label: 'B200' })} note={b200Fig.note} />
        <IndexCard label="Effective capacity gap" truth="inf" value={`${(gap / 1000).toFixed(1)} GW`} unit={live && supplyRegions.length ? `${supplyRegions.length} regions` : `by ${horizonLabel(12)}`} delta={live && supplyRegions.length ? null : 0.6} dUnit=" GW" invert spark={live && supplyRegions.length ? [gap / 1000, gap / 1000] : [8.1, 8.4, 8.2, 8.9, 9.3, 9.8, 10.4]} color="var(--color-inf)" onClick={() => { setXray({ layers: ['effective', 'pipeline'], reveal: 100 }); navigate('xray') }} note={live && supplyRegions.length ? 'Pipeline − est. usable · sample records' : 'Announced − est. usable'} />
        <IndexCard label="ERCOT day-ahead" truth="obs" value={`$${ENERGY[0].priceNow.toFixed(0)}`} unit="/MWh 30d" delta={ENERGY[0].chg30} invert spark={ENERGY[0].price} color="var(--color-warn)" onClick={() => inspect({ kind: 'energy', id: 'ercot', label: 'ERCOT' })} note={ENERGY[0].freshness === 'illustrative' ? 'Illustrative · FLOPX has no real ERCOT series' : `Observed · ${ENERGY[0].freshness}`} />
        <IndexCard label="CPX-1 paper NAV" truth="dec" value={`$${(nav / 1e6).toFixed(2)}M`} unit="" delta={((nav - NAV0) / NAV0) * 100} dUnit="%" spark={EQUITY.slice(-12)} color="var(--color-dec)" onClick={() => navigate('fund')} note={`Paper · ${proposals.filter((p) => p.state === 'open').length} proposals pending`} />
      </div>
      {/* One note for the six cards: the cards that are real are named here, and each card's own foot line says where its figure comes from. */}
      <div className="-mt-3.5 mb-1.5 font-mono text-[9.5px] text-dim">
        <Fresh quiet f="illustrative" label={realCards.length === 0 ? 'Illustrative' : `Illustrative, except ${realCards.join(', ')} (FLOPX data; each card's foot line names its source)`} />
      </div>
      {gdhCards.length > 0 && (
        <div className="mb-5 font-mono text-[9.5px] text-dim">
          {gdhCards.map((g) => g.name).join(' and ')} spark{gdhCards.length === 1 ? '' : 's'}: <a href={GDH_URL} target="_blank" rel="noreferrer" className="underline decoration-dotted underline-offset-2 hover:text-mute">{gdhCards[0].live!.history!.attribution}</a> · weekly medians of advertised on-demand asks as monthly means, {gdhCards[0].live!.history!.first} to {gdhCards[0].live!.history!.last}, archived. The 30d change is GetDeploying's where the FLOPX index is younger than 30 days.
        </div>
      )}

      <div className="mb-5 grid grid-cols-1 gap-5 xl:grid-cols-[1.6fr_1fr]">
        <Panel
          title="Inferred compute pressure"
          eyebrow={<><TruthGlyph truth="inf" size={7} /> Latest · {MONTHS[NOW_IDX]} <Fresh quiet f="illustrative" label={live ? (PRESSURE_MODEL.globalScaled ? SCALED_LABEL : `History before ${MONTHS[NOW_IDX]} and forecast illustrative`) : 'Illustrative'} /></>}
          right={<Btn size="sm" variant="ghost" onClick={() => navigate('xray')}>Open X-Ray <ArrowUpRight size={12} /></Btn>}
          bodyClass="p-0"
        >
          <div className="h-[330px]">
            <WorldMap compact callouts layers={['pressure']} onRegion={(id) => { setXray({ region: id, tab: 'brief' }); navigate('xray') }} minHeight={330} />
          </div>
          <div className="border-t border-line px-4 py-3">
            <TimeChart labels={MONTHS} height={110} nowIdx={NOW_IDX} yFmt={(v) => v.toFixed(0)} series={[{ id: 'cpi', label: 'Global CPI', values: GLOBAL_CPI.map((v, i) => (i <= NOW_IDX ? v : null)), color: 'var(--color-inf)' }, { id: 'fc', label: 'Forecast', values: GLOBAL_CPI.map((v, i) => (i >= NOW_IDX ? v : null)), color: 'var(--color-fct)', style: 'dashed', band: cpiBand }]} />
          </div>
        </Panel>

        <Panel title="What changed" eyebrow={<>Last 7 days · ranked by estimated impact <Fresh quiet f="illustrative" label="Illustrative, except rows marked FLOPX" /></>} bodyClass="p-0">
          {[
            PRESSURE_MODEL.anomaliesLive && ANOMALY_CARDS[0]
              ? { t: 'inf' as const, title: ANOMALY_CARDS[0].title, body: ANOMALY_CARDS[0].sub, onClick: () => { setXray({ region: ANOMALY_CARDS[0].region, tab: 'brief' }); navigate('xray') }, tag: 'Anomaly', real: true }
              : { t: 'inf' as const, title: 'Texas pressure +14 pts in 6 weeks', body: '600 MW energisation slipped to Q4; H100 premium +17%.', onClick: () => { setXray({ region: 'us-tx', tab: 'brief' }); navigate('xray') }, tag: 'Anomaly', real: false },
            { t: 'obs' as const, title: 'ERCOT opens large-load consultation', body: 'Curtailment requirement for new loads > 75 MW.', onClick: () => navigate('news', { id: 'n3' }), tag: 'Confirmed', real: false },
            { t: 'obs' as const, title: 'EU H100 spot below $2/hr on marketplaces', body: 'Mostly Nordic and community hosts.', onClick: () => navigate('markets', { gpu: 'h100' }), tag: 'Observed', real: false },
            { t: 'dec' as const, title: 'CPX-1 proposes VST +1.5 pp, CRWV −2.0 pp', body: 'Awaiting your approval.', onClick: () => { setFund({ tab: 'decisions' }); navigate('fund') }, tag: 'Decision', real: false },
            // The forecast line reads off the H100 index signal, whose status comes from the backend's own
            // forecast call once it answers (src/live/adapt-w2.ts applyForecastValidation); today a refusal.
            sigH100.basis === 'backend'
              ? { t: 'fct' as const, title: sigH100.horizons['1M'].status === 'insufficient' ? 'H100 index 1M forecast: not issued' : `H100 index 1M forecast: ${sigH100.horizons['1M'].status}`, body: `${sigH100.basisNote ?? ''}${sigH100.horizons['1M'].status === 'insufficient' ? ' · insufficient history, nothing validated' : ''}`, onClick: () => { setFund({ tab: 'signals', signal: 'sig-h100' }); navigate('fund') }, tag: 'Forecast', real: true }
              // Before the forecast call answers (or when it times out) the honest state: nothing issued, nothing validated.
              : { t: 'fct' as const, title: 'H100 index 1M forecast: not issued', body: 'Awaiting the FLOPX forecast call · nothing validated', onClick: () => { setFund({ tab: 'signals', signal: 'sig-h100' }); navigate('fund') }, tag: 'Forecast', real: true },
            live
              ? { t: 'obs' as const, title: `${exploratory} of ${ranked.length} FLOPX regions read exploratory`, body: 'Fewer than three pressure factors are backed by real inputs; most inputs are still sample data.', onClick: () => navigate('xray'), tag: 'Data issue', real: true }
              : { t: 'obs' as const, title: 'Sydney rental feed stale since 06 Mar', body: 'Provider API change; regional estimate widened.', onClick: () => { setXray({ region: 'au-syd' }); navigate('xray') }, tag: 'Data issue', real: false },
          ].map((x, i) => (
            <button key={i} onClick={x.onClick} className="group grid w-full cursor-pointer grid-cols-[14px_1fr_auto] items-start gap-3 border-b border-line/70 px-4 py-3 text-left last:border-0 hover:bg-hover">
              <span className="mt-1"><TruthGlyph truth={x.t} size={8} /></span>
              <span><span className="block text-[12.5px] font-medium text-bone">{x.title}</span><span className="block text-[11.5px] text-mute">{x.body}</span></span>
              <span className="flex items-center gap-1 text-[11px] text-dim font-medium capitalize">{x.tag}{x.real && <span className="font-mono text-[9.5px] font-normal normal-case text-mute" title="Read from FLOPX, not the design">· FLOPX</span>}<ArrowRight size={11} className="opacity-0 transition-opacity group-hover:opacity-100" /></span>
            </button>
          ))}
        </Panel>
      </div>

      <div className="mb-5 grid grid-cols-1 gap-5 xl:grid-cols-3">
        <Panel title="Top evidence-backed signals" eyebrow={<>Status shown per horizon — no single ‘AI confidence’ <Fresh quiet f="illustrative" /></>} right={<Btn size="sm" variant="ghost" onClick={() => { setFund({ tab: 'signals' }); navigate('fund') }}>All</Btn>} bodyClass="p-0">
          {SIGNALS.slice(0, 4).map((s) => (
            <button key={s.id} onClick={() => { setFund({ tab: 'signals', signal: s.id }); navigate('fund') }} className="w-full cursor-pointer border-b border-line/70 px-4 py-3 text-left last:border-0 hover:bg-hover">
              <div className="flex items-center justify-between"><span className="text-[12.5px] font-medium">{s.name}</span><DirectionTag direction={s.direction} asset={s.asset.startsWith('b-') ? 'basket' : s.asset} /></div>
              <HorizonStatus horizons={s.horizons} className="mt-2" />
            </button>
          ))}
        </Panel>
        <Panel title="Financial reactions" eyebrow={<>1-week move · linked driver is an estimated association <Fresh quiet f="illustrative" /></>} bodyClass="p-0">
          <ReactionGroup title="Stocks" rows={[
            // Modelled drivers name the region's live reading when FLOPX covers it, so the phrase never contradicts the X-Ray table.
            { id: 'VST', why: regionDriver('us-tx', 'TX pressure ↑'), link: 'modelled' },
            { id: 'APLD', why: 'Red Plains slip (design example)', link: 'documented' },
            { id: 'CRWV', why: 'Debt facility; rental mix (design example)', link: 'documented' },
            { id: 'NBIS', why: regionDriver('no-nordics', 'Nordic loosening'), link: 'modelled' },
            { id: 'DLR', why: 'NoVA pre-leasing', link: 'documented' },
            { id: 'NVDA', why: 'No compute link found', link: 'none' },
          ].map((r) => ({ ...r, link: r.link as Reaction['link'], kind: 'security' as const, px: secByTicker(r.id)!.px }))} />
          <ReactionGroup title="Indexes" rows={['SOX', 'SPX'].map((t) => ({ id: t, kind: 'security' as const, px: secByTicker(t)!.px, why: secByTicker(t)!.name, link: 'none' as const }))} />
          <ReactionGroup title="Baskets" wide rows={BASKETS.map((b) => ({ id: b.id, label: b.name, kind: 'basket' as const, px: basketSeries(b), why: b.members.map((m) => m.t).join(' · '), link: 'none' as const }))} />
        </Panel>
        <Panel title="Intelligence events" eyebrow="News · projects · prediction markets" right={<Btn size="sm" variant="ghost" onClick={() => navigate('news')}>All</Btn>} bodyClass="p-0">
          {NEWS.slice(0, 4).map((n) => (
            <button key={n.id} onClick={() => navigate('news', { id: n.id })} className="w-full cursor-pointer border-b border-line/70 px-4 py-2.5 text-left hover:bg-hover">
              <div className="flex items-center gap-2 text-[11px] text-dim font-medium"><span>{n.type}</span><span>· {n.ago} ago</span><span className={cx(n.verification === 'Single source' && 'text-warn')}>· {n.verification}</span></div>
              <div className="mt-0.5 text-[12px] text-bone">{n.title}</div>
            </button>
          ))}
          <button onClick={() => navigate('news', { id: 'pm1' })} className="w-full cursor-pointer px-4 py-2.5 text-left hover:bg-hover">
            <div className="flex items-center gap-2 text-[11px] text-dim font-medium"><span className="text-fct">Prediction market</span><span>· {PRED[0].venue}</span></div>
            <div className="mt-0.5 flex items-center justify-between text-[12px]"><span>{PRED[0].question}</span><span className="num ml-3 text-fct">{Math.round(PRED[0].prob * 100)}%</span></div>
          </button>
        </Panel>
      </div>

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
        <Pathway
          tone="inf"
          eyebrow="Flagship · G-Research"
          title="Compute Economy X-Ray"
          body="Reveal the hidden pressure behind global compute: where announced supply isn't becoming usable capacity, why, and who is exposed."
          stats={[['Regions', live ? `${ranked.length} of ${REGIONS.length} live` : '17'], ['Projects', '12'], ['Anomalies', PRESSURE_MODEL.anomaliesLive ? `${ANOMALY_CARDS.length} open` : 'not loaded']]}
          onClick={() => navigate('xray')}
        />
        <Pathway
          tone="dec"
          eyebrow="Flagship · HRT"
          title="Systematic Compute Fund"
          body="Follow compute-market information through forecasts, validation and position changes to paper orders and attribution."
          stats={[['Signals', `${SIGNALS.length} (${SIGNALS.filter((s) => s.horizons['1M'].status === 'validated').length} validated @1M${SIGNALS.every((s) => s.basis === 'backend') ? '' : SIGNALS.some((s) => s.basis === 'pending') ? ', awaiting evaluation' : ', illustrative'})`], ['Proposals', `${proposals.filter((p) => p.state === 'open').length} open`], ['Mode', 'Paper']]}
          onClick={() => navigate('fund')}
        />
      </div>
    </div>
  )
}

function IndexCard({ label, truth, value, unit, delta, dUnit = '%', spark, color, onClick, note, invert }: { label: string; truth: 'obs' | 'inf' | 'dec'; value: string; unit: string; delta: number | null; dUnit?: string; spark: number[]; color?: string; onClick: () => void; note: string; invert?: boolean }) {
  return (
    <button onClick={onClick} className={cx('group cursor-pointer bg-panel p-4 text-left transition-colors hover:bg-raise', truth === 'inf' && 'bg-[linear-gradient(180deg,color-mix(in_oklab,var(--color-inf)_5%,transparent),transparent)]')}>
      <div className="mb-2 flex items-center justify-between"><span className="flex items-center gap-1.5"><TruthTag truth={truth} compact /></span><ArrowUpRight size={12} className="text-dim opacity-0 transition-opacity group-hover:opacity-100" /></div>
      <div className="text-[11.5px] text-mute">{label}</div>
      <div className="mt-1 flex items-baseline gap-1.5"><span className="num text-[22px] leading-none text-bone">{value}</span><span className="font-mono text-[10px] text-dim">{unit}</span></div>
      <div className="mt-2 flex items-end justify-between gap-2">
        {delta == null ? <span className="font-mono text-[11px] text-dim">no prior reading</span> : <Delta v={delta} d={dUnit === '%' ? 2 : 1} suffix={dUnit} className="text-[11px]" invert={invert} />}
        <Spark values={spark} width={72} height={20} color={color} />
      </div>
      <div className="mt-1.5 font-mono text-[9.5px] text-dim">{note}</div>
    </button>
  )
}

function Pathway({ tone, eyebrow, title, body, stats, onClick }: { tone: 'inf' | 'dec'; eyebrow: string; title: string; body: string; stats: [string, string][]; onClick: () => void }) {
  return (
    <button onClick={onClick} className={cx('group relative cursor-pointer overflow-hidden rounded-md border p-6 text-left transition-colors', tone === 'inf' ? 'border-inf/25 hover:border-inf/50' : 'border-dec/25 hover:border-dec/50')}>
      <div className={cx('absolute inset-0 opacity-60', tone === 'inf' ? 'hatch-inf' : '')} style={tone === 'dec' ? { backgroundImage: 'repeating-linear-gradient(45deg, color-mix(in oklab, var(--color-dec) 8%, transparent) 0 1px, transparent 1px 7px)' } : { opacity: 0.12 }} />
      <div className="relative">
        <div className={cx('eyebrow mb-2', tone === 'inf' ? '!text-inf' : '!text-dec')}>{eyebrow}</div>
        <div className="flex items-center justify-between"><h3 className="font-serif text-[30px] leading-none">{title}</h3><ArrowRight className="text-mute transition-transform group-hover:translate-x-1" /></div>
        <p className="mt-2 max-w-md text-[12.5px] leading-relaxed text-mute">{body}</p>
        <div className="mt-4 flex gap-6">{stats.map(([k, v]) => <div key={k}><div className="text-[11px] text-dim font-medium">{k}</div><div className="num text-[13px]">{v}</div></div>)}</div>
      </div>
    </button>
  )
}

type Reaction = { id: string; label?: string; kind: 'security' | 'basket'; px: number[]; why: string; link: 'documented' | 'modelled' | 'none' }

/** One asset class in Financial reactions: a labelled sub-header, then a row per instrument. */
function ReactionGroup({ title, rows, wide }: { title: string; rows: Reaction[]; wide?: boolean }) {
  const { inspect } = useStore()
  return (
    <div className="border-b border-line last:border-0">
      <div className="flex items-center justify-between bg-raise/60 px-4 py-1.5 text-[11px] text-dim font-medium">
        <span>{title}</span>
        <span>{rows.length}</span>
      </div>
      {rows.map((r) => (
        <button key={r.id} onClick={() => inspect({ kind: r.kind, id: r.id, label: r.label ?? r.id })} className="grid w-full cursor-pointer items-center gap-3 border-t border-line/70 px-4 py-2 text-left first-of-type:border-0 hover:bg-hover" style={{ gridTemplateColumns: `${wide ? 170 : 56}px 64px 1fr auto` }}>
          <span className={cx('truncate text-[12px]', wide ? 'text-bone' : 'font-mono')}>
            {r.label ?? r.id}
            {wide && <span className="block truncate font-mono text-[10px] text-dim">{r.why}</span>}
          </span>
          <Spark values={r.px.slice(-12)} width={64} height={16} />
          <span className={cx('flex items-center gap-1.5 truncate text-[11.5px] text-mute', wide && 'invisible')}><span className={cx('inline-block w-3 shrink-0', r.link === 'documented' ? 'h-px bg-bone' : r.link === 'modelled' ? 'border-t border-dashed border-inf' : 'h-px bg-transparent')} />{r.why}</span>
          <Delta v={chg(r.px)} className="text-[12px]" />
        </button>
      ))}
    </div>
  )
}

/** "US East and Midwest (Ashburn hub)" -> "US East and Midwest" for sentences. */
const plainName = (name: string) => name.replace(/\s*\([^)]*\)\s*$/, '')
