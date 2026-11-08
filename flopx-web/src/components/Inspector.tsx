import type { ReactNode } from 'react'
import { ArrowUpRight, MessageSquareText, X } from 'lucide-react'
import { fmtPct, fmtReliability, GPUS, gpuById, horizonLabel, MONTHS, NOW_IDX, OFFERS, providerById, regionById, REGIONS, type EntityKind } from '@/data/core'
import { offerRegionShort } from '@/live/gpu'
import { energyById, PROJECTS, projectById } from '@/data/infra'
import { BASKETS, basketSeries, chg, last, NEWS, PRED, secByTicker } from '@/data/finance'
import { signalById, SIGNALS } from '@/data/fund'
import { pressureAt, pressureColor, pressureWord, uncAt } from '@/lib/model'
import { useStore, type EntityRef } from '@/lib/store'
import { Btn, cx, Delta, EntityLink, Fresh, Source, Spark, TruthGlyph, TruthTag, Validation } from './ui'

const KIND_LABEL: Record<EntityKind, string> = { region: 'Region', gpu: 'GPU category', provider: 'Provider', project: 'Data-centre project', energy: 'Energy market', security: 'Security', basket: 'Basket', signal: 'Strategy signal', news: 'Event', theme: 'Theme' }

function Chain({ steps }: { steps: { label: string; truth?: 'obs' | 'inf' | 'fct' | 'dec'; children: ReactNode }[] }) {
  return (
    <ol className="relative ml-1.5 border-l border-line2 pl-4">
      {steps.map((s, i) => (
        <li key={i} className="relative pb-4 last:pb-0">
          <span className="absolute -left-[21px] top-0.5 flex size-[10px] items-center justify-center rounded-full border border-line2 bg-ink"><span className="size-1 rounded-full bg-mute" /></span>
          <div className="eyebrow mb-1 flex items-center gap-2">{s.label}{s.truth && <TruthTag truth={s.truth} compact />}</div>
          <div className="space-y-1 text-[12px] text-bone/90">{s.children}</div>
        </li>
      ))}
    </ol>
  )
}

const ref = (kind: EntityKind, id: string, label: string): EntityRef => ({ kind, id, label })

