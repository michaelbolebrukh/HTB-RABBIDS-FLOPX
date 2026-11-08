import { useRef, useState, type ReactNode } from 'react'
import { Fn, Fresh, Notes, PageHeader, Panel, PanelSwitch, StripCell, TruthGlyph } from '@/components/ui'
import { Row } from '@/components/collapse'
import { blended, closedVsOpen, median, spreads } from '@/lib/tokenmarket'
import { FIT, MODEL, NOTES, SNAP, fmtStamp, times, usd } from '@/components/intel/shared'
import { CheapestWay, ClosedVsOpen, NewestReleases, PriceLadder } from '@/components/intel/Labs'
import { EndpointTable, HedonicFit, HostTable, ProviderLeaderboard, SpreadLadder, TokenProcurement, WhyCheaper } from '@/components/intel/Providers'

const SPREADS = spreads(SNAP.endpoints)
const CVO = closedVsOpen(SNAP.models)
const labMedian = (lab: string) => median(SNAP.models.filter((m) => m.lab === lab).map(blended))

export default function Intelligence() {
  const deepest = SPREADS[0]
  const [model, setModel] = useState(deepest?.model ?? '')
  const routeRef = useRef<HTMLDivElement>(null)
  const route = (id: string) => {
    setModel(id)
    routeRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }
  const labs = new Set(SNAP.models.map((m) => m.lab)).size
  const open = SNAP.models.filter((m) => m.open).length
  const ant = labMedian('Anthropic'), ds = labMedian('DeepSeek')
  const name = (id: string) => MODEL.get(id)?.name ?? id
  const stats: { truth: 'obs' | 'inf'; label: string; value: ReactNode; sub: ReactNode }[] = [
    { truth: 'obs', label: 'Token index', value: <>{usd(deepest.median)}<span className="ml-0.5 text-[12px] text-dim">/M</span></>, sub: `${name(deepest.model)} median $/M · snapshot ${snapshotAge(SNAP.endpointsAt)}, no history yet` },
    { truth: 'obs', label: 'Models priced', value: SNAP.models.length, sub: `${labs} labs · ${open} open-weight` },
    { truth: 'obs', label: 'Closed vs open, median', value: times(CVO.ratio), sub: `closed ${usd(CVO.closedMedian)} · open ${usd(CVO.openMedian)} per M` },
    { truth: 'obs', label: 'Anthropic vs DeepSeek, median', value: times(ant / ds), sub: `${usd(ant)} vs ${usd(ds)} per M` },
    { truth: 'obs', label: 'Same weights, many prices', value: times(deepest.spread), sub: `${name(deepest.model)}, ${deepest.hosts} hosts${deepest.samePrecision ? ` · ${times(deepest.samePrecision.spread)} at ${deepest.samePrecision.bits} bit` : ''}` },
    { truth: 'inf', label: 'Hedonic fit R²', value: FIT.r2.toFixed(3), sub: `within the same weights, attributes explain ${Math.round(FIT.withinShare * 100)}%` },
  ]
  const providers = new Set(SNAP.endpoints.map((e) => e.provider)).size
  const epModels = new Set(SNAP.endpoints.map((e) => e.model)).size
  return (
    <div>
      <PageHeader
        eyebrow={<>OpenRouter list prices · catalog <span className="num">{fmtStamp(SNAP.catalogAt)}</span> · endpoints <span className="num">{fmtStamp(SNAP.endpointsAt)}</span> UTC<Fn n={6} note={NOTES[5]} /></>}
        title="Intelligence market"
        sub="What labs list, and what providers charge for the same open weights."
        right={<Fresh quiet f="historical" label={`Catalog ${fmtStamp(SNAP.catalogAt)} · endpoints ${fmtStamp(SNAP.endpointsAt)}`} />}
        about={<><b className="text-bone">Labs</b> set list prices for their own models, closed and open weights. <b className="text-bone">Providers</b> then serve the same open weights at very different prices. Every price is a blended list price per million tokens (0.75 × prompt + 0.25 × completion).<Fn n={1} note={NOTES[0]} /></>}
      />

      <Panel variant="strip" id="Key figures" title="Token prices" className="mb-7">
        {stats.map((s, i) => (
          <StripCell key={s.label} lead={i === 0} label={<span className="flex items-center gap-1.5"><TruthGlyph truth={s.truth} size={7} />{s.label}</span>} sub={s.sub}>{s.value}</StripCell>
        ))}
      </Panel>

      <LayerHeading n={1} title="Labs: what each lab charges" sub={<>{`OpenRouter catalog · ${SNAP.models.length} text models · list price on each default route`}<Fn n={1} note={NOTES[0]} /></>} />
      <Row cols={[1.35, 1]} className="mb-9">
        <PriceLadder />
        <div className="flex min-w-0 flex-col gap-5">
          <ClosedVsOpen />
          <NewestReleases />
        </div>
      </Row>

      <LayerHeading n={2} title="Providers: same weights, many prices" sub={`${SNAP.endpoints.length} endpoints · ${epModels} open-weight models · ${providers} providers`} />
      <Row ref={routeRef} cols={[1.35, 1]} className="mb-5 scroll-mt-4">
        <PanelSwitch items={[
          { label: 'Spread ladder', node: <SpreadLadder onRoute={route} /> },
          { label: 'Host table', node: <HostTable onRoute={route} /> },
          { label: 'Cheapest way to buy', node: <CheapestWay onRoute={route} /> },
        ]} />
        <TokenProcurement model={model} onModel={setModel} />
      </Row>
      <Row cols={[1.35, 1]} className="mb-5">
        <PanelSwitch items={[
          { label: 'Why it is cheaper', node: <WhyCheaper model={model} /> },
          { label: 'Hedonic fit', node: <HedonicFit /> },
        ]} />
        <ProviderLeaderboard />
      </Row>
      <div className="mb-5">
        <EndpointTable model={model} />
      </div>

      <Notes items={NOTES} />
    </div>
  )
}

/** "4.6 h old" / "3 d old": the age of the archived OpenRouter snapshot, never a LIVE badge. */
export function snapshotAge(iso: string, now: Date = new Date()): string {
  const h = (now.getTime() - new Date(iso).getTime()) / 3600000
  if (!Number.isFinite(h) || h < 0) return 'archived'
  if (h < 1) return `${Math.round(h * 60)} min old`
  if (h < 48) return `${h.toFixed(1)} h old`
  return `${Math.round(h / 24)} d old`
}

function LayerHeading({ n, title, sub }: { n: number; title: string; sub: ReactNode }) {
  return (
    <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-line pb-2">
      <span className="text-[11px] font-medium text-dim">Layer {n}</span>
      <h2 className="text-[16px] leading-tight font-semibold text-bone">{title}</h2>
      <span className="text-[11.5px] text-dim">{sub}</span>
    </div>
  )
}
