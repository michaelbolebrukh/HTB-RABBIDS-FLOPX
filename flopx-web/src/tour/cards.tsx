// The animated overlay cards, one per beat id. Every number is read from the live store when the
// card renders (src/tour/data.ts) and shown with the badge the live adapters gave it; a beat whose
// data has not landed says so and the tour continues. Counters count up, bars grow, chips and rows
// stagger in; all of it collapses to the final state under prefers-reduced-motion (motion.ts, CSS).
import type { ReactNode } from 'react'
import { ArrowRight, Check, Send, X } from 'lucide-react'
import { MONTHS, NOW_IDX, type Freshness, type Truth } from '@/data/core'
import { SIGNALS } from '@/data/fund'
import { PROCUREMENT, SNAPSHOT } from '@/data/handoff'
import { useProcurePlan, type ProcureSpec } from '@/live/adapt-w5'
import { fmtObserved } from '@/live/provenance'
import { pressureWord, scenarioImpact } from '@/lib/model'
import type { Store } from '@/lib/store'
import { cx, Fresh, Meter, TruthGlyph, Validation } from '@/components/ui'
import {
  agentToolCount, anomalyFacts, evaluationFacts, factorsOf, feedStatus, forecastFacts, fundTests, globalPressure, liveRegions, nextSteps, pickRegion, plainRegionName, realityRows,
  retrievalCounters, topRegion, transforms, useForecastOutcome, type Fact,
} from './data'
import { PLAN_GPU } from './tours'
import { familySummary, FAMILIES, useSources } from './sources'
import { fmtLike } from './format'
import { useCountUp, useMounted, useStagger } from './motion'
import { AGENT_WAIT_CAP_MS, agentAnswerState, prefetchFailedMs, prefetchWarmMs } from './prefetch'
import { useEffect, useState } from 'react'

// ---------- small pieces ----------

/** The provenance chip. Only the closing card (What is real today) still shows one per row (N-TOUR2 #5). */
function Badge({ f, label }: { f: Freshness; label?: string }) {
  return <Fresh f={f} label={label} className="tour-badge" />
}

/** One status dot per retrieval card: green when the feed is live, amber while it is still reading, red when it failed. */
function Dot({ status, title }: { status: 'live' | 'pending' | 'failed' | 'unavailable'; title: string }) {
  return <span data-tour-dot={status} title={title} className={cx('inline-block size-2 shrink-0 rounded-full', status === 'live' ? 'bg-up pulse-dot' : status === 'pending' ? 'bg-warn pulse-dot' : status === 'failed' ? 'bg-down' : 'bg-dim')} />
}

/**
 * The one muted line at a card's foot naming which of its figures are illustrative (or missing), so
 * no sample number reads as data without a chip on every row. Nothing is rendered when all is real.
 */
function Foot({ items, label = 'Illustrative' }: { items: (string | false | null | undefined)[]; label?: string }) {
  const xs = items.filter((x): x is string => !!x)
  if (!xs.length) return null
  return <p data-tour-foot className="border-t border-line/60 pt-1.5 text-[0.82em] leading-snug text-dim"><span className="text-mute">{label}:</span> {xs.join(' · ')}.</p>
}

const notReal = (f: Freshness) => f === 'illustrative' || f === 'simulated' || f === 'unavailable'

/** A figure; counts up when it carries a number. No chip: the card's foot names what is illustrative. */
function Figure({ f, big, on = true, k, dimWhenIllustrative = true }: { f: Fact; big?: boolean; on?: boolean; k?: string | number; dimWhenIllustrative?: boolean }) {
  const n = useCountUp(on && f.num != null ? f.num : null, 900, k ?? f.label)
  const text = f.num != null && n != null ? fmtLike(f.value, n) : f.value
  const muted = dimWhenIllustrative && notReal(f.fresh)
  return (
    <div className={cx('min-w-0', !on && 'opacity-0')} data-fresh={f.fresh}>
      <div className="flex items-center gap-1.5 text-[0.88em] text-mute">{f.truth && <TruthGlyph truth={f.truth} size={7} />}<span className="truncate">{f.label}</span></div>
      <div className="mt-0.5 flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5"><span className={cx('num', muted ? 'text-mute' : 'text-bone', big ? 'text-[1.9em] leading-none' : 'text-[1.25em] leading-tight')}>{text}</span>{f.unit && <span className="font-mono text-[0.8em] text-dim">{f.unit}</span>}{muted && <span className="font-mono text-[0.72em] uppercase tracking-wider text-dim">{f.fresh === 'unavailable' ? 'n/a' : f.fresh}</span>}</div>
      {f.note && <div className="mt-0.5 text-[0.84em] leading-snug text-dim">{f.note}</div>}
    </div>
  )
}

function Lead({ children }: { children: ReactNode }) {
  return <p className="text-[1.02em] leading-relaxed text-bone/90">{children}</p>
}
function Aside({ children }: { children: ReactNode }) {
  return <p className="text-[0.86em] leading-snug text-mute">{children}</p>
}
function Missing({ what }: { what: string }) {
  return <div className="rounded-[4px] border border-dashed border-line2 px-3 py-2 text-[0.9em] text-mute"><span className="font-mono text-[0.8em] uppercase tracking-wider text-dim">Unavailable</span> <span className="ml-1.5">{what} has not landed from the FLOPX backend. The card shows no figure for it; the tour continues.</span></div>
}