export function Inspector() {
  const { inspector, setInspector, navigate, setXray, ask, setTicket, setFund, positions } = useStore()
  if (!inspector) return null
  const { kind, id } = inspector
  let body: ReactNode = null
  let actions: ReactNode = null
  let title = inspector.label
  let sub: ReactNode = null

  if (kind === 'region') {
    const r = regionById(id)!
    const p = pressureAt(r, NOW_IDX)
    title = r.name
    sub = <>{r.marketplace} · grid {r.grid.toUpperCase()} · {r.quality !== 'good' ? <span className="text-warn">{r.quality} coverage</span> : 'full coverage'}</>
    const projects = PROJECTS.filter((x) => x.region === r.id)
    const held = positions.filter((x) => r.documented.some((d) => d.t === x.ticker) || r.sensitivities.some((s) => s.t === x.ticker))
    const sigs = SIGNALS.filter((s) => s.regions.includes(r.id))
    body = p == null ? (
      <div className="rounded-md border border-dashed border-line2 p-4 text-[12px] text-mute">No usable rental quotes or market data. FLOPX deliberately shows <b className="text-bone">no estimate</b> rather than extrapolating from neighbouring regions. {r.opMW} MW of facilities are reported here (observed), but compute availability is unknown.</div>
    ) : (
      <>
        <div className="mb-4 grid grid-cols-2 gap-3">
          <div className="rounded-md border border-inf/25 p-3">
            <TruthTag truth="inf" />
            <div className="num mt-1 text-[26px] leading-none" style={{ color: pressureColor(p) }}>{p!.toFixed(0)}<span className="ml-1 text-[12px] text-mute">± {uncAt(r, NOW_IDX).toFixed(0)}</span></div>
            <div className="mt-1 text-[11px] text-mute">Compute pressure · {pressureWord(p)}</div>
          </div>
          <div className="rounded-md border border-line p-3">
            <TruthTag truth="fct" />
            <div className="num mt-1 text-[26px] leading-none text-fct">{pressureAt(r, NOW_IDX + 6)!.toFixed(0)}<span className="ml-1 text-[12px] text-mute">± {uncAt(r, NOW_IDX + 6).toFixed(0)}</span></div>
            <div className="mt-1 text-[11px] text-mute">6M forecast · {MONTHS[NOW_IDX + 6]}</div>
          </div>
        </div>
        <Spark values={r.pressure} width={392} height={44} color="var(--color-inf)" dashedFrom={NOW_IDX} className="mb-4" />
        <Chain steps={[
          { label: 'Physical infrastructure', truth: 'obs', children: <>{projects.length ? projects.map((x) => <div key={x.id} className="flex justify-between"><EntityLink r={ref('project', x.id, x.name)} /><span className="text-[11px] text-mute">{x.stage} · <span className="num">{x.reportedMW} MW</span></span></div>) : <span className="text-mute">No tracked projects</span>}<div className="text-[11px] text-dim">{r.opMW.toLocaleString()} MW operational · {r.conMW.toLocaleString()} MW construction (reported)</div></> },
          { label: 'Compute economics', truth: 'obs', children: <><div className="flex justify-between"><span>H100 median on-demand</span><span className="num">${r.h100.toFixed(2)} <Delta v={r.h100Chg} /></span></div><div className="flex justify-between"><span>Premium vs global</span><span className="num">{fmtPct(r.premium, 0)}</span></div>{r.power > 0 && <div className="flex justify-between"><span>Power (<EntityLink r={ref('energy', r.grid, r.grid.toUpperCase())} />)</span><span className="num">${r.power}/MWh</span></div>}</> },
          { label: 'Affected companies', children: <>{r.documented.map((d) => <div key={d.t} className="flex items-start gap-2"><span className="mt-1.5 inline-block h-px w-3 bg-bone" title="Documented tie" /><div><EntityLink r={ref('security', d.t, d.t)} className="font-mono" /> <span className="text-mute">— {d.tie}</span><div><Source source={d.source} /></div></div></div>)}{r.sensitivities.map((s) => <div key={s.t} className="flex items-start gap-2"><span className="mt-1.5 inline-block h-px w-3 border-t border-dashed border-inf" title="Modelled sensitivity" /><div><EntityLink r={ref('security', s.t, s.t)} className="font-mono" /> <span className="text-mute">— modelled β {s.beta} (R² {s.r2}) · estimated association, not proven cause</span></div></div>)}{!r.documented.length && !r.sensitivities.length && <span className="text-mute">No documented or modelled exposure</span>}</> },
          { label: 'Forecasts & signals', truth: 'fct', children: sigs.length ? sigs.map((s) => <div key={s.id} className="flex items-center justify-between"><EntityLink r={ref('signal', s.id, s.name)} /><Validation s={s.horizons['1M'].status} /></div>) : <span className="text-mute">No strategy signals use this region</span> },
          { label: 'Portfolio decisions', truth: 'dec', children: held.length ? held.map((h) => <div key={h.ticker} className="flex justify-between font-mono text-[11px]"><span>{h.ticker} · {h.strategy}</span><span>{h.qty > 0 ? 'Long' : 'Short'} {Math.abs(h.qty).toLocaleString()} sh</span></div>) : <span className="text-mute">No paper exposure</span> },
        ]} />
      </>
    )
    actions = <><Btn variant="primary" size="sm" onClick={() => { setXray({ region: r.id, tab: 'brief' }); navigate('xray'); setInspector(null) }}>Open in X-Ray <ArrowUpRight size={12} /></Btn><Btn size="sm" onClick={() => ask(`Why is compute pressure ${pressureWord(p).toLowerCase()} in ${r.name}?`)}><MessageSquareText size={12} /> Ask</Btn></>
  } else if (kind === 'gpu') {
    const g = gpuById(id)!
    const offers = OFFERS.filter((o) => o.gpu === g.id).sort((a, b) => a.price - b.price)
    title = `${g.name} rental`
    sub = `${g.arch} · ${g.memory}`
    body = (
      <>
        <div className="mb-3 flex items-end justify-between">
          <div><TruthTag truth="obs" /><div className="num mt-1 text-[26px] leading-none">${(g.live ? g.live.current : g.index[NOW_IDX]).toFixed(2)}<span className="ml-1 text-[12px] text-mute">/GPU-hr median</span></div></div>
          {g.live ? <Fresh f={g.live.freshness} label={g.live.freshLabel} /> : <Fresh f="illustrative" />}
        </div>
        <Spark values={g.index.slice(0, NOW_IDX + 1)} width={392} height={44} className="mb-4" />
        <div className="eyebrow mb-1.5">Cheapest observed offers</div>
        <div className="space-y-1">
          {offers.slice(0, 6).map((o) => (
            <div key={o.id} className="grid grid-cols-[1fr_auto_auto] gap-3 font-mono text-[11px]">
              <span className="truncate"><EntityLink r={ref('provider', o.provider, providerById(o.provider)!.name)} /> · {offerRegionShort(o)}</span>
              <span className="text-dim">{o.contract}</span>
              <span className="text-bone">${o.price.toFixed(2)}</span>
            </div>
          ))}
        </div>
        <div className="mt-4 text-[11px] leading-relaxed text-dim">Prices are list/quoted rates, not transaction prices. Hyperscaler list prices are not comparable to negotiated reserved contracts.</div>
      </>
    )
    actions = <><Btn variant="primary" size="sm" onClick={() => { navigate('markets', { gpu: g.id }); setInspector(null) }}>Compare offers</Btn><Btn size="sm" onClick={() => { navigate('procurement', { gpu: g.id }); setInspector(null) }}>Plan procurement</Btn></>
  } else if (kind === 'provider') {
    const p = providerById(id)!
    const offers = OFFERS.filter((o) => o.provider === p.id)
    title = p.name
    sub = `${p.type}${p.ticker ? ` · listed as ${p.ticker}` : ''}`
    body = (
      <>
        <div className="mb-4 grid grid-cols-3 gap-2">
          <Stat label="Reliability" value={fmtReliability(p)} truth="inf" sub={p.freshness === 'illustrative' ? 'illustrative' : 'median host score /100'} />
          <Stat label="Offers" value={`${offers.length}`} truth="obs" sub="tracked" />
          <Stat label="Regions" value={`${new Set(offers.map((o) => o.region)).size}`} truth="obs" sub="observed" />
        </div>
        <div className="mb-3 text-[12px] text-mute">{p.note}</div>
        <div className="mb-1.5 text-[11px] text-dim">{p.freshness === 'illustrative' ? 'Illustrative provider. Reliability blends observed status-page uptime (90d) with incident reports. Marketplace hosts vary widely; score is a median.' : 'Reliability is the median host reliability score the provider reports on its live offers, where it reports one.'}</div>
        {p.ticker && <div className="mt-3">Listed equity: <EntityLink r={ref('security', p.ticker, p.ticker)} className="font-mono" /></div>}
      </>
    )
    actions = <Btn variant="primary" size="sm" onClick={() => { navigate('markets', { provider: p.id }); setInspector(null) }}>View offers</Btn>
  } else if (kind === 'project') {
    const p = projectById(id)!
    title = p.name
    sub = <>{regionById(p.region)!.name} · {p.developer}</>
    body = (
      <>
        <div className="mb-4 grid grid-cols-3 gap-2">
          <Stat label="Reported" value={`${p.reportedMW} MW`} truth="obs" sub={p.stage} />
          <Stat label="Energised" value={`${p.energizedMW} MW`} truth="obs" sub="as reported" />
          <Stat label={`Usable by ${horizonLabel(12)}`} value={`${p.effectiveRange[0]}–${p.effectiveRange[1]}`} truth="inf" sub="MW est." />
        </div>
        <div className="mb-4 rounded-md border border-line bg-ink p-3 text-[11.5px]">
          <div className="grid grid-cols-3 gap-2 font-mono text-[10.5px]">
            <div><div className="text-dim">Facility</div>{p.lat.toFixed(2)}, {p.lon.toFixed(2)}</div>
            <div><div className="text-dim">Grid zone</div>{p.gridZone}</div>
            <div><div className="text-dim">Market region</div>{p.marketplaceRegion}</div>
          </div>
        </div>
        <Timeline p={p} />
        <div className="mt-4 text-[12px] text-bone/90"><span className="eyebrow mr-2">Impact</span>{p.impact}</div>
      </>
    )
    actions = <Btn variant="primary" size="sm" onClick={() => { setXray({ region: p.region, project: p.id, tab: 'pipeline' }); navigate('xray'); setInspector(null) }}>Open in X-Ray <ArrowUpRight size={12} /></Btn>
  } else if (kind === 'energy') {
    const e = energyById(id)!
    title = e.name
    sub = e.operator
    body = (
      <>
        <div className="mb-3 flex items-end justify-between"><div>{e.freshness !== 'illustrative' && <TruthTag truth="obs" />}<div className="num mt-1 text-[26px] leading-none">{e.currency === 'EUR' ? '€' : '$'}{e.priceNow.toFixed(1)}<span className="ml-1 text-[12px] text-mute">/MWh DA</span></div></div><Fresh f={e.freshness} /></div>
        <Spark values={e.price} width={392} height={44} color="var(--color-warn)" className="mb-4" />
        <div className="mb-3 grid grid-cols-3 gap-2">
          <Stat label="Scarcity hrs" value={e.scarcityHours != null ? `${e.scarcityHours}` : '—'} truth="obs" sub={e.scarcityHours != null ? '>$200/MWh, 30d' : 'not tracked'} />
          <Stat label="Reserve" value={e.reserveMargin != null ? `${e.reserveMargin}%` : '—'} truth="obs" sub={e.reserveMargin != null ? 'margin' : 'not published'} />
          <Stat label="Queue" value={e.queueGW != null ? `${e.queueGW} GW` : '—'} truth="obs" sub="large load" />
        </div>
        <div className="text-[12px] text-mute">{e.constraint}</div>
        <div className="mt-3">Compute regions: {e.regions.map((r) => <EntityLink key={r} r={ref('region', r, regionById(r)!.name)} className="mr-2" />)}</div>
      </>
    )
    actions = <Btn variant="primary" size="sm" onClick={() => { navigate('energy', { market: e.id }); setInspector(null) }}>Open in Energy</Btn>
  } else if (kind === 'security') {
    const s = secByTicker(id)!
    const pos = positions.find((p) => p.ticker === id)
    title = `${s.ticker} · ${s.name}`
    sub = `${s.group} · ${s.exchange}`
    const sigs = SIGNALS.filter((x) => x.asset === id || BASKETS.find((b) => b.id === x.asset)?.members.some((m) => m.t === id))
    body = (
      <>
        <div className="mb-3 flex items-end justify-between"><div><TruthTag truth="obs" /><div className="num mt-1 text-[26px] leading-none">${last(s.px).toFixed(2)} <Delta v={chg(s.px)} className="text-[13px]" /></div></div><Fresh f="delayed" label="Delayed 15m" /></div>
        <Spark values={s.px} width={392} height={44} className="mb-4" />
        <div className="mb-4 space-y-1.5">
          {s.computeLinks.map((l, i) => (
            <div key={i} className="flex items-start gap-2 text-[12px]"><span className={cx('mt-2 inline-block w-3 shrink-0', l.kind === 'documented' ? 'h-px bg-bone' : 'border-t border-dashed border-inf')} /><span><span className="eyebrow mr-1.5">{l.kind}</span>{l.text}{l.region && <> · <EntityLink r={ref('region', l.region, regionById(l.region)!.short)} /></>}</span></div>
          ))}
        </div>
        {sigs.length > 0 && <div className="mb-3"><div className="eyebrow mb-1">Signals</div>{sigs.map((x) => <div key={x.id} className="flex justify-between"><EntityLink r={ref('signal', x.id, x.name)} /><Validation s={x.horizons['1M'].status} /></div>)}</div>}
        <div className="rounded-md border border-dec/25 bg-dec/[0.03] p-3 text-[12px]">
          <TruthTag truth="dec" />
          <div className="mt-1">{pos ? <>Paper position: <b className="num">{pos.qty.toLocaleString()}</b> sh @ ${pos.avg.toFixed(2)} · owner {pos.strategy}</> : 'No paper position.'}</div>
        </div>
      </>
    )
    actions = <><Btn variant="primary" size="sm" onClick={() => { navigate('securities', { t: s.ticker }); setInspector(null) }}>Research</Btn>{s.group !== 'Index' && <Btn variant="decision" size="sm" onClick={() => setTicket({ ticker: s.ticker })}>Trade</Btn>}<Btn size="sm" onClick={() => ask(`What compute exposure does ${s.ticker} have?`)}><MessageSquareText size={12} /> Ask</Btn></>
  } else if (kind === 'basket') {
    const b = BASKETS.find((x) => x.id === id)!
    const sr = basketSeries(b)
    title = b.name
    sub = 'Basket · equal-risk weights (illustrative)'
    body = (
      <>
        <div className="mb-3"><TruthTag truth="obs" /><div className="num mt-1 text-[26px] leading-none">{last(sr).toFixed(1)} <Delta v={chg(sr)} className="text-[13px]" /></div></div>
        <Spark values={sr} width={392} height={44} className="mb-4" />
        <div className="mb-3 text-[12px] text-mute">{b.thesis}</div>
        {b.members.map((m) => <div key={m.t} className="flex justify-between font-mono text-[11.5px]"><EntityLink r={ref('security', m.t, m.t)} /><span className="text-mute">{(m.w * 100).toFixed(0)}%</span></div>)}
      </>
    )
    actions = <Btn variant="primary" size="sm" onClick={() => { navigate('securities', { t: b.id }); setInspector(null) }}>Research basket</Btn>
  } else if (kind === 'signal') {
    const s = signalById(id)!
    title = s.name
    sub = <>{s.assetLabel} · {s.direction}</>
    body = (
      <>
        <p className="mb-4 text-[12.5px] leading-relaxed text-bone/90">{s.summary}</p>
        <div className="space-y-2">
          {(['1W', '1M', '3M'] as const).map((h) => {
            const f = s.horizons[h]
            return (
              <div key={h} className="flex items-center justify-between rounded-[4px] border border-line px-3 py-2">
                <span className="font-mono text-[11px] text-mute">{h}</span>
                {(s.basis === 'backend' || s.basis === 'pending') && f.status === 'insufficient' ? <span className="font-mono text-[11px] text-dim" title={s.basisNote}>not issued</span> : <span className="num text-[12px] text-fct">{f.mean > 0 ? '+' : ''}{f.mean.toFixed(1)}% <span className="text-dim">[{f.p10.toFixed(0)}, {f.p90.toFixed(0)}]</span></span>}
                <Validation s={f.status} />
              </div>
            )
          })}
        </div>
      </>
    )
    actions = <Btn variant="primary" size="sm" onClick={() => { setFund({ tab: 'signals', signal: s.id }); navigate('fund'); setInspector(null) }}>Open in Fund</Btn>
  } else if (kind === 'news') {
    const n = NEWS.find((x) => x.id === id)
    const pm = PRED.find((x) => x.id === id)
    title = n?.title ?? pm?.question ?? id
    sub = n ? `${n.type} · ${n.source}` : pm?.venue
    body = n ? (
      <>
        <div className="mb-2 flex gap-2"><span className="text-[11px] text-mute">{n.verification}</span><Source source={n.time} /></div>
        <p className="mb-4 text-[12.5px] leading-relaxed">{n.summary}</p>
        <div className="text-[12px]">Regions: {n.regions.map((r) => <EntityLink key={r} r={ref('region', r, regionById(r)!.short)} className="mr-2" />)}</div>
        <div className="text-[12px]">Assets: {n.tickers.map((t) => <EntityLink key={t} r={ref('security', t, t)} className="mr-2 font-mono" />)}</div>
      </>
    ) : pm ? (
      <>
        <div className="num text-[26px]">{Math.round(pm.prob * 100)}%<span className="ml-2 text-[12px] text-mute">market-implied</span></div>
        <Spark values={pm.probHist} width={392} height={40} color="var(--color-fct)" />
        {pm.caution && <div className="mt-3 rounded-[4px] border border-warn/30 p-2 text-[11.5px] text-warn">{pm.caution}</div>}
        <div className="mt-3 text-[11.5px] text-dim">A market price reflects traders’ beliefs and liquidity, not an objective probability.</div>
      </>
    ) : null
    actions = <Btn variant="primary" size="sm" onClick={() => { navigate('news', { id }); setInspector(null) }}>Open in News</Btn>
  } else if (kind === 'theme') {
    title = inspector.label
    sub = 'Theme'
    const t = THEMES.find((x) => x.id === id)
    body = t ? <><p className="mb-3 text-[12.5px] text-bone/90">{t.text}</p>{t.links.map((l) => <div key={l.id}><EntityLink r={l} /></div>)}</> : null
  }

  return (
    <aside className="slide-in absolute inset-y-0 right-0 z-40 flex w-[440px] flex-col border-l border-line2 bg-panel shadow-[-24px_0_48px_rgba(30,27,22,0.18)]">
      <header className="flex items-start justify-between gap-3 border-b border-line px-5 py-4">
        <div className="min-w-0">
          <div className="eyebrow mb-1">{KIND_LABEL[kind]}</div>
          <h2 className="text-[20px] leading-tight font-semibold tracking-tight text-bone">{title}</h2>
          {sub && <div className="mt-1 text-[11px] text-dim">{sub}</div>}
        </div>
        <button onClick={() => setInspector(null)} className="cursor-pointer rounded p-1 text-dim hover:bg-hover hover:text-bone" aria-label="Close inspector"><X size={16} /></button>
      </header>
      <div className="flex-1 overflow-y-auto px-5 py-4">{body}</div>
      {actions && <footer className="flex flex-wrap gap-2 border-t border-line px-5 py-3">{actions}</footer>}
    </aside>
  )
}

