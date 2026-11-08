import { useRef, useState, type ReactNode } from 'react'
import { BrainCircuit } from 'lucide-react'
import { Fresh, PageHeader, TruthGlyph } from '@/components/ui'
import { blended, closedVsOpen, median, spreads } from '@/lib/tokenmarket'
import { FIT, MODEL, SNAP, fmtStamp, times, usd } from '@/components/intel/shared'
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
  const stats: { truth: 'obs' | 'inf'; label: string; value: ReactNode; sub: ReactNode; live?: boolean }[] = [
    { truth: 'obs', label: 'Token index', value: <>{usd(deepest.median)}<span className="ml-0.5 text-[12px] text-dim">/M</span></>, sub: `${name(deepest.model)} median $/M · one snapshot, no history yet`, live: true },
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
        eyebrow={<><BrainCircuit size={12} /> Intelligence market · OpenRouter · {fmtStamp(SNAP.endpointsAt)} UTC</>}
        title="The market for intelligence"
        sub={<>Two layers of one market. <b className="text-bone">Labs</b> set list prices for their own models, closed and open weights. <b className="text-bone">Providers</b> then serve the same open weights at very different prices. Every price is a blended list price per million tokens (0.75 × prompt + 0.25 × completion): what someone asks, not what anyone paid.</>}
        right={<Fresh quiet f="historical" label={`Catalog ${fmtStamp(SNAP.catalogAt)} · endpoints ${fmtStamp(SNAP.endpointsAt)}`} />}
      />

      <div className="mb-7 grid grid-cols-2 overflow-hidden rounded-md border border-line bg-panel md:grid-cols-3 xl:grid-cols-6">
        {stats.map((s) => (
          <div key={s.label} className="border-line px-4 py-3 not-last:border-r max-xl:border-b">
            <div className="mb-1.5 flex items-center justify-between">
              <span className="flex items-center gap-1.5 font-mono text-[9.5px] tracking-wider text-dim uppercase"><TruthGlyph truth={s.truth} size={7} />{s.truth === 'obs' ? 'Obs' : 'Inf'}</span>
              {s.live && <span className="rounded-[3px] border border-line2 px-1 font-mono text-[9px] text-mute uppercase" title={`OpenRouter snapshot ${fmtStamp(SNAP.endpointsAt)} UTC; FLOPX has no live token-market feed yet`}>Snapshot · {snapshotAge(SNAP.endpointsAt)}</span>}
            </div>
            <div className="text-[11.5px] text-mute">{s.label}</div>
            <div className="num text-[24px] leading-tight text-bone">{s.value}</div>
            <div className="mt-0.5 text-[10.5px] leading-snug text-dim">{s.sub}</div>
          </div>
        ))}
      </div>

      <LayerHeading n={1} title="Labs: what each lab charges" sub={`OpenRouter catalog · ${SNAP.models.length} text models · list price on each model's default route`} />
      <div className="mb-5 grid grid-cols-1 gap-5 xl:grid-cols-[1.35fr_1fr]">
        <PriceLadder />
        <ClosedVsOpen />
      </div>
      <div className="mb-9 grid grid-cols-1 gap-5 xl:grid-cols-[1.35fr_1fr]">
        <CheapestWay onRoute={route} />
        <NewestReleases />
      </div>

      <LayerHeading n={2} title="Providers: same weights, many prices" sub={`${SNAP.endpoints.length} endpoints · ${epModels} open-weight models · ${providers} providers`} />
      <div ref={routeRef} className="mb-5 grid scroll-mt-4 grid-cols-1 gap-5 xl:grid-cols-[1.35fr_1fr]">
        <SpreadLadder onRoute={route} />
        <TokenProcurement model={model} onModel={setModel} />
      </div>
      <div className="mb-5 grid grid-cols-1 gap-5 xl:grid-cols-[1.35fr_1fr]">
        <HostTable onRoute={route} />
        <WhyCheaper model={model} />
      </div>
      <div className="mb-5 grid grid-cols-1 gap-5 xl:grid-cols-2">
        <HedonicFit />
        <ProviderLeaderboard />
      </div>
      <div className="mb-5">
        <EndpointTable model={model} />
      </div>

      <ul className="space-y-1 border-t border-line pt-3 text-[11px] leading-relaxed text-dim">
        <li><b className="text-mute">Catalog:</b> list prices on OpenRouter for each model's default route; closed models are priced by their lab or its cloud resellers. Not executed spend, and output-heavy work costs more than the 3:1 blend.</li>
        <li><b className="text-mute">Endpoints:</b> prices are list prices net of OpenRouter discounts, not executed spend. Quantization is a provider label, not verified, and is unknown for many endpoints.</li>
        <li><b className="text-mute">Endpoints:</b> latency and throughput are not in this snapshot, so fast-hardware hosts carry a positive residual that is partly paid-for speed, not mispricing. Some endpoints have time-of-day pricing; the stored price is the default window.</li>
        <li><b className="text-mute">Sources:</b> GET openrouter.ai/api/v1/models at {fmtStamp(SNAP.catalogAt)} UTC; GET openrouter.ai/api/v1/models/&#123;id&#125;/endpoints at {fmtStamp(SNAP.endpointsAt)} UTC. Refresh with <code className="font-mono">node scripts/openrouter-snapshot.mjs</code>.</li>
      </ul>
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

function LayerHeading({ n, title, sub }: { n: number; title: string; sub: string }) {
  return (
    <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-line pb-2">
      <span className="font-mono text-[10px] tracking-wider text-dim uppercase">Layer {n}</span>
      <h2 className="font-serif text-[20px] leading-tight text-bone">{title}</h2>
      <span className="text-[11.5px] text-dim">{sub}</span>
    </div>
  )
}