const ago = (iso: string | null, now = Date.now()) => {
  if (!iso) return 'no time stated'
  const m = Math.max(0, Math.round((now - Date.parse(iso)) / 60000))
  return m < 1 ? 'just now' : m < 60 ? `${m} min ago` : m < 60 * 36 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`
}

// ---------- 1 · the invisible market ----------

function SourceFan({ k }: { k: string }) {
  const src = useSources()
  const fams = familySummary(src.rows)
  const shown = useStagger(FAMILIES.length, 240, `${k}:${src.status}`)
  const feed = feedStatus()
  return (
    <div className="space-y-3">
      <Lead>Nobody publishes GPU utilisation or usable capacity. FLOPX reads what <em>is</em> public: advertised asks, power prices, filings, prediction markets, news and facility records, and fans them into one ticker.</Lead>
      {/* Six families in two columns of three (N-TOUR2 #1), fanning down into the one ticker node. */}
      <div className="grid grid-cols-2 gap-x-3 gap-y-1.5">
        {fams.map((f, i) => {
          const live = f.rows.filter((r) => r.freshness !== 'illustrative').length
          return (
            <div key={f.family} className={cx('tour-chip flex items-center justify-between gap-2 rounded-[4px] border border-line bg-ink/60 px-2.5 py-1.5', !live && src.status !== 'pending' && 'border-dashed')} data-on={i < shown} data-fresh={f.freshness}>
              <div className="min-w-0"><div className="text-[0.95em] text-bone">{f.label} <span className="text-dim">· {live || 'no'} live source{live === 1 ? '' : 's'}</span></div><div className="truncate text-[0.8em] text-dim">{src.status === 'pending' ? 'reading freshness blocks' : f.newest ? `last observed ${fmtObserved(f.newest)} UTC · ${ago(f.newest)}` : f.what}</div></div>
            </div>
          )
        })}
      </div>
      <div className="flex items-center gap-3">
        <svg className="tour-fan h-7 min-w-0 flex-1" viewBox="0 0 600 28" preserveAspectRatio="none" aria-hidden>
          {fams.map((f, i) => <path key={f.family} data-on={i < shown} d={`M${50 + i * 100} 0 C ${50 + i * 100} 14, 600 10, 600 28`} fill="none" stroke={f.freshness === 'live' ? 'var(--color-up)' : 'var(--color-dim)'} strokeWidth="1.2" />)}
        </svg>
        <div className="flex items-center gap-2 rounded-[4px] border border-line2 bg-panel px-2.5 py-1.5">
          <Dot status={feed.state.status === 'live' ? 'live' : feed.state.status === 'failed' ? 'failed' : 'pending'} title={`ticker feeds: ${feed.state.status}`} />
          <span className="font-mono text-[0.78em] tracking-wider text-dim">TICKER</span>
          <span className="num text-[1.1em] text-bone">{feed.live}/{feed.total}</span>
          <span className="text-[0.75em] text-dim">feeds live</span>
        </div>
      </div>
      {src.status === 'failed' && <Missing what="The source list (freshness blocks)" />}
      {src.unavailable.length > 0 && <Aside>Named as unavailable by the backend itself: {src.unavailable.map((u) => `${u.source} (${u.reason})`).join(', ')}. A missing source is reported, never filled in.</Aside>}
      <Foot items={[...fams.filter((f) => f.freshness === 'illustrative').map((f) => `${f.label} (FLOPX sample set only, dashed)`), src.status === 'ready' && fams.some((f) => f.freshness === 'unavailable') && `no source at all for ${fams.filter((f) => f.freshness === 'unavailable').map((f) => f.label).join(', ')}`]} />
    </div>
  )
}

// ---------- 2 · retrieval ----------

function Counters({ k }: { k: string }) {
  const src = useSources() // re-render when the payload totals land
  const facts = retrievalCounters()
  const shown = useStagger(facts.length, 160, k)
  const feed = feedStatus()
  const status = src.status === 'failed' && feed.state.status === 'failed' ? 'failed' : src.status === 'ready' || feed.state.status === 'live' ? 'live' : 'pending'
  return (
    <div className="space-y-3">
      <div className="flex items-start gap-2"><span className="pt-1.5"><Dot status={status} title={`payload reads: ${status}`} /></span><Lead>What landed from the backend in this session, counted in the payloads themselves.</Lead></div>
      <div className="grid grid-cols-3 gap-x-4 gap-y-2.5">
        {facts.map((f, i) => <Figure key={f.label} f={f} on={i < shown} k={`${k}:${i}`} />)}
      </div>
      <Foot items={[...facts.filter((f) => f.fresh === 'illustrative').map((f) => `${f.label} (${f.note ?? 'sample data'})`), ...facts.filter((f) => f.fresh === 'unavailable').map((f) => `${f.label} not landed (${f.note ?? 'missing'})`), 'a sample row counts as illustrative here and everywhere, never as live']} />
    </div>
  )
}

function SourceTable({ k }: { k: string }) {
  const src = useSources()
  const rows = src.rows.slice(0, 12)
  const shown = useStagger(rows.length, 110, `${k}:${src.status}`)
  const sample = rows.filter((r) => r.kind === 'sample')
  const licence = (r: (typeof rows)[number]) => (r.licence ? r.licence.replace(/\s*\(terms asserted, not verified\)\s*$/, '').slice(0, 40) + (r.licence.length > 40 ? '…' : '') : r.kind === 'sample' ? 'authored by FLOPX' : 'licence not stated')
  return (
    <div className="space-y-2.5">
      <div className="flex items-start gap-2"><span className="pt-1.5"><Dot status={src.status === 'ready' ? 'live' : src.status === 'failed' ? 'failed' : 'pending'} title={`freshness blocks: ${src.status}`} /></span><Lead>Every source FLOPX read for this picture, with the licence it is used under and when it was last observed.</Lead></div>
      {src.status === 'pending' && <Aside>Reading the backend's freshness blocks (six payloads).</Aside>}
      {src.status === 'failed' && <Missing what="The source list" />}
      {/* Two columns of compact rows (N-TOUR2 #1): no inner scroller, the whole list stays on the card. */}
      <div className="grid grid-cols-2 gap-x-5 text-[0.86em]">
        {rows.map((r, i) => (
          <div key={r.id} className="tour-chip flex items-baseline justify-between gap-2 border-t border-line/60 py-1" data-on={i < shown} data-fresh={r.freshness}>
            <div className="min-w-0">
              <div className={cx('truncate', r.kind === 'sample' ? 'text-mute' : 'text-bone')}>{r.label}</div>
              <div className="truncate text-[0.9em] text-dim" title={r.licence ?? undefined}>{FAMILIES.find((f) => f.id === r.family)?.label} · {licence(r)}{r.licence?.includes('asserted') && ' · asserted'}</div>
            </div>
            <div className="num shrink-0 text-right text-dim">{r.observedAt ? fmtObserved(r.observedAt) : r.kind === 'sample' ? 'authored' : 'no time'}</div>
          </div>
        ))}
      </div>
      {src.unavailable.length > 0 && <Aside>Unavailable and said so: {src.unavailable.map((u) => `${u.source} (${u.reason})`).join(', ')}.</Aside>}
      {src.generatedAt && <Aside>Newest payload generated {fmtObserved(src.generatedAt)} UTC{src.rows.length > rows.length ? ` · ${src.rows.length - rows.length} more sources not listed here` : ''}.</Aside>}
      <Foot items={sample.map((r) => `${r.label} (authored, not observed)`)} />
    </div>
  )
}

// ---------- 3 · analysis ----------

function ProvenanceFlow({ k }: { k: string }) {
  const ts = transforms()
  const shown = useStagger(ts.length, 700, k)
  return (
    <div className="space-y-2.5">
      <Lead>Every number belongs to one of the four classes lit on the page, and a raw observation never becomes an inference without the step being shown.</Lead>
      {ts.length === 0 && <Missing what="A live ask, filing or market" />}
      {/* One transformation per column, raw above derived (N-TOUR2 #1: three across at 1440 instead of three tall rows). */}
      <div className={cx('grid gap-2', ts.length === 1 ? 'grid-cols-1' : ts.length === 2 ? 'grid-cols-2' : 'grid-cols-3')}>
        {ts.map((t, i) => (
          <div key={t.id} className={cx('flex flex-col gap-1.5 rounded-[4px] border border-line bg-ink/60 p-2.5', i >= shown && 'opacity-0')}>
            <Figure f={{ ...t.from, note: undefined }} on={i < shown} k={`${k}:${t.id}:a`} />
            <div className="tour-flow-arrow flex items-center gap-1.5 text-fct" data-on={i < shown}><ArrowRight size={14} className="shrink-0 rotate-90" /><span className="text-[0.78em] leading-snug text-dim">{t.how}</span></div>
            <Figure f={{ ...t.to, note: undefined }} on={i < shown} k={`${k}:${t.id}:b`} />
          </div>
        ))}
      </div>
      <Foot items={ts.flatMap((t) => [t.from, t.to]).filter((f) => f.fresh === 'illustrative').map((f) => f.label)} />
    </div>
  )
}

// ---------- 4 · modelling ----------

function FactorCompose({ k }: { k: string }) {
  const pick = pickRegion()
  const f = factorsOf(pick.region)
  const on = useMounted(k)
  const shown = useStagger(f?.factors.length ?? 0, 260, k)
  const score = useCountUp(f && shown >= (f.factors.length || 1) ? f.score : null, 1100, `${k}:score`)
  if (!f) return <div className="space-y-3"><Lead>The X-Ray's pressure score is cpx-v1: six factors, each weighted, composed into one 0 to 100 number per region and GPU.</Lead><Missing what="The pressure feed" /><Aside data-tour-region="">Region: {pick.why}.</Aside></div>
  const total = f.factors.reduce((s, x) => s + Math.abs(x.contribution ?? 0), 0) || 1
  const sampleFed = f.factors.filter((x) => x.contribution != null && x.fresh === 'illustrative')
  const noData = f.factors.filter((x) => x.contribution == null)
  return (
    <div className="space-y-3">
      <Lead><span className="text-inf">{plainRegionName(f.name)}</span>, H100 SXM cell: six cpx-v1 factors compose into one score. Weights are the model's; contributions are what each factor added tonight.</Lead>
      {/* Which region, and why, in one line (N-TOUR2 #2). */}
      <p data-tour-region={f.region.live?.backendId ?? f.region.id} className="text-[0.84em] leading-snug text-mute">Region chosen: <span className="text-bone">{plainRegionName(f.name)}</span>, because {pick.why}.</p>
      {/* Six factors in two columns of three (N-TOUR2 #1). */}
      <div className="grid grid-cols-2 gap-x-5 gap-y-2">
        {f.factors.map((x, i) => {
          const share = Math.abs(x.contribution ?? 0) / total
          return (
            <div key={x.id} className={cx('grid grid-cols-[1fr_44px_52px] items-center gap-2', i >= shown && 'opacity-30')} data-fresh={x.contribution == null ? 'unavailable' : x.fresh}>
              <div className="min-w-0">
                <div className="truncate text-[0.9em] text-bone">{x.label}</div>
                <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-line"><div className={cx('tour-bar h-full rounded-full', x.fresh === 'illustrative' ? 'hatch-inf border border-inf/60' : 'bg-inf')} data-on={on && i < shown} style={{ '--w': `${Math.max(2, share * 100)}%` } as React.CSSProperties} /></div>
              </div>
              <div className="num text-right text-[0.85em] text-dim" title="weight">w {x.weight.toFixed(2)}</div>
              <div className={cx('num text-right text-[0.85em]', x.contribution == null ? 'text-dim' : 'text-mute')} title="contribution">{x.contribution == null ? 'no data' : `${x.contribution >= 0 ? '+' : ''}${x.contribution.toFixed(1)}`}</div>
            </div>
          )
        })}
      </div>
      <div className="flex items-center gap-4 rounded-[4px] border border-inf/30 bg-inf/[0.05] px-3 py-2">
        <span className="font-mono text-[0.8em] text-dim">=</span>
        <span className="num text-[2em] leading-none text-inf">{score == null ? '—' : score.toFixed(0)}</span>
        <div className="min-w-0 flex-1 text-[0.86em] text-mute">{pressureWord(f.score)} · {f.label} · {Math.round(f.realShare * 100)}% of inputs real · {f.factorsWithData} of {f.factorsTotal} factors with data · as of {f.asOf}</div>
      </div>
      <Aside>It infers pressure; it does not measure utilisation. "Inputs real" is the share of factor inputs that are not FLOPX sample data; "with data" counts factors that have any input at all. Sample-fed factors are hatched and keep the region exploratory.</Aside>
      <Foot items={[sampleFed.length > 0 && `sample-fed factors (hatched): ${sampleFed.map((x) => x.label).join(', ')}`, noData.length > 0 && `no data at all: ${noData.map((x) => x.label).join(', ')}`, f.fresh === 'illustrative' && 'the composed score itself reads illustrative']} />
    </div>
  )
}

function RevealCard({ store, k }: { store: Store; k: string }) {
  const reveal = store.xray.reveal
  const g = globalPressure()
  const regions = liveRegions()
  const obs = Math.round(25 + 0.75 * (100 - reveal))
  return (
    <div className="space-y-3">
      <Lead>Raw layers first: asks, power, pipeline pins. Then the tour moves the reveal slider and the inferred pressure glows through them.</Lead>
      <div className="grid grid-cols-2 gap-3">
        <div className="rounded-[4px] border border-line bg-ink/60 p-2.5"><div className="flex items-center gap-1.5 text-[0.85em] text-mute"><TruthGlyph truth="obs" size={7} />Observed layers</div><div className="num text-[1.7em] text-bone">{obs}%</div></div>
        <div className="rounded-[4px] border border-inf/30 bg-inf/[0.05] p-2.5"><div className="flex items-center gap-1.5 text-[0.85em] text-mute"><TruthGlyph truth="inf" size={7} />Inferred pressure</div><div className="num text-[1.7em] text-inf">{reveal}%</div></div>
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-line"><div className="h-full rounded-full bg-inf" style={{ width: `${reveal}%`, transition: 'width 0.12s linear' }} /></div>
      <Figure f={g} k={`${k}:g`} />
      <Aside>{regions.length ? `${regions.length} regions carry a FLOPX score tonight; the others keep the design's values. Hatching marks inferred values, rings show uncertainty.` : 'No region carries a FLOPX score yet; the map shows the design values.'}</Aside>
      <Foot items={[g.fresh === 'illustrative' && `the pressure mean (${g.note})`, regions.length ? 'regions without a FLOPX score on the map (design values)' : 'every region on the map (design values)']} />
    </div>
  )
}