export function Stat({ label, value, sub, truth }: { label: string; value: ReactNode; sub?: ReactNode; truth?: 'obs' | 'inf' | 'fct' | 'dec' }) {
  return (
    <div className={cx('rounded-md border p-2.5', truth === 'inf' ? 'border-inf/25' : truth === 'fct' ? 'border-fct/25 border-dashed' : truth === 'dec' ? 'border-dec/25' : 'border-line')}>
      <div className="flex items-center gap-1.5 text-[11px] text-dim font-medium">{truth && <TruthGlyph truth={truth} size={7} />}{label}</div>
      <div className="num mt-1 text-[15px] text-bone">{value}</div>
      {sub && <div className="mt-0.5 text-[11px] text-dim">{sub}</div>}
    </div>
  )
}

export function Timeline({ p }: { p: (typeof PROJECTS)[number] }) {
  return (
    <div>
      <div className="eyebrow mb-2">Timeline · reported milestones</div>
      <ol className="space-y-0">
        {p.milestones.map((m, i) => (
          <li key={i} className="grid grid-cols-[14px_1fr_auto] items-start gap-2.5 pb-2.5">
            <span className={cx('mt-1 size-2.5 rounded-full border', m.status === 'done' && 'border-bone bg-bone', m.status === 'pending' && 'border-mute', m.status === 'slipped' && 'border-down bg-down/40', m.status === 'at-risk' && 'border-warn border-dashed')} />
            <div>
              <div className="text-[12px] text-bone">{m.label}</div>
              {m.originally && <div className="text-[11px] text-down">slipped from {m.originally}</div>}
              {m.source && <Source source={m.source} />}
            </div>
            <span className={cx('font-mono text-[10.5px]', m.status === 'slipped' ? 'text-down' : m.status === 'at-risk' ? 'text-warn' : 'text-mute')}>{m.date}</span>
          </li>
        ))}
      </ol>
    </div>
  )
}

