import { useMemo, useState } from 'react'
import { ArrowRight } from 'lucide-react'
import { AS_OF, band, fmtReliability, GPUS, MONTHS, NOW_IDX, OFFERS, PROVIDERS, providerById, REGIONS, regionById, type Offer } from '@/data/core'
import { GDH_URL, offerRegionName, offerRegionShort } from '@/live/gpu'
import { SIGNALS } from '@/data/fund'
import { beeswarm, FCPI_MARKS_LABEL, GDH_BADGE_LABEL, SCALED_SHORT_LABEL, TimeChart, useWidth } from '@/components/charts'
import { Row } from '@/components/collapse'
import { Btn, cx, dominantFresh, Empty, Fn, Fresh, freshLabel, Notes, PageHeader, Panel, Seg, TruthGlyph, TruthTag, Validation } from '@/components/ui'
import { BREV, derived, GPU_FV, SNAPSHOT } from '@/data/handoff'
import { useStore } from '@/lib/store'

const NOTES = [
  'Spreads reflect differences in latency, interconnect, egress, data residency and provider mix. They are not risk-free arbitrage. * = stale feed.',
  'Quotes are list prices observed from provider APIs and marketplaces, not transaction prices.',
  'Ask = fair value + residual. Routing never reads the residual; it buys the cheapest ask that fits.',
]
const PROVIDER_TYPES = ['Hyperscaler', 'Neocloud', 'Marketplace'] as const

const median = (a: number[]) => { const s = [...a].sort((x, y) => x - y); return s.length ? (s.length % 2 ? s[(s.length - 1) / 2] : (s[s.length / 2 - 1] + s[s.length / 2]) / 2) : 0 }