// ---------- 5 · estimation and honesty ----------

/** Body copy 13 px and figures 15 px at scale 1 (Present 1.25), one idea per line, at most six rows (N-TOUR2 #4). */
const V_BODY: React.CSSProperties = { fontSize: 'calc(13px * var(--tour-scale, 1))' }
const V_FIG: React.CSSProperties = { fontSize: 'calc(15px * var(--tour-scale, 1))' }

function Verdicts({ k, lead }: { k: string; lead: ReactNode }) {
  const ev = evaluationFacts()
  const ft = fundTests()
  const fc = forecastFacts(useForecastOutcome())
  const on = useMounted(k)
  const shownTests = useStagger(ft.tests, 45, k)
  const n = ev ? ev.improves + ev.inconclusive + ev.worsens + ev.insufficient : 0
  return (
    <div className="space-y-2.5 leading-snug" style={V_BODY} data-tour-verdicts>
      <p className="text-bone/90">{lead}</p>
      <div className="grid grid-cols-2 gap-3">
        <div className="rounded-[4px] border border-line bg-ink/60 p-2.5">
          <div className="text-bone">Does compute data improve the 1M forecast?</div>
          {ev ? (
            <>
              <div className="mt-2 flex h-2.5 w-full overflow-hidden rounded-sm bg-line">
                {([['improves', ev.improves, 'var(--color-up)'], ['inconclusive', ev.inconclusive, 'var(--color-dim)'], ['worsens', ev.worsens, 'var(--color-down)'], ['insufficient', ev.insufficient, 'var(--color-line2)']] as const).map(([id, v, c]) => <div key={id} className="tour-bar h-full" data-on={on} style={{ '--w': `${n ? (v / n) * 100 : 0}%`, background: c } as React.CSSProperties} title={`${id}: ${v}`} />)}
              </div>
              <div className="num mt-1.5 flex flex-wrap gap-x-3 text-mute" style={V_FIG}><span><span className="text-up">{ev.improves}</span> improve</span><span>{ev.inconclusive} inconclusive</span><span><span className="text-down">{ev.worsens}</span> worsen</span>{ev.insufficient > 0 && <span>{ev.insufficient} too few</span>}</div>
              <div className="mt-1 text-dim">of {ev.tickers} tickers, out of sample, {ev.basis.caption}</div>
            </>
          ) : <div className="mt-1.5 text-mute">evaluation not loaded</div>}
        </div>
        <div className="rounded-[4px] border border-warn/30 bg-warn/[0.04] p-2.5">
          <div className="text-warn" style={V_FIG}>Edge in <span className="num">{ft.edges}</span> of <span className="num">{ft.tests}</span> pre-registered fund tests</div>
          <div className="mt-2 grid grid-cols-12 gap-1">{Array.from({ length: ft.tests }, (_, i) => <span key={i} className={cx('h-2.5 rounded-[2px] border border-warn/50 transition-opacity', i < shownTests ? 'opacity-100' : 'opacity-0', i < ft.edges && 'bg-warn')} />)}</div>
          <div className="mt-1.5 text-mute">{ft.tickers} tickers · {ft.exploratory} half-size paper orders kept exploratory</div>
          <div className="mt-1 text-dim">"no edge found" is published, not hidden</div>
        </div>
      </div>
      <div className="flex items-center gap-3 rounded-[4px] border border-line bg-ink/60 p-2.5">
        <div className="min-w-0 flex-1"><div className="text-bone">30-day H100 index forecast</div><div className="text-mute">{fc.headline}</div></div>
        <div className="w-32"><Meter value={fc.have} max={fc.need} color="var(--color-cat2)" /><div className="num mt-0.5 text-right text-dim" style={V_FIG}>{fc.have} / {fc.need} points</div></div>
      </div>
      <Foot items={[ev && ev.basis.kind === 'sample' && `the harness scores sample closes (${ev.basis.label.toLowerCase()}), so its verdicts are about a sample, not the market`, !ev && 'the evaluation harness has not loaded', `the fund tests are the ${SNAPSHOT.short} hand-off figures`]} />
    </div>
  )
}