export const THEMES: { id: string; label: string; text: string; links: EntityRef[] }[] = [
  { id: 'power-constraints', label: 'Power constraints', text: 'Where grid access, transmission and power prices limit how fast announced capacity becomes usable compute.', links: [ref('energy', 'ercot', 'ERCOT'), ref('energy', 'pjm', 'PJM'), ref('region', 'ie-dub', 'Dublin'), ref('basket', 'b-power', 'Power for AI')] },
  { id: 'neocloud', label: 'Neocloud economics', text: 'GPU rental providers whose margins depend on rental prices, utilisation and delivery timing.', links: [ref('basket', 'b-neo', 'Neocloud basket'), ref('signal', 'sig-neo', 'Neocloud rental softening'), ref('gpu', 'h100', 'H100')] },
  { id: 'blackwell', label: 'Blackwell transition', text: 'B200 deliveries shift demand away from H100, pressuring older-generation rental prices.', links: [ref('gpu', 'b200', 'B200'), ref('gpu', 'h100', 'H100'), ref('signal', 'sig-h100', 'H100 index forecast')] },
]

export const ALL_ENTITIES: EntityRef[] = [
  ...REGIONS.map((r) => ref('region', r.id, r.name)),
  ...GPUS.map((g) => ref('gpu', g.id, g.name)),
  ...['coreweave', 'lambda', 'nebius', 'crusoe', 'aws', 'azure', 'gcp', 'vast', 'runpod'].map((p) => ref('provider', p, providerById(p)!.name)),
  ...PROJECTS.map((p) => ref('project', p.id, p.name)),
  ...['ercot', 'pjm', 'nordpool', 'sem', 'sg', 'de', 'gb', 'nl', 'jepx', 'aemo', 'bpa', 'hq', 'wecc-az'].map((e) => ref('energy', e, energyById(e)!.name)),
  ...['NVDA', 'AMD', 'TSM', 'MSFT', 'AMZN', 'GOOGL', 'ORCL', 'CRWV', 'NBIS', 'IREN', 'APLD', 'EQIX', 'DLR', 'VST', 'CEG', 'NRG'].map((t) => ref('security', t, `${t} · ${secByTicker(t)!.name}`)),
  ...BASKETS.map((b) => ref('basket', b.id, b.name)),
  ...SIGNALS.map((s) => ref('signal', s.id, s.name)),
  ...NEWS.map((n) => ref('news', n.id, n.title)),
  ...PRED.map((p) => ref('news', p.id, p.question)),
  ...THEMES.map((t) => ref('theme', t.id, t.label)),
]