export default function Markets() {
  const { route, inspect, navigate, live } = useStore()
  const [gpu, setGpu] = useState(route.params.gpu ?? 'h100')
  const [contract, setContract] = useState<'all' | Offer['contract']>('all')
  const [ic, setIc] = useState<'all' | 'ib'>('all')
  const [provider, setProvider] = useState(route.params.provider ?? 'all')
  const [region, setRegion] = useState('all')
  const [hz, setHz] = useState<'1W' | '1M' | '3M'>('1M')
  const g = GPUS.find((x) => x.id === gpu)!
  const offers = useMemo(() => OFFERS.filter((o) => o.gpu === gpu && (contract === 'all' || o.contract === contract) && (ic === 'all' || o.interconnect === 'InfiniBand 3.2T') && (provider === 'all' || o.provider === provider) && (region === 'all' || o.region === region)).sort((a, b) => a.price - b.price), [gpu, contract, ic, provider, region, live])
  const od = OFFERS.filter((o) => o.gpu === gpu && o.contract === 'On-demand')
  const globalMed = median(od.map((o) => o.price))
  // Regions come from the offers themselves: live offers carry backend region ids and names.
  const byRegion = [...new Map(od.map((o) => [o.region, o])).values()].map((o0) => ({ r: { ...regionById(o0.region)!, id: o0.region, name: offerRegionName(o0) }, known: REGIONS.some((x) => x.id === o0.region), prices: od.filter((o) => o.region === o0.region).map((o) => o.price) })).map((x) => ({ ...x, med: median(x.prices) })).sort((a, b) => b.med - a.med)
  const gl = g.live
  const odFresh = od.some((o) => o.freshness === 'illustrative')
  // G14: g.index is the design's curve with the real months in their slots and the illustrative
  // months (history and forecast) multiplied to join them, so the forecast path starts at the real
  // Now; its band scales by the same factor (src/live/README.md).
  const fcBase = g.index
  const sig = SIGNALS.find((s) => s.id === 'sig-h100')!
  const sigLive = sig.basis === 'backend'
  const fcMonths = hz === '3M' ? 3 : 1
  const idx = g.index.map((v) => (Number.isNaN(v) ? null : v))
  const b = band(fcBase, g.unc * 0.6 * (gl?.bandFactor ?? 1))
  const fcVals = fcBase.map((v) => (Number.isNaN(v) ? null : v)).map((v, i) => (i >= NOW_IDX && i <= NOW_IDX + fcMonths ? v : null))
  const firstReal = gl ? gl.real.indexOf(true) : -1
  // N-GDH: where the backend archives GetDeploying's weekly medians for this model, the observed
  // line is those weeks as monthly means (real, archived, CC BY 4.0) and the FLOPX index's own days
  // are drawn as separate marks; the design forecast joins the line's present as before.
  const hist = gl?.history
  const lastWeek = hist ? hist.weekly[hist.weekly.length - 1] : null
  // The offers table carries its status once, in the eyebrow; only a row that differs from the rest keeps a chip.
  const offersFresh = dominantFresh(offers.map((o) => o.freshness))
  // SLA and egress are terms of the provider type: the line above the table reads them off the listed offers.
  const terms = PROVIDER_TYPES.map((t) => ({ t, o: offers.find((o) => providerById(o.provider)?.type === t) })).filter((x) => x.o)

  return (
    <div className="max-md:[&_[data-fresh]]:whitespace-normal">
      <PageHeader
        eyebrow={<span title={`Provider APIs and marketplaces · as of ${AS_OF}`}>Provider APIs and marketplaces</span>}
        title="GPU rental markets"
        sub="Quoted GPU prices by hardware, provider, region and contract."
        right={<Seg className="max-w-full overflow-x-auto" value={gpu} onChange={setGpu} options={GPUS.map((x) => ({ id: x.id, label: x.name }))} />}
      />

      <Row cols={[1.4, 1]} className="mb-5">
        <Panel title={`${g.name} rental index`} id="Rental index" eyebrow={<><TruthGlyph truth="obs" size={7} />Median on-demand $/GPU-hr · monthly</>} right={<div className="flex items-center gap-2">{gl ? <><Fresh quiet f={gl.freshness} label={gl.freshLabel} />{gl.scaled && <Fresh quiet f="illustrative" label={SCALED_SHORT_LABEL} />}</> : <><Fresh quiet f="illustrative" />{gpu === 'h100' && <Validation s={sig.horizons[hz].status} />}</>}<Seg size="sm" value={hz} onChange={setHz} options={[{ id: '1W', label: '1W' }, { id: '1M', label: '1M' }, { id: '3M', label: '3M' }]} /></div>}>
          <TimeChart
            labels={MONTHS.slice(0, NOW_IDX + 4)}
            height={230}
            nowIdx={NOW_IDX}
            legend
            yFmt={(v) => `$${v.toFixed(2)}`}
            series={[
              ...(hist && gl?.indexMarks ? [{ id: 'fcpi', label: FCPI_MARKS_LABEL, values: gl.indexMarks.slice(0, NOW_IDX + 1), color: 'var(--color-accent)', points: true, fmt: (v: number) => `$${v.toFixed(2)}` }] : []),
              { id: 'obs', label: hist ? 'GetDeploying weekly median, monthly mean' : 'Observed index', values: idx.slice(0, NOW_IDX + 1), color: 'var(--color-obs)', width: 2 },
              { id: 'fc', label: `${hz} cost forecast`, values: fcVals.slice(0, NOW_IDX + 4), color: 'var(--color-fct)', style: 'dashed', band: { lo: b.lo.slice(0, NOW_IDX + 4).map((v, i) => (i <= NOW_IDX + fcMonths ? v : null)), hi: b.hi.slice(0, NOW_IDX + 4).map((v, i) => (i <= NOW_IDX + fcMonths ? v : null)) } },
            ]}
          />
          <div className="mt-2 flex flex-wrap gap-4 text-[11px] text-mute">
            <span title={gl ? `FCPI index: median of ${gl.coverage.offerCount} comparable asks (US-normalised, ${gl.coverage.providers} provider${gl.coverage.providers === 1 ? '' : 's'}, ${gl.coverage.regions} region${gl.coverage.regions === 1 ? '' : 's'}). Vast.ai asks are shown in the distribution below but never enter the index (licence). ${gl.limitations.join('; ')}` : 'Illustrative design value'}>Now <span className="num text-bone">${(gl ? gl.current : g.index[NOW_IDX]).toFixed(2)}</span>{gl && <span className="text-dim"> · {gl.observed} UTC · index over {gl.coverage.offerCount} comparable ask{gl.coverage.offerCount === 1 ? '' : 's'}; {OFFERS.filter((o) => o.gpu === gpu).length} offers listed; Vast asks shown, never in the index</span>}</span>
            {hist && <span className="flex items-center"><Fresh quiet f="historical" label={GDH_BADGE_LABEL} /></span>}
            {hist && lastWeek && <span title={`GetDeploying's weekly median of advertised on-demand asks for the ${g.name} family (SXM and PCIe boards pooled), ${hist.weeks} weeks from ${hist.first} to ${hist.last}, each stamped at its week's end; the line shows their monthly means. Archived by FLOPX, CC BY 4.0.`}>GetDeploying week ending {hist.last} <span className="text-bone">${lastWeek.value.toFixed(2)}</span><span className="text-dim"> · weekly median, archived</span></span>}
            <span title={gl ? (sigLive && gpu === 'h100' ? sig.basisNote : 'Illustrative: the FLOPX forecast needs at least twice the horizon in index history') : undefined}>{hz} forecast <span className="num text-fct">{gpu === 'h100' && sigLive && sig.horizons[hz].status === 'insufficient' ? 'not issued (insufficient history)' : gpu === 'h100' ? `${sig.horizons[hz].mean > 0 ? '+' : ''}${sig.horizons[hz].mean}% [${sig.horizons[hz].p10}, ${sig.horizons[hz].p90}]` : `$${fcBase[NOW_IDX + fcMonths].toFixed(2)} (exploratory)`}</span></span>
            {gl && <span className="text-dim">{hist ? `${MONTHS[firstReal]} to ${MONTHS[NOW_IDX]} real (GetDeploying, archived); ` : ''}{gl.scaled ? `${firstReal > 0 ? `history before ${MONTHS[firstReal]} and ` : ''}forecast scaled to today's reading, illustrative` : `${firstReal > 0 ? `Before ${MONTHS[firstReal]} illustrative; ` : ''}forecast${gpu === 'h100' ? ' and its validation' : ''} illustrative`}</span>}
            <span title={g.pressureLive ? `Median of ${g.pressureLive.cells} regional ${g.pressureLive.method} scores, ${g.pressureLive.asOf} UTC` : 'Illustrative design value'}>Inferred category pressure <span className="num text-inf">{g.pressure}</span>{!g.pressureLive && <span className="text-dim"> (illustrative)</span>}</span>
            {gpu !== 'h100' && <span className="text-warn">Forecasts for {g.name} are not yet validated</span>}
          </div>
          {hist && (
            <div className="mt-1.5 font-mono text-[10px] text-dim">
              History: <a href={GDH_URL} target="_blank" rel="noreferrer" className="underline decoration-dotted underline-offset-2 hover:text-mute">{hist.attribution}</a> · {hist.weeks} weekly medians of advertised on-demand asks, {hist.first} to {hist.last}, each stamped at its week's end and averaged by month here; GetDeploying pools the {g.name} family's boards. FLOPX index days are the marks.
            </div>
          )}
        </Panel>
        <Panel title="Regional spread" eyebrow={<>{`On-demand median by region vs global $${globalMed.toFixed(2)}`}{' '}<Fn n={1} note={NOTES[0]} />{odFresh && <> <Fresh quiet f="illustrative" /></>}</>}>
          <div className="space-y-1.5">
            {byRegion.map(({ r, med, prices, known }) => {
              const prem = ((med - globalMed) / globalMed) * 100
              return (
                <button key={r.id} onClick={() => known && inspect({ kind: 'region', id: r.id, label: r.name })} title={r.name} className="grid w-full cursor-pointer grid-cols-[96px_1fr_56px_48px] items-center gap-2 text-left hover:bg-hover max-md:min-h-11 max-md:grid-cols-[96px_1fr_58px_64px] max-md:[&_.num]:text-[12px] max-md:[&>span:first-child]:text-[12.5px]">
                  <span className="truncate text-[11.5px] text-mute">{r.name.split(' (')[0]}{r.quality === 'stale' && <span className="ml-1 text-warn">*</span>}</span>
                  <span className="relative h-3">
                    <span className="absolute inset-y-0 left-1/2 w-px bg-line2" />
                    <span className={cx('absolute inset-y-0.5 rounded-sm', prem >= 0 ? 'bg-down/70' : 'bg-up/70')} style={{ left: prem >= 0 ? '50%' : `${50 + Math.max(-50, prem * 1.2)}%`, width: `${Math.min(50, Math.abs(prem) * 1.2)}%` }} />
                  </span>
                  <span className="num text-right text-[11.5px]">${med.toFixed(2)}</span>
                  <span className={cx('num text-right text-[10.5px]', prem > 0 ? 'text-down' : 'text-up')}>{prem > 0 ? '+' : ''}{prem.toFixed(0)}%<span className="ml-0.5 text-dim">·{prices.length}</span></span>
                </button>
              )
            })}
          </div>
        </Panel>
      </Row>

      <Row cols={[1, '380px']} className="mb-5 items-start">
        <Panel title="Price distribution" eyebrow="Every observed offer · by contract type" right={<span className="flex items-center gap-2 text-[11px] text-dim">{OFFERS.some((o) => o.gpu === gpu && o.freshness === 'illustrative') && <Fresh quiet f="illustrative" />}<span><span className="num">{OFFERS.filter((o) => o.gpu === gpu).length}</span> offers · $/GPU-hr</span></span>}>
          <Strip offers={OFFERS.filter((o) => o.gpu === gpu)} onPick={(o) => inspect({ kind: 'provider', id: o.provider, label: providerById(o.provider)!.name })} />
        </Panel>
        <WorkloadBox />
      </Row>

      <Panel className="mb-5" id="Fair value" title="Fair value and venue comparison" right={<span className="flex items-center gap-2 text-[11px] text-dim">Hedonic fit and Brev scrape · <Fresh quiet f="historical" label={SNAPSHOT.short} /></span>} bodyClass="p-0">
        <div className="grid grid-cols-1 divide-line-soft max-lg:divide-y lg:grid-cols-3 lg:divide-x">
          <div className="p-4">
            <div className="mb-2 text-[12.5px] font-semibold text-bone">Fair value <span className="font-normal text-dim">· {GPU_FV.asOf}</span></div>
            <div className="num text-[22px] leading-tight">{GPU_FV.asks.toLocaleString()} <span className="font-sans text-[12px] text-mute">asks</span></div>
            <div className="mt-3 text-[11px] text-dim">Share of log-price variance</div>
            <div className="mt-1 flex h-3 w-full overflow-hidden rounded-sm" role="img" aria-label={`Fair value explains r² ${GPU_FV.r2} of log price`} title={`Ridge on ${GPU_FV.features}`}>
              <span className="bg-cat1" style={{ width: `${GPU_FV.r2 * 100}%` }} title={`Fair value, r² ${GPU_FV.r2}`} />
              <span className="bg-cat3" style={{ width: `${(1 - GPU_FV.r2) * 100}%` }} title="Unexplained: region, mispricing and noise" />
            </div>
            <div className="mt-1.5 flex justify-between text-[11px] text-mute">
              <span className="flex items-center gap-1.5"><span className="size-2 rounded-full bg-cat1" />Explained by specs <span className="num text-bone">r² {GPU_FV.r2}</span></span>
              <span className="flex items-center gap-1.5"><span className="size-2 rounded-full bg-cat3" />Unexplained <span className="num text-bone">{(1 - GPU_FV.r2).toFixed(2)}</span></span>
            </div>
            <div className="mt-3 text-[11px] text-dim" title="An ask's residual is its mispricing">Ask = fair value + residual<Fn n={3} note={NOTES[2]} /></div>
          </div>
          <div className="p-4">
            <div className="mb-2 text-[12.5px] font-semibold text-bone">Same GPU, different place <span className="font-normal text-dim">· {GPU_FV.example.gpu}</span></div>
            <div className="space-y-2">
              {[GPU_FV.example.low, GPU_FV.example.high].map((p, i) => (
                <div key={p.place} className="grid grid-cols-[64px_1fr_72px] items-center gap-2 text-[11px]">
                  <span className="text-mute">{p.place}</span>
                  <div className="h-2 rounded-sm bg-line/60"><div className="h-full rounded-sm bg-mute" style={{ width: `${(p.price / GPU_FV.example.high.price) * 100}%` }} title={`${p.place} $${p.price.toFixed(2)}`} /></div>
                  <span className="num text-right text-bone">${p.price.toFixed(2)}{i === 1 && <span className="ml-1 text-warn">+{(derived.sameGpuPremium * 100).toFixed(0)}%</span>}</span>
                </div>
              ))}
            </div>
            <div className="mt-4 flex h-3 w-full overflow-hidden rounded-sm" role="img" aria-label={`${GPU_FV.example.gapLogPts} log-point gap: ${GPU_FV.example.specsLogPts} from specs, ${derived.regionLogPts.toFixed(1)} from region and mispricing`}>
              <span className="bg-cat1" style={{ width: `${(GPU_FV.example.specsLogPts / GPU_FV.example.gapLogPts) * 100}%` }} />
              <span className="bg-cat3" style={{ width: `${(derived.regionLogPts / GPU_FV.example.gapLogPts) * 100}%` }} />
            </div>
            <div className="mt-1.5 flex justify-between text-[11px] text-mute">
              <span className="flex items-center gap-1.5"><span className="size-2 rounded-full bg-cat1" />Specs <span className="num text-bone">{GPU_FV.example.specsLogPts}</span></span>
              <span className="flex items-center gap-1.5"><span className="size-2 rounded-full bg-cat3" />Region + mispricing <span className="num text-bone">{derived.regionLogPts.toFixed(1)}</span></span>
            </div>
            <div className="num mt-1 text-[10.5px] text-dim">{GPU_FV.example.gapLogPts} log points total</div>
          </div>
          <div className="p-4">
            <div className="mb-2 text-[12.5px] font-semibold text-bone">Second venue: Brev <span className="font-normal text-dim">· {BREV.clouds} clouds</span></div>
            <div className="text-[11px] text-dim" title={`Clouds: ${BREV.examples.join(', ')}`}>Cheapest H200 SXM, $/GPU-hr</div>
            <div className="mt-2 space-y-2">
              {([['Vast', BREV.h200sxm.vast], ['Brev', BREV.h200sxm.brev]] as const).map(([k, v]) => (
                <div key={k} className="grid grid-cols-[40px_1fr_72px] items-center gap-2 text-[11px]">
                  <span className="text-mute">{k}</span>
                  <div className="h-2 rounded-sm bg-line/60"><div className="h-full rounded-sm bg-mute" style={{ width: `${(v / BREV.h200sxm.brev) * 100}%` }} title={`${k} $${v.toFixed(2)}`} /></div>
                  <span className="num text-right text-bone">${v.toFixed(2)}{k === 'Brev' && <span className="ml-1 text-warn">+{(derived.brevPremium * 100).toFixed(0)}%</span>}</span>
                </div>
              ))}
            </div>
            <div className="mt-3 text-[11px] text-dim"><span className="num text-mute">{BREV.clouds}</span> clouds · <span className="num text-mute">{BREV.locations}</span> locations · <span className="num text-mute">{BREV.rows.toLocaleString()}</span> rows · r² <span className="num text-mute">{BREV.r2}</span></div>
            <p className="mt-3 text-[11.5px] text-dim">Dearer per hour, but sells whole 8-GPU nodes</p>
          </div>
        </div>
      </Panel>

      <Panel
        title="Offers"
        eyebrow={<span className="flex items-center gap-1.5" title="Click a row to inspect the provider">Observed quotes<Fn n={2} note={NOTES[1]} />{offersFresh && <Fresh quiet f={offersFresh} label={offers.some((o) => o.freshness !== offersFresh) ? `${freshLabel(offersFresh)} unless a row says otherwise` : undefined} />}</span>}
        right={
          <div className="flex flex-wrap items-center gap-2 max-md:w-full">
            <select value={contract} onChange={(e) => setContract(e.target.value as typeof contract)} className="h-7 rounded-[4px] border border-line2 bg-ink px-2 text-[11.5px] max-md:h-11 max-md:min-w-0 max-md:flex-1 max-md:basis-[40%] max-md:text-[16px]"><option value="all">All contracts</option>{['On-demand', 'Spot', '1-mo reserved', '1-yr reserved'].map((c) => <option key={c}>{c}</option>)}</select>
            <select value={provider} onChange={(e) => setProvider(e.target.value)} className="h-7 rounded-[4px] border border-line2 bg-ink px-2 text-[11.5px] max-md:h-11 max-md:min-w-0 max-md:flex-1 max-md:basis-[40%] max-md:text-[16px]"><option value="all">All providers</option>{PROVIDERS.filter((p) => OFFERS.some((o) => o.provider === p.id)).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select>
            <select value={region} onChange={(e) => setRegion(e.target.value)} className="h-7 rounded-[4px] border border-line2 bg-ink px-2 text-[11.5px] max-md:h-11 max-md:min-w-0 max-md:flex-1 max-md:basis-[40%] max-md:text-[16px]"><option value="all">All regions</option>{[...new Map(OFFERS.map((o) => [o.region, o])).values()].map((o) => ({ id: o.region, name: offerRegionName(o) })).sort((a, b) => a.name.localeCompare(b.name)).map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}</select>
            <Seg size="sm" value={ic} onChange={setIc} options={[{ id: 'all', label: 'Any fabric' }, { id: 'ib', label: 'InfiniBand' }]} />
          </div>
        }
        bodyClass="p-0"
      >
        {offers.length === 0 ? <Empty className="m-4" title="No offers match these filters" body="Try another region or contract type. FLOPX shows only offers it has actually observed." /> : (
          <div className="max-h-[480px] overflow-auto">
            {terms.length > 0 && <div className="border-b border-line-soft px-3 py-1.5 text-[11px] text-dim">SLA · egress by provider type: {terms.map(({ t, o }, k) => <span key={t}>{k > 0 && '; '}{t} <span className="num text-mute">{o!.sla} · {o!.egress}</span></span>)}. Observed time is {offersFresh ? freshLabel(offersFresh).toLowerCase() : 'live'} unless marked.</div>}
            <table className="w-full text-[12px] max-md:text-[12.5px]">
              <thead className="sticky top-0 bg-panel max-md:z-[2]">
                <tr className="h-8 border-b border-line text-[11px] whitespace-nowrap text-dim">
                  {['Provider', 'Region', 'Contract', 'Fabric', 'Node (GPUs)', 'Availability', 'Observed', '$/GPU-hr'].map((h, i) => <th key={h} title={h === 'Observed' ? 'Status as the eyebrow unless marked' : undefined} className={cx('px-3 font-medium', i === 7 || i === 4 ? 'text-right' : 'text-left', i === 0 && 'max-md:sticky max-md:left-0 max-md:z-[1] max-md:bg-panel')}>{h}</th>)}
                </tr>
              </thead>
              <tbody>
                {offers.map((o) => {
                  const p = providerById(o.provider)!
                  const kind = o.note ?? p.type
                  return (
                    <tr key={o.id} onClick={() => inspect({ kind: 'provider', id: p.id, label: p.name })} title={o.detail} className="h-9 cursor-pointer border-b border-line-soft hover:bg-hover max-md:h-11">
                      <td className="px-3 py-1.5 whitespace-nowrap max-md:sticky max-md:left-0 max-md:z-[1] max-md:max-w-[132px] max-md:truncate max-md:bg-panel max-md:shadow-[1px_0_0_var(--color-line-soft)]">{p.name}{!p.name.toLowerCase().includes(kind.toLowerCase()) && <> <span className="text-[10.5px] text-dim">{kind}</span></>}</td>
                      <td className="px-3 py-1.5 whitespace-nowrap text-mute">{offerRegionName(o).split(' (')[0]}</td>
                      <td className="px-3 py-1.5 whitespace-nowrap">{o.contract}</td>
                      <td className="px-3 py-1.5 whitespace-nowrap text-mute">{o.interconnect}</td>
                      <td className="num px-3 py-1.5 text-right text-mute">{o.nodeSize}</td>
                      <td className="px-3 py-1.5"><span className={cx('text-[11.5px]', o.available === 'Available' && 'text-up', o.available === 'Limited' && 'text-warn', o.available === 'Sold out' && 'text-down', o.available === 'Waitlist' && 'text-mute')}>{o.available}</span></td>
                      <td className="px-3 py-1.5"><span className="flex items-center gap-1.5 whitespace-nowrap"><span className="num text-[11px] text-dim">{o.observed}</span>{o.freshness !== offersFresh && <Fresh f={o.freshness} />}</span></td>
                      <td className="num px-3 py-1.5 text-right text-[13px]">{o.price.toFixed(2)}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
      <Notes items={NOTES} />
      <div className="mt-3 flex justify-end"><Btn size="sm" variant="ghost" className="max-md:min-h-11" onClick={() => navigate('procurement', { gpu })}>Plan a procurement with these offers <ArrowRight size={12} /></Btn></div>
    </div>
  )
}

/** Round tick step so the axis reads $1, $2, ... rather than raw min/max fractions. */
function niceStep(span: number, max = 6) {
  return [0.1, 0.25, 0.5, 1, 2, 5, 10].find((s) => span / s <= max) ?? 10
}

function Strip({ offers, onPick }: { offers: Offer[]; onPick: (o: Offer) => void }) {
  const [ref, w] = useWidth<HTMLDivElement>()
  if (!offers.length) return <Empty title="No offers for this GPU" />
  const rows: Offer['contract'][] = ['Spot', 'On-demand', '1-mo reserved', '1-yr reserved']
  const prices = offers.map((o) => o.price)
  const step = niceStep(Math.max(...prices) - Math.min(...prices))
  const lo = Math.floor(Math.min(...prices) / step) * step
  const hi = Math.ceil(Math.max(...prices) / step) * step
  const ticks = Array.from({ length: Math.round((hi - lo) / step) + 1 }, (_, k) => lo + k * step)
  const pad = 112
  const rowH = 52
  const r = 4
  const plotH = rows.length * rowH
  const sx = (v: number) => pad + ((v - lo) / (hi - lo || 1)) * (w - pad - 20)
  const color = (o: Offer) => { const t = providerById(o.provider)!.type; return t === 'Hyperscaler' ? 'var(--color-cat1)' : t === 'Neocloud' ? 'var(--color-cat2)' : 'var(--color-cat3)' }
  return (
    <div ref={ref}>
      <svg width={w} height={plotH + 22} role="img" aria-label="Offer prices by contract type, dollars per GPU-hour. Click a dot to inspect the provider.">
        {ticks.map((v) => (
          <g key={v}>
            <line x1={sx(v)} x2={sx(v)} y1={0} y2={plotH} stroke="var(--color-line)" strokeDasharray="2 4" />
            <text x={sx(v)} y={plotH + 15} textAnchor="middle" className="fill-dim font-mono" fontSize={9.5}>${step < 1 ? v.toFixed(2) : v.toFixed(0)}</text>
          </g>
        ))}
        {rows.map((row, k) => {
          const inRow = offers.filter((o) => o.contract === row)
          const cy = k * rowH + rowH / 2
          const dy = beeswarm(inRow.map((o) => sx(o.price)), r, rowH / 2 - r - 1)
          const sorted = inRow.map((o) => o.price).sort((a, b) => a - b)
          const mid = Math.floor((sorted.length - 1) / 2)
          const med = sorted.length ? (sorted[mid] + sorted[sorted.length - 1 - mid]) / 2 : null
          return (
            <g key={row}>
              <text x={0} y={cy + 3.5} className="fill-mute" fontSize={11}>{row}<tspan className="fill-dim font-mono" fontSize={9.5} dx={6}>{inRow.length}</tspan></text>
              <line x1={pad} x2={w - 20} y1={cy} y2={cy} stroke="var(--color-line)" strokeOpacity={0.6} />
              {med != null && (
                <line x1={sx(med)} x2={sx(med)} y1={cy - rowH / 2 + 3} y2={cy + rowH / 2 - 3} stroke="var(--color-bone)" strokeOpacity={0.35} strokeWidth={1.5}>
                  <title>{`${row} median $${med.toFixed(2)}/GPU-hr`}</title>
                </line>
              )}
              {inRow.map((o, j) => {
                const sold = o.available === 'Sold out'
                return (
                  <circle key={o.id} cx={sx(o.price)} cy={cy + dy[j]} r={r} fill={sold ? 'var(--color-panel)' : color(o)} stroke={sold ? color(o) : 'var(--color-panel)'} strokeWidth={1.5} className="cursor-pointer" onClick={() => onPick(o)}>
                    <title>{`${providerById(o.provider)!.name} · ${offerRegionName(o)} · $${o.price.toFixed(2)}/GPU-hr · ${o.available}${o.note ? ` · ${o.note}` : ''}${o.freshness === 'illustrative' ? ' · illustrative' : ''}`}</title>
                  </circle>
                )
              })}
            </g>
          )
        })}
      </svg>
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-mute">
        <span className="flex items-center gap-1.5"><span className="size-2.5 rounded-full bg-cat1" />Hyperscaler (list)</span>
        <span className="flex items-center gap-1.5"><span className="size-2.5 rounded-full bg-cat2" />Neocloud</span>
        <span className="flex items-center gap-1.5"><span className="size-2.5 rounded-full bg-cat3" />Marketplace</span>
        <span className="flex items-center gap-1.5"><span className="size-2.5 rounded-full border-[1.5px] border-mute" />Sold out</span>
        <span className="flex items-center gap-1.5"><span className="h-2.5 w-[1.5px] bg-bone/40" />Median</span>
      </div>
    </div>
  )
}

function WorkloadBox() {
  const { navigate } = useStore()
  // The worked example matches the Procurement desk's default spec (64 GPUs, 12 weeks, any fabric):
  // no live offer reports a fabric, so an InfiniBand requirement would only ever match design offers.
  const [text, setText] = useState('Fine-tune a 70B model on 64 H100s for 12 weeks, any fabric, EU or US.')
  const [run, setRun] = useState(false)
  const parsed = useMemo(() => {
    const n = +(text.match(/(\d+)\s*(?:x\s*)?(?:h100|gpus?|b200|h200)/i)?.[1] ?? 64)
    const weeks = +(text.match(/(\d+)\s*weeks?/i)?.[1] ?? 12)
    const gpu = /b200/i.test(text) ? 'b200' : /h200/i.test(text) ? 'h200' : 'h100'
    const ib = /infiniband|multi-node/i.test(text)
    return { n, weeks, gpu, ib }
  }, [text])
  const opts = OFFERS.filter((o) => o.gpu === parsed.gpu && (!parsed.ib || o.interconnect === 'InfiniBand 3.2T') && o.available !== 'Sold out' && o.contract !== '1-yr reserved').sort((a, b) => a.price - b.price).slice(0, 3)
  return (
    <Panel title="Describe a workload" eyebrow={<><TruthTag truth="fct" compact /> Rental recommendation</>} right={<span className="text-[11px] text-dim">Not an equity trade</span>}>
      <textarea value={text} onChange={(e) => { setText(e.target.value); setRun(false) }} rows={3} className="w-full resize-none rounded-[4px] border border-line2 bg-ink p-2.5 text-[12px] leading-snug outline-none focus:border-mute max-md:text-[16px]" />
      <div className="mt-1.5 text-[11px] text-dim">Parsed: {parsed.n} × {GPUS.find((g) => g.id === parsed.gpu)!.name} · {parsed.weeks} wk · {parsed.ib ? 'InfiniBand required' : 'any fabric'}</div>
      <Btn size="sm" variant="primary" className="mt-2 max-md:min-h-11 max-md:px-4" onClick={() => setRun(true)}>Compare options</Btn>
      {run && (
        <div className="fade-in mt-3 space-y-1.5">
          {opts.length === 0 && <div className="text-[12px] text-mute">No observed offers meet these constraints.</div>}
          {opts.map((o, i) => {
            const total = o.price * parsed.n * parsed.weeks * 168
            return (
              <div key={o.id} className="rounded-[4px] border border-line p-2.5">
                <div className="flex items-baseline justify-between gap-3 text-[12px]"><span className="min-w-0"><span className="num mr-1.5 text-dim">{i + 1}</span><span className="font-medium text-bone">{providerById(o.provider)!.name}</span><span className="text-mute"> · {offerRegionShort(o)} · {o.contract}</span></span><span className="num shrink-0 font-semibold text-bone" title="Estimated total for the parsed workload">${Math.round(total / 1000)}k</span></div>
                <div className="mt-0.5 flex flex-wrap gap-x-2 text-[11px] text-dim">
                  <span><span className="num">${o.price.toFixed(2)}</span>/GPU-hr</span>
                  <span className={cx('whitespace-nowrap', o.available === 'Available' ? 'text-up' : 'text-warn')}>{o.available}{o.available !== 'Available' && ' · capacity risk'}</span>
                  <span className="whitespace-nowrap">reliability {fmtReliability(providerById(o.provider))}</span>
                  {o.freshness === 'illustrative' && <Fresh f="illustrative" />}
                </div>
              </div>
            )
          })}
          <Btn size="sm" variant="ghost" onClick={() => navigate('procurement', { gpu: parsed.gpu, n: String(parsed.n), weeks: String(parsed.weeks) })}>Full cost & risk scenario in Procurement <ArrowRight size={11} /></Btn>
        </div>
      )}
    </Panel>
  )
}