function ForecastCard({ k }: { k: string }) {
  const fc = forecastFacts(useForecastOutcome())
  const on = useMounted(`${k}:${fc.state}`)
  return (
    <div className="space-y-3">
      <Lead>The timing gate asks the forecast whether it may time a purchase. Tonight the forecast answers for itself.</Lead>
      <div className={cx('rounded-[4px] border p-3', fc.state === 'refused' ? 'border-down/40 bg-down/[0.04]' : 'border-line bg-ink/60')}>
        <div className="flex items-center justify-between gap-2"><span className={cx('text-[1.05em] font-semibold', fc.state === 'refused' ? 'text-down' : 'text-bone')}>{fc.headline}</span><span className="font-mono text-[0.75em] uppercase tracking-wider text-dim">{fc.state === 'pending' ? 'asking' : fc.state === 'ok' ? 'live' : 'live refusal'}</span></div>
        <div className="mt-1 font-mono text-[0.82em] text-mute">{fc.detail}</div>
        <div className="mt-3 h-2 w-full overflow-hidden rounded-full bg-line"><div className="tour-bar h-full rounded-full bg-cat2" data-on={on} style={{ '--w': `${Math.min(100, (fc.have / Math.max(1, fc.need)) * 100)}%` } as React.CSSProperties} /></div>
        <div className="num mt-1 flex justify-between text-[0.82em] text-dim"><span>{fc.have} daily points of history</span><span>{fc.need} needed (twice the horizon)</span></div>
      </div>
      <div className="grid grid-cols-2 gap-3 text-[0.88em]">
        <div className="rounded-[4px] border border-line p-2.5"><div className="text-mute">Gate</div><div className={cx('num text-[1.3em]', fc.state === 'ok' ? 'text-up' : 'text-down')}>{fc.state === 'ok' ? 'Open' : 'Closed'}</div><div className="text-[0.85em] text-dim">Flexible costs the same as run now</div></div>
        <div className="rounded-[4px] border border-line p-2.5"><div className="text-mute">Uncertainty</div><div className="num text-[1.3em] text-bone">{fc.state === 'ok' ? '80% band' : 'none issued'}</div><div className="text-[0.85em] text-dim">{fc.state === 'ok' ? 'shown as p10 to p90 on the page' : 'a refusal has no band to show'}</div></div>
      </div>
      <Aside>The saving on this desk comes from search and feasibility across venues, not from timing the market. That claim is on the never-claim list until the gate opens.</Aside>
      <Foot items={[fc.state === 'failed' && 'no forecast figure: the endpoint did not answer, nothing stands in for it']} />
    </div>
  )
}

// ---------- 6 · decision ----------

function AnomalyCard({ k }: { k: string }) {
  const a = anomalyFacts()
  const r = topRegion()
  const shown = useStagger(3, 300, k)
  const p = r?.live?.pressure
  return (
    <div className="space-y-3">
      <Lead>The anomaly scan ranks what changed, with its evidence, its confidence and what it affects, so a reader can disagree with it.</Lead>
      {a.live && a.first ? (
        <div className="rounded-[4px] border border-inf/30 bg-inf/[0.05] p-2.5">
          <div className="flex items-center justify-between gap-2"><span className="text-[1.02em] font-semibold text-inf">{a.first.title}</span></div>
          <div className="mt-0.5 text-[0.88em] text-mute">{a.first.sub}</div>
          <div className="mt-1.5 flex items-center gap-2 text-[0.84em] text-dim"><span>severity</span><Meter value={a.first.severity} max={1} color="var(--color-inf)" className="w-24" /><span className="num">{a.first.severity.toFixed(2)}</span><span>· {a.cards.length} open</span></div>
        </div>
      ) : <Missing what="The anomaly scan" />}
      <div className="grid grid-cols-3 gap-2 text-[0.86em]">
        {[['What changed', 'obs', 'the observations, each with source and time'], ['Why it might have', 'inf', 'hypotheses ranked by support; conflicts shown'], ['What it affects', 'dec', 'documented ties and modelled sensitivities']].map(([t, tr, d], i) => (
          <div key={t} className={cx('tour-chip rounded-[4px] border border-line p-2', i < shown && '')} data-on={i < shown}><div className="flex items-center gap-1.5 text-bone"><TruthGlyph truth={tr as Truth} size={7} />{t}</div><div className="text-[0.88em] text-dim">{d}</div></div>
        ))}
      </div>
      {r && p && <Aside>{plainRegionName(r.name)} reads {p.score.toFixed(0)} ({pressureWord(p.score)}, {p.label}).</Aside>}
      <Foot items={[!a.live && 'the anomaly scan has not loaded; no anomaly stands in for it', 'the design\u2019s Texas anomaly narrative, wherever it still appears on the page']} />
    </div>
  )
}

function ScenarioCard({ store, k }: { store: Store; k: string }) {
  const r = topRegion() ?? null
  const sc = store.xray.scenario
  const on = useMounted(k)
  if (!r || !sc) return <div className="space-y-3"><Lead>A what-if changes only estimated quantities.</Lead><Missing what="A scored region" /></div>
  const imp = scenarioImpact(r, sc)
  const rows: [string, string, string][] = [
    [`Usable additions by ${MONTHS[Math.min(38, NOW_IDX + 12)]}`, `${r.eff27[0]} to ${r.eff27[1]} MW`, `${imp.eff[0]} to ${imp.eff[1]} MW`],
    ['Pressure, +6 months', `${(r.pressure[Math.min(38, NOW_IDX + 6)] ?? 0).toFixed(0)}`, `${((r.pressure[Math.min(38, NOW_IDX + 6)] ?? 0) + imp.d6).toFixed(0)} (+${imp.d6.toFixed(1)})`],
    ['H100 premium vs global', `${r.premium >= 0 ? '+' : ''}${r.premium.toFixed(0)}%`, `${imp.premiumAfter >= 0 ? '+' : ''}${imp.premiumAfter.toFixed(0)}%`],
  ]
  return (
    <div className="space-y-3">
      <Lead>What if {plainRegionName(r.name)}'s grid upgrade slips {sc.slipMonths} months? The scenario touches only estimates, lists its assumptions and is labelled hypothetical.</Lead>
      <div className="rounded-[4px] border border-dashed border-dec/50 bg-dec/[0.06] p-2.5 font-mono text-[0.84em] text-dec">WHAT-IF · {r.short} grid upgrade +{sc.slipMonths}mo · hypothetical</div>
      <div className="space-y-1.5 text-[0.9em]">
        <div className="grid grid-cols-[1fr_auto_16px_auto] gap-2 text-[0.85em] text-dim"><span>Estimated quantity</span><span className="text-right">Base</span><span /><span className="text-right">Scenario</span></div>
        {rows.map(([l, a, b], i) => <div key={l} className={cx('grid grid-cols-[1fr_auto_16px_auto] items-center gap-2 border-t border-line/60 pt-1.5 transition-opacity duration-300', on ? 'opacity-100' : 'opacity-0')} style={{ transitionDelay: `${i * 160}ms` }}><span className="text-mute">{l}</span><span className="num text-right text-bone">{a}</span><ArrowRight size={11} className="text-dec" /><span className="num text-right text-dec">{b}</span></div>)}
      </div>
      <Aside>The observed quantities (asks, power prices, filings) do not move. The scenario multiplies the region's stated slip sensitivity ({r.slipSensitivity.toLocaleString('en-GB')} MW) and widens uncertainty 25%.{r.live?.supply ? ' Supply figures come from FLOPX facility records.' : ''}</Aside>
      <Foot items={['every scenario column (hypothetical)', !r.live?.supply && 'the base supply figures (usable MW, premium) are the design\u2019s, not FLOPX records', r.live?.supply && r.live.supply.freshness === 'illustrative' && 'the supply figures read illustrative in the facility records']} />
    </div>
  )
}

function ExposureCard({ k }: { k: string }) {
  const r = topRegion()
  const shown = useStagger((r?.documented.length ?? 0) + (r?.sensitivities.length ?? 0), 220, k)
  if (!r) return <div className="space-y-3"><Lead>Who is exposed to a region's pressure.</Lead><Missing what="A scored region" /></div>
  const f: Freshness = r.live?.exposure?.freshness ?? 'illustrative'
  return (
    <div className="space-y-3">
      <Lead>From pressure to companies: documented ties are facts with a source; modelled sensitivities are associations, drawn dashed and never shown as causal.</Lead>
      <div className="grid grid-cols-2 gap-3 text-[0.88em]">
        <div><div className="mb-1 flex items-center gap-2 text-dim"><span className="inline-block h-px w-4 bg-bone" />Documented ties</div>{r.documented.length ? r.documented.map((d, i) => <div key={d.t + d.tie} className="tour-chip border-t border-line/60 py-1" data-on={i < shown}><span className="font-mono text-bone">{d.t}</span> <span className="text-mute">{d.tie}</span><div className="text-[0.84em] text-dim">{d.source}</div></div>) : <div className="text-dim">none recorded for this region</div>}</div>
        <div><div className="mb-1 flex items-center gap-2 text-dim"><span className="inline-block w-4 border-t border-dashed border-inf" />Modelled sensitivities</div>{r.sensitivities.length ? r.sensitivities.map((s, i) => <div key={s.t} className="tour-chip flex items-center justify-between border-t border-line/60 py-1" data-on={r.documented.length + i < shown}><span className="font-mono text-bone">{s.t}</span><span className="num text-mute">β {s.beta.toFixed(2)} · R² {Number.isFinite(s.r2) ? s.r2.toFixed(2) : 'n/a'}</span></div>) : <div className="text-dim">none estimated</div>}</div>
      </div>
      <Aside>{r.live?.exposure ? `FLOPX exposure-v1 linked these companies to ${plainRegionName(r.name)}.` : `No FLOPX exposure record for ${plainRegionName(r.name)}.`} {r.sensitivities.some((x) => !Number.isFinite(x.r2)) && 'R² reads n/a because the backend states each sensitivity as an authored estimate per profile role, not a fitted regression, so no fit statistic exists.'} The fund treats a documented tie and a modelled β differently when it sizes a position.</Aside>
      <Foot items={[f === 'illustrative' && 'every row on this card (the design\u2019s worked example; no FLOPX exposure record for this region)', f !== 'illustrative' && r.sensitivities.length > 0 && 'the modelled sensitivities are authored estimates, not fitted']} />
    </div>
  )
}

/** The buyer's job: 8 × H200 for a week, the GPU the procurement script and the page's hand-off snapshot both name (N-TOUR2 #3). */
export const PLAN_SPEC: ProcureSpec = { gpu: PLAN_GPU, n: 8, weeks: 1, deadlineDays: 30, area: 'Any', ib: false, minRel: 80, budgetK: 1500 }
export const PLAN_GPU_NAME = 'H200'
/** The illustrative stand-in when the live planner has not answered: the hand-off snapshot's own H200 plan, never another GPU's. */
export const PLAN_FALLBACK = PROCUREMENT.plans.find((x) => x.recommended) ?? PROCUREMENT.plans[0]

function PlanCard({ k }: { k: string }) {
  const p = useProcurePlan(PLAN_SPEC)
  const plan = p.status === 'live' ? p.plan : null
  const best = plan?.options[0]
  const live = !!(plan && best)
  const n = useCountUp(live ? best!.total : p.status === 'failed' ? PLAN_FALLBACK.cost : null, 900, `${k}:${best?.id ?? p.status}`)
  return (
    <div className="space-y-3">
      <Lead>The same evidence, read by a buyer: 8 × {PLAN_GPU_NAME} for a week. FLOPX ranks the asks that can actually run it and says which venue is cheapest, feasibly.</Lead>
      {live ? (
        <div className="rounded-[4px] border border-line bg-ink/60 p-2.5" data-plan="live">
          <div className="text-[0.9em] text-mute">Cheapest feasible ask, live from the planner</div>
          <div className="mt-0.5 text-[1.05em] text-bone"><span className="font-semibold">{best!.providerName}</span> · {best!.regionName}</div>
          <div className="mt-0.5 flex items-baseline gap-2"><span className="num text-[1.8em] leading-none text-bone">${(n ?? 0).toFixed(0)}</span><span className="text-[0.85em] text-dim">expected total · ${best!.price.toFixed(2)}/GPU-hr · {best!.contract} · observed {best!.observed}</span></div>
          <div className="mt-1 text-[0.84em] text-dim">{plan!.options.length} asks can run the job · tier {plan!.tier} · planner {plan!.asOf} UTC{p.gate ? ` · timing gate ${p.gate.open ? 'open' : 'closed'}` : ''}</div>
        </div>
      ) : p.status === 'failed' ? (
        <div className="rounded-[4px] border border-dashed border-line2 bg-ink/40 p-2.5" data-plan="snapshot">
          <div className="text-[0.9em] text-mute">The planner did not answer; the hand-off example for the same GPU stands in</div>
          <div className="mt-0.5 text-[1.05em] text-mute"><span className="font-semibold">{PLAN_FALLBACK.label}</span> · {PLAN_FALLBACK.note}</div>
          <div className="mt-0.5 flex items-baseline gap-2"><span className="num text-[1.8em] leading-none text-mute">${(n ?? 0).toFixed(0)}</span><span className="text-[0.85em] text-dim">expected total · {PROCUREMENT.job} · {SNAPSHOT.short}</span></div>
        </div>
      ) : <Aside><span className="pulse-dot mr-1.5 inline-block size-1.5 rounded-full bg-fct" />Asking the FLOPX planner (POST /flopx/procure/plan) for {PLAN_GPU_NAME} asks.</Aside>}
      <Aside>Nothing is reserved until a person confirms a quote; a request is logged as "requested, not reserved". Rental recommendations never create a fund position.</Aside>
      <Foot items={[!live && p.status === 'failed' && `every figure above (the ${SNAPSHOT.short} ${PLAN_GPU_NAME} example, not a live plan)`, live && best!.freshness === 'illustrative' && 'the ranked ask comes from the FLOPX sample tier, not an observed listing']} />
    </div>
  )
}

function SignalCard({ k }: { k: string }) {
  const s = SIGNALS.find((x) => x.id === 'sig-ercot') ?? SIGNALS[0]
  const shown = useStagger(4, 260, k)
  return (
    <div className="space-y-3">
      <Lead>A signal is a chain, not a number: features with their class, a forecast distribution per horizon, a validation status per horizon, and only then a proposal.</Lead>
      <div className="rounded-[4px] border border-line bg-ink/60 p-2.5">
        <div className="flex items-center justify-between gap-2"><span className="text-[1.02em] font-semibold text-bone">{s.name}</span><span className="flex items-center gap-2"><span className={cx('text-[0.9em]', s.direction === 'Long' ? 'text-up' : s.direction === 'Short' ? 'text-down' : 'text-dim')}>{s.direction} {s.asset}</span></span></div>
        <div className="mt-2 grid grid-cols-3 gap-2 text-[0.86em]">{(['1W', '1M', '3M'] as const).map((h, i) => <div key={h} className="tour-chip rounded-[4px] border border-line p-2" data-on={i < shown}><div className="flex items-center justify-between text-dim"><span>{h}</span><span className="num">{s.horizons[h].mean >= 0 ? '+' : ''}{s.horizons[h].mean.toFixed(1)}%</span></div><div className="num text-[0.85em] text-dim">[{s.horizons[h].p10}, {s.horizons[h].p90}] · n {s.horizons[h].n}</div><Validation s={s.horizons[h].status} short className="mt-1" /></div>)}</div>
      </div>
      <Aside>FLOPX's own evaluation finds no validated edge yet, so no signal here is sized as if it had one.</Aside>
      <Foot items={['every figure on this card (the design\u2019s worked example signal, not a FLOPX forecast)']} />
    </div>
  )
}

function SendCard({ store, k }: { store: Store; k: string }) {
  const open = store.proposals.filter((p) => p.state === 'open')
  const p = open[0] ?? store.proposals[0]
  const sent = store.proposals.filter((x) => x.state === 'sent').length
  const shown = useStagger(4, 300, `${k}:${sent}`)
  const steps: [string, Truth, string][] = [['Signal', 'fct', 'validated at 1M, otherwise position capped'], ['Proposal', 'dec', `${open.length} awaiting a decision`], ['Send', 'dec', 'a person presses it; Reject is logged too'], ['Paper fill', 'obs', 'about 1.4 s · queued when the market is closed · rejected on a limit breach']]
  return (
    <div className="space-y-3">
      <Lead>Nothing executes on its own. The model proposes a target change; a person sends, modifies or rejects it, and every override is logged for attribution.</Lead>
      <div className="flex items-stretch gap-1.5">{steps.map(([t, tr, d], i) => <div key={t} className="tour-chip flex min-w-0 flex-1 flex-col rounded-[4px] border border-line bg-ink/60 p-2" data-on={i < shown}><div className="flex items-center gap-1.5 text-[0.9em] text-bone"><TruthGlyph truth={tr} size={7} />{t}</div><div className="text-[0.8em] leading-snug text-dim">{d}</div></div>)}</div>
      {p && <div className="flex items-center justify-between gap-2 rounded-[4px] border border-dec/30 bg-dec/[0.05] p-2.5 text-[0.9em]"><div><span className="font-mono text-[0.85em] text-dim">Proposal {p.id.toUpperCase()}</span> <span className={p.side === 'Buy' || p.side === 'Cover' ? 'text-up' : 'text-down'}>{p.side}</span> <span className="num text-bone">{(p.modifiedQty ?? p.qty).toLocaleString('en-GB')}</span> <span className="font-mono text-bone">{p.ticker}</span> · {p.currentW.toFixed(1)}% to {p.targetW.toFixed(1)}%</div></div>}
      <Aside>Press <span className="inline-flex items-center gap-1 text-bone"><Send size={10} /> Send to paper</span> on the spotlighted card to watch the order route, fill and appear under Recent orders. {sent > 0 && <span className="text-up">{sent} sent so far in this session.</span>}</Aside>
      <Foot items={['the proposal, its weights and the paper fill (the design\u2019s worked example; the fill is simulated at last price plus 3 bps)']} />
    </div>
  )
}

function AttributionCard({ store, k }: { store: Store; k: string }) {
  const filled = store.orders.filter((o) => o.status === 'Filled' && o.origin === 'Model')
  const shown = useStagger(Math.min(3, filled.length), 240, `${k}:${filled.length}`)
  return (
    <div className="space-y-3">
      <Lead>Every paper fill is attributed to the signal that proposed it, so a later evaluation can ask which information actually paid.</Lead>
      {filled.length ? filled.slice(0, 3).map((o, i) => <div key={o.id} className="tour-chip flex items-center justify-between gap-2 rounded-[4px] border border-line bg-ink/60 px-2.5 py-1.5 text-[0.9em]" data-on={i < shown}><span><span className="font-mono text-[0.85em] text-dim">{o.id}</span> <span className={o.side === 'Buy' || o.side === 'Cover' ? 'text-up' : 'text-down'}>{o.side}</span> <span className="num text-bone">{o.qty.toLocaleString('en-GB')}</span> <span className="font-mono text-bone">{o.ticker}</span>{o.fillPx != null && <span className="num text-mute"> @ ${o.fillPx.toFixed(2)}</span>}</span><span className="flex items-center gap-2 text-[0.84em] text-dim">{o.signal ?? 'user'} · {o.time}</span></div>) : <Missing what="A filled paper order" />}
      <Aside>The attribution table below the proposals shows realised P&amp;L by signal.</Aside>
      <Foot items={['every fill above is simulated (last price plus 3 bps)', 'the attribution table and its P&L are the design\u2019s worked example']} />
    </div>
  )
}

function LoopCard({ store, k }: { store: Store; k: string }) {
  const a = anomalyFacts()
  const open = store.proposals.filter((p) => p.state === 'open').length
  const shown = useStagger(6, 280, k)
  const nodes: [string, Truth, string, Freshness][] = [
    ['Anomaly', 'inf', a.live && a.first ? a.first.title : 'scan not loaded', a.live ? 'live' : 'unavailable'],
    ['Signal', 'fct', 'feature chain, per-horizon validation', 'illustrative'],
    ['Proposal', 'dec', `${open} open`, 'illustrative'],
    ['Send', 'dec', 'a person decides', 'simulated'],
    ['Paper fill', 'obs', 'routed, filled, queued or rejected', 'simulated'],
    ['Attribution', 'obs', 'P&L back to the signal', 'illustrative'],
  ]
  return (
    <div className="space-y-3">
      <Lead>The brief is where the loop closes: what changed, what it affects, what the desk proposes, what a person decided, and what the outcome was attributed to.</Lead>
      <div className="grid grid-cols-3 gap-1.5">{nodes.map(([t, tr, d, f], i) => <div key={t} className="tour-chip flex flex-col rounded-[4px] border border-line bg-ink/60 p-2" data-on={i < shown}><div className="flex items-center justify-between gap-1.5 text-[0.9em] text-bone"><span className="flex items-center gap-1.5"><span className="num text-[0.8em] text-dim">{i + 1}</span><TruthGlyph truth={tr} size={7} />{t}</span><ArrowRight size={10} className={cx('text-dim', i === nodes.length - 1 && 'invisible')} /></div><div className="mt-0.5 text-[0.82em] leading-snug text-dim">{d}</div><div className={cx('mt-auto pt-1.5 font-mono text-[0.72em] uppercase tracking-wider', f === 'live' ? 'text-up' : 'text-dim')} data-fresh={f}>{f === 'live' ? 'real tonight' : f === 'unavailable' ? 'not loaded' : f}</div></div>)}</div>
      <Aside>Real tonight: the anomaly scan and the pressure it reads.</Aside>
      <Foot items={[`${nodes.filter(([, , , f]) => f !== 'live' && f !== 'unavailable').map(([t]) => t).join(', ')} (the fund\u2019s worked example; Send and the fill are simulated)`]} />
    </div>
  )
}

type AgentCardState = 'pending' | 'landed' | 'offline' | 'timeout'

/**
 * The page's own answer state, polled: pending, landed (with the seconds it took), offline (the page
 * fell back to its prepared card because the backend did not answer), or past the wait cap. `warmMs`
 * is how long a successful early request took; `failedMs` how long a failed one ran (WF5 #2: a time
 * to failure is never shown as "the backend took").
 */
export function agentCardState(page: ReturnType<typeof agentAnswerState>, cur: AgentCardState, secs: number): AgentCardState {
  if (cur === 'landed' || cur === 'offline') return cur
  if (page === 'landed') return 'landed'
  if (page === 'offline') return 'offline'
  // While the page is still asking (even after the tour's early request failed) keep waiting for the
  // page's own request, up to the cap; the pending copy says when the early one failed.
  return secs * 1000 >= AGENT_WAIT_CAP_MS ? 'timeout' : 'pending'
}

function useAgentAnswer(k: string): { state: AgentCardState; seconds: number; warmMs: number | null; failedMs: number | null } {
  const [st, setSt] = useState<{ state: AgentCardState; seconds: number }>({ state: 'pending', seconds: 0 })
  useEffect(() => {
    const start = performance.now()
    setSt({ state: 'pending', seconds: 0 })
    const id = setInterval(() => {
      const secs = Math.round((performance.now() - start) / 1000)
      const page = agentAnswerState()
      setSt((cur) => {
        const next = agentCardState(page, cur.state, secs)
        return next === cur.state && (next === 'landed' || next === 'offline') ? cur : { state: next, seconds: secs }
      })
    }, 300)
    return () => clearInterval(id)
  }, [k])
  return { ...st, warmMs: prefetchWarmMs(), failedMs: prefetchFailedMs() }
}

const secs1 = (ms: number) => `${(ms / 1000).toFixed(1)} s`

function AgentCard({ k }: { k: string }) {
  const shown = useStagger(4, 320, k)
  const a = useAgentAnswer(k)
  const steps: [string, string][] = [['Ask', 'plain words; a deterministic grammar picks the intent and the screen'], ['Engine tool', `one of ${agentToolCount()} tools; the backend composes the brief from its own evidence, not a language model`], ['Sourced card', 'facts badged per evidence row; interpretation kept apart, with a confidence'], ['Send or reject', 'a proposed action waits for a person; both verdicts are logged']]
  return (
    <div className="space-y-3">
      <Lead>The Agent Desk answers from engine tools. Facts arrive with their source and provenance; the reading is marked as composed; nothing runs until Send.</Lead>
      <div className="space-y-1.5">{steps.map(([t, d], i) => <div key={t} className="tour-chip grid grid-cols-[22px_1fr] items-start gap-2 rounded-[4px] border border-line bg-ink/60 px-2.5 py-1.5" data-on={i < shown}><span className="num pt-px text-[0.8em] text-dim">0{i + 1}</span><span className="text-[0.9em]"><span className="text-bone">{t}</span> <span className="text-dim">· {d}</span></span></div>)}</div>
      <div data-agent-state={a.state} className={cx('flex items-center gap-2 rounded-[4px] border px-2.5 py-1.5 text-[0.86em]', a.state === 'landed' ? 'border-up/40 text-up' : a.state === 'timeout' || a.state === 'offline' || (a.state === 'pending' && a.failedMs != null) ? 'border-warn/40 text-warn' : 'border-line text-mute')}>
        {a.state === 'pending' && a.failedMs == null && <><span className="pulse-dot inline-block size-1.5 rounded-full bg-fct" />Waiting for POST /flopx/ask: {a.seconds} s so far{a.warmMs != null ? ` (the tour asked ahead; the backend took ${secs1(a.warmMs)})` : ' (the tour asked ahead one beat early)'}. Autoplay holds this beat until the answer is on the page, up to {AGENT_WAIT_CAP_MS / 1000} s.</>}
        {a.state === 'pending' && a.failedMs != null && <><span className="pulse-dot inline-block size-1.5 rounded-full bg-warn" />The tour's early POST /flopx/ask failed after {secs1(a.failedMs)}. Waiting for the page's own request: {a.seconds} s so far, up to {AGENT_WAIT_CAP_MS / 1000} s; if it fails too, the page shows its prepared card.</>}
        {a.state === 'landed' && <>Answer on the page after {a.seconds} s{a.warmMs != null ? `; the tour's early request took the backend ${secs1(a.warmMs)}` : ''}.</>}
        {a.state === 'offline' && <>POST /flopx/ask did not answer{a.failedMs != null ? ` (the tour's early request failed after ${secs1(a.failedMs)})` : ''}; the page shows its prepared card, badged illustrative. Nothing here is a FLOPX reading.</>}
        {a.state === 'timeout' && <>No answer within {AGENT_WAIT_CAP_MS / 1000} s. The page keeps waiting and shows the answer when it lands; autoplay moves on rather than pretending.</>}
      </div>
      <Aside>The spotlighted answer is live from POST /flopx/ask and the composed brief for Texas; when the backend does not answer, the page says "offline, prepared card" and badges it illustrative.</Aside>
    </div>
  )
}

// ---------- 7 · review ----------

function RealityCard({ k }: { k: string }) {
  const rows = realityRows()
  const src = useSources()
  const steps = nextSteps()
  const shown = useStagger(rows.length + steps.length, 140, `${k}:${src.status}`)
  const col = (status: 'real' | 'partly' | 'illustrative', title: string, tone: string) => {
    const mine = rows.filter((r) => r.status === status)
    return (
      <div>
        <div className={cx('mb-1.5 flex items-center justify-between text-[0.92em] font-semibold', tone)}><span>{title}</span><span className="num text-dim">{mine.length}</span></div>
        <div className="space-y-1.5">{mine.map((r) => { const i = rows.indexOf(r); return <div key={r.area} className="tour-chip rounded-[4px] border border-line bg-ink/60 p-2" data-on={i < shown}><div className="flex items-center justify-between gap-2 text-[0.88em] text-bone"><span>{r.area}</span><Badge f={r.fresh} /></div><div className="text-[0.8em] leading-snug text-dim">{r.note}</div></div> })}</div>
      </div>
    )
  }
  return (
    <div className="space-y-3">
      <Lead>What is real today, what is still illustrative, and what comes next, read from the same live store the pages read.</Lead>
      <div className="grid grid-cols-3 gap-3">{col('real', 'Real today', 'text-up')}{col('partly', 'Real in part', 'text-warn')}{col('illustrative', 'Illustrative', 'text-mute')}</div>
      <div>
        <div className="mb-1 text-[0.92em] font-semibold text-fct">What is next</div>
        <div className="grid grid-cols-2 gap-1.5">{steps.map((s, i) => <div key={s} className="tour-chip flex gap-2 rounded-[4px] border border-dashed border-fct/30 px-2.5 py-1.5 text-[0.86em] text-mute" data-on={rows.length + i < shown}><ArrowRight size={11} className="mt-1 shrink-0 text-fct" />{s}</div>)}</div>
      </div>
      <Aside>{src.generatedAt ? `Newest backend payload ${fmtObserved(src.generatedAt)} UTC · ` : ''}{src.rows.filter((r) => r.freshness !== 'illustrative').length} live sources named by the backend; intelligence-market figures are the {SNAPSHOT.short} snapshot.</Aside>
    </div>
  )
}

// ---------- dispatch ----------

export function CardBody({ beatId, store }: { beatId: string; store: Store }) {
  const k = beatId
  switch (beatId) {
    case 'gr-invisible':
    case 'mv-invisible':
      return <SourceFan k={k} />
    case 'hrt-retrieval':
    case 'mv-retrieval':
      return <Counters k={k} />
    case 'hrt-sources':
      return <SourceTable k={k} />
    case 'gr-analysis':
    case 'mv-analysis':
      return <ProvenanceFlow k={k} />
    case 'gr-factors':
    case 'mv-factors':
      return <FactorCompose k={k} />
    case 'gr-reveal':
    case 'mv-reveal':
      return <RevealCard store={store} k={k} />
    case 'gr-estimation':
    case 'mv-estimation':
      return <Verdicts k={k} lead={<>Every forecast is scored against a baseline; the verdicts below are the harness's own outputs tonight.</>} />
    case 'hrt-eval':
      return <Verdicts k={k} lead={<>"Edge in 0 of 24" is the tests' result, and the fund publishes it; each ticker is scored against a baseline.</>} />
    case 'hrt-forecast':
    case 'mv-forecast':
      return <ForecastCard k={k} />
    case 'gr-anomaly':
      return <AnomalyCard k={k} />
    case 'gr-scenario':
      return <ScenarioCard store={store} k={k} />
    case 'gr-exposure':
      return <ExposureCard k={k} />
    case 'gr-plan':
      return <PlanCard k={k} />
    case 'hrt-signal':
      return <SignalCard k={k} />
    case 'hrt-send':
      return <SendCard store={store} k={k} />
    case 'hrt-attribution':
      return <AttributionCard store={store} k={k} />
    case 'mv-loop':
      return <LoopCard store={store} k={k} />
    case 'mv-agent':
      return <AgentCard k={k} />
    case 'mv-review':
      return <RealityCard k={k} />
    default:
      return <Aside>No card for this beat.</Aside>
  }
}

export const Icons = { Check, X }
