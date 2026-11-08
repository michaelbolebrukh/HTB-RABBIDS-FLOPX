import { useEffect, useState, type ReactNode } from 'react'
import { AlertTriangle, X } from 'lucide-react'
import { regionById, type Freshness } from '@/data/core'
import { NEWS, NEWS_FEED_SUMMARY as LF, PRED, type NewsItem, type NewsType, type PredMarket } from '@/data/finance'
import { projectById } from '@/data/infra'
import { TimeChart } from '@/components/charts'
import { AIBlock, cx, EntityLink, Fresh, PageHeader, Panel, Source, Spark, TruthTag } from '@/components/ui'
import { Drawer as Fold, Foldable, FoldButton, Row } from '@/components/collapse'
import { collapse, panelKey, useCollapsed } from '@/lib/layout'
import { splitFeed, type FeedItem } from '@/lib/feed'
import { useStore } from '@/lib/store'
import { NEWS_SUMMARY as NS, PM_CALIBRATION as PC, SNAPSHOT } from '@/data/handoff'

const TYPES: ('All' | NewsType)[] = ['All', 'News', 'Corporate', 'Project change', 'Regulatory', 'Prediction market']
/** One hue per event type, drawn from the theme tokens; warn stays reserved for weak verification. */
const TYPE_COLOR: Record<NewsType, string> = {
  News: 'var(--color-dec)',
  Corporate: 'var(--color-cat5)',
  'Project change': 'var(--color-up)',
  Regulatory: 'var(--color-act)',
  'Prediction market': 'var(--color-fct)',
}
const tint = (c: string, pct: number) => `color-mix(in srgb, ${c} ${pct}%, transparent)`
const days = (ago: string) => parseInt(ago, 10) || 0
const weak = (n: NewsItem) => n.verification === 'Single source' || n.verification === 'Unverified'

export default function News() {
  const { route, navigate } = useStore()
  const [type, setType] = useState<(typeof TYPES)[number]>('All')
  const [region, setRegion] = useState('all')
  const sel = route.params.id
  const open = (id: string) => navigate('news', { id })
  const close = () => navigate('news')
  const items: FeedItem[] = [
    ...NEWS.filter((n) => (type === 'All' || n.type === type) && (region === 'all' || n.regions.includes(region))).map((n) => ({ kind: 'news' as const, n })),
    ...PRED.filter((p) => (type === 'All' || type === 'Prediction market') && (region === 'all' || p.regions.includes(region))).map((p) => ({ kind: 'pm' as const, p })),
  ]
  const feed = splitFeed(items)
  const pms = items.flatMap((it) => (it.kind === 'pm' ? [it.p] : []))
  const byAge = (a: NewsItem, b: NewsItem) => days(a.ago) - days(b.ago)
  const keyNews = feed.key.flatMap((it) => (it.kind === 'news' ? [it.n] : [])).sort(byAge)
  const otherNews = feed.routine.flatMap((it) => (it.kind === 'news' ? [it.n] : [])).sort(byAge)
  const allNews = [...keyNews, ...otherNews].sort(byAge)
  const freshOfList = (xs: { freshness?: Freshness }[]): Freshness | undefined => (xs.some((x) => x.freshness === 'illustrative') ? 'illustrative' : undefined)
  const news = NEWS.find((n) => n.id === sel)
  const pm = PRED.find((p) => p.id === sel)

  return (
    <div>
      <PageHeader eyebrow={<>GDELT headlines, filings and prediction-market venues · <span className="num">{SNAPSHOT.short}</span></>} title="Events & market odds" sub="Events, sources and market-implied odds in one feed."
        about="Each item type is kept distinguishable. A market price is a crowd belief shaped by liquidity, not an objective probability." />
      <Foldable label="Filters" className="mb-4">
        <div className="flex flex-wrap items-center gap-2">
          {TYPES.map((t) => {
            const on = type === t
            const c = t === 'All' ? null : TYPE_COLOR[t]
            const count = t === 'All' ? NEWS.length + PRED.length : t === 'Prediction market' ? PRED.length : NEWS.filter((n) => n.type === t).length
            return (
              <button key={t} onClick={() => setType(t)} aria-pressed={on}
                className={cx('flex h-7 cursor-pointer items-center gap-1.5 rounded-ctl border px-2.5 text-[11.5px] transition-colors duration-[120ms]', on ? 'border-line2 bg-raise text-bone' : 'border-line text-mute hover:bg-hover hover:text-bone')}
                style={on && c ? { borderColor: tint(c, 50), background: tint(c, 10) } : undefined}>
                {c && <span className="size-1.5 rounded-full" style={{ background: c }} />}
                {t}<span className="num text-[10px] text-dim">{count}</span>
              </button>
            )
          })}
          <select value={region} onChange={(e) => setRegion(e.target.value)} className="ml-auto h-7 max-w-full rounded-[4px] border border-line2 bg-panel px-2 text-[11.5px]"><option value="all">All regions</option>{['us-tx', 'us-va', 'no-nordics', 'sg', 'ie-dub', 'de-fra'].map((r) => <option key={r} value={r}>{regionById(r)!.name}</option>)}</select>
        </div>
      </Foldable>

      <Foldable label="Timeline" className="mb-6">
        <Timeline items={allNews} sel={sel} onPick={open} />
      </Foldable>

      <Row cols={[2, 1]} at="lg" gap="gap-3" className="mb-5">
        <Panel title="Prediction-market calibration" right={<Fresh quiet f="historical" label={SNAPSHOT.short} />}>
          <p className="text-[12px] leading-relaxed text-mute">Reduction in forecast error from market prices, relative to the base rate.</p>
          <table className="mt-3 w-full text-[12px]">
            <thead>
              <tr className="border-b border-line text-left text-[10.5px] tracking-wide text-dim uppercase">
                <th className="py-1.5 pr-4 font-normal">Horizon</th>
                <th className="py-1.5 pr-4 font-normal whitespace-nowrap">Error cut</th>
                <th className="w-full py-1.5 pr-4 font-normal"><span className="sr-only">Scale</span></th>
                <th className="py-1.5 font-normal whitespace-nowrap">Reading</th>
              </tr>
            </thead>
            <tbody>
              <CalRow horizon="1 week" pct={PC.week.cutPct} reading="Informative" strong title={`A week before resolution, market prices cut base-rate error by ${PC.week.cutPct}%.`} />
              <CalRow horizon="1 month" pct={PC.month.cutPct} reading="Not significant" title={`A month out the cut is ${PC.month.cutPct}%, not distinguishable from zero.`} />
            </tbody>
          </table>
          <div className="mt-3 text-[11px] text-dim">
            <span className="num">{PC.markets}</span> markets · <span className="num">{PC.events}</span> events · <span className="num">${(PC.tradedUsd / 1e6).toFixed(1)}M</span> traded
          </div>
        </Panel>
        <Panel variant="rail" title="Headlines" eyebrow="Top themes" right={LF.live ? <Fresh quiet f="live" label={`Feed ${LF.asOf}`} /> : <Fresh quiet f="historical" label={SNAPSHOT.short} />}>
          <ol className="divide-y divide-line-soft">
            {(LF.live ? LF.themes : NS.themes).map((t, i) => (
              <li key={t} className="flex items-baseline gap-3 py-2 text-[12.5px]">
                <span className="num w-3 shrink-0 text-right text-[11px] text-act" style={{ opacity: 1 - i * 0.15 }}>{i + 1}</span>
                <span className={i === 0 ? 'text-bone' : 'text-mute'}>{t}</span>
              </li>
            ))}
          </ol>
          <div className="mt-2 text-[11px] text-dim" title={LF.live ? `${LF.sources.join(', ')}, last ${LF.days} days` : `${NS.source}, last ${NS.days} days`}>
            <span className="num">{LF.live ? LF.headlines : NS.headlines}</span> headlines · last <span className="num">{LF.live ? LF.days : NS.days}</span> days · {LF.live ? `${LF.sources.length} sources` : NS.source}
          </div>
        </Panel>
      </Row>

      {pms.length > 0 && (
        <Section title="Prediction markets" sub={freshOfList(pms) === 'illustrative' ? 'Market-implied odds, illustrative venues · thin books flagged' : 'Market-implied odds · thin books flagged'} count={pms.length} fresh={freshOfList(pms)}>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 2xl:grid-cols-4">
            {pms.map((p) => <MarketCard key={p.id} p={p} active={sel === p.id} onPick={open} />)}
          </div>
        </Section>
      )}
      <Section title="Key news" sub="High impact" count={keyNews.length} fresh={freshOfList(keyNews)}>
        <ol className="divide-y divide-line-soft border-y border-line-soft">
          {keyNews.map((n) => <NewsRow key={n.id} n={n} big active={sel === n.id} onPick={open} />)}
        </ol>
      </Section>
      <Section title="Other news" sub="Filings, project updates, lower impact" count={otherNews.length} fresh={freshOfList(otherNews)}>
        <ol className="divide-y divide-line-soft border-y border-line-soft">
          {otherNews.map((n) => <NewsRow key={n.id} n={n} active={sel === n.id} onPick={open} />)}
        </ol>
      </Section>
      {items.length === 0 && <div className="rounded-md border border-line bg-panel p-8 text-center text-[12px] text-mute">Nothing matches the filters.</div>}

      {(news || pm) && (
        <Drawer onClose={close}>
          {news && <NewsDetail n={news} />}
          {pm && <MarketDetail p={pm} />}
        </Drawer>
      )}
    </div>
  )
}

function Section({ title, sub, count, fresh, children }: { title: string; sub: string; count: number; fresh?: Freshness; children: ReactNode }) {
  const { route } = useStore()
  const open = !useCollapsed().includes(panelKey(route.page, title))
  if (count === 0) return null
  return (
    <section className="mb-6">
      <div className="mb-2.5 flex flex-wrap items-baseline gap-x-2.5">
        <h2 className="text-[14px] font-semibold tracking-tight text-bone">{title}</h2>
        <span className="num text-[11px] text-dim">{count}</span>
        <span className="text-[11.5px] text-dim">{sub}</span>
        {fresh && <Fresh quiet f={fresh} className="self-center" />}
        <span className="ml-auto self-center"><FoldButton open={open} label={title.toLowerCase()} onClick={() => collapse.toggle(route.page, title)} /></span>
      </div>
      <Fold open={open}>{children}</Fold>
    </section>
  )
}

const LANES: NewsType[] = ['News', 'Corporate', 'Project change', 'Regulatory']

/** Last 60 days as swimlanes, one per event type: dots sized by impact, filled when verified, high-impact events labelled. */
function Timeline({ items, sel, onPick }: { items: NewsItem[]; sel?: string; onPick: (id: string) => void }) {
  const span = 60
  const x = (d: number) => `${100 - (Math.min(d, span) / span) * 100}%`
  const lanes = LANES.filter((t) => items.some((n) => n.type === t))
  const laneH = 30
  return (
    <div className="rounded-md border border-line bg-panel px-4 pt-3 pb-2">
      <div className="mb-2 flex items-center justify-between text-[11px] text-dim">
        <span className="font-medium text-mute">Last 60 days <span className="num ml-1.5 text-dim">{items.length} events</span></span>
        <span className="flex items-center gap-3">
          <span className="flex items-center gap-1"><span className="size-2 rounded-full bg-mute" />filled = verified</span>
          <span className="flex items-center gap-1"><span className="size-2 rounded-full border border-mute" />hollow = single source</span>
        </span>
      </div>
      <div className="grid grid-cols-[104px_minmax(0,1fr)]">
        <div>
          {lanes.map((t) => (
            <div key={t} className="flex items-center gap-1.5 text-[11px] text-mute" style={{ height: laneH }}><span className="size-1.5 rounded-full" style={{ background: TYPE_COLOR[t] }} />{t}</div>
          ))}
        </div>
        <div className="relative" style={{ height: lanes.length * laneH + 16 }}>
          <div className="absolute top-0 right-0 bg-fct/[0.06]" style={{ left: x(7), height: lanes.length * laneH }} title="Last 7 days: headline window" />
          {lanes.map((t, i) => (
            <div key={t} className="absolute inset-x-0 h-px" style={{ top: i * laneH + laneH / 2, background: tint(TYPE_COLOR[t], 20) }} />
          ))}
          {[0, 15, 30, 45, 60].map((d) => (
            <div key={d} className="absolute top-0" style={{ left: x(d), height: lanes.length * laneH + 16 }}>
              <div className={cx('w-px', d === 0 ? 'bg-fct/60' : 'bg-line/70')} style={{ height: lanes.length * laneH }} />
              <span className={cx('num absolute bottom-0 -translate-x-1/2 text-[9.5px] whitespace-nowrap', d === 0 ? 'text-fct' : 'text-dim')} style={d === 0 ? { transform: 'translateX(-100%)' } : d === 60 ? { transform: 'none' } : undefined}>{d === 0 ? 'today' : `${d}d`}</span>
            </div>
          ))}
          {items.filter((n) => lanes.includes(n.type)).map((n) => {
            const d = days(n.ago)
            const on = sel === n.id
            const size = n.impact === 'high' ? 13 : n.impact === 'medium' ? 9 : 7
            const c = TYPE_COLOR[n.type]
            const top = lanes.indexOf(n.type) * laneH + laneH / 2
            const labelLeft = d < 30
            return (
              <div key={n.id} className="absolute" style={{ left: x(d), top }}>
                {n.impact === 'high' && (
                  <span className={cx('pointer-events-none absolute top-1/2 max-w-[220px] -translate-y-1/2 truncate text-[10.5px] whitespace-nowrap text-mute', labelLeft ? 'right-3 text-right' : 'left-3')}>{n.title}</span>
                )}
                <button title={`${n.ago} ago · ${n.title}`} aria-label={n.title} onClick={() => onPick(n.id)}
                  className="absolute -translate-x-1/2 -translate-y-1/2 cursor-pointer rounded-full hover:brightness-125"
                  style={{ width: size, height: size, background: weak(n) ? 'var(--color-panel)' : c, border: `1.5px solid ${c}`,
                    boxShadow: on ? `0 0 0 2px var(--color-panel), 0 0 0 3px ${c}` : undefined }} />
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}

/** One calibration horizon: error cut as a figure and a bar on a 0–100% scale. */
function CalRow({ horizon, pct, reading, strong, title }: { horizon: string; pct: number; reading: string; strong?: boolean; title: string }) {
  return (
    <tr className="border-b border-line-soft" title={title}>
      <td className="py-2.5 pr-4 whitespace-nowrap text-mute">{horizon}</td>
      <td className={cx('num py-2.5 pr-4 text-right', strong ? 'text-bone' : 'text-dim')}>{pct}%</td>
      <td className="py-2.5 pr-4"><div className="h-[3px] bg-line"><div className={cx('h-full', strong ? 'bg-up' : 'bg-dim')} style={{ width: `${pct}%` }} /></div></td>
      <td className={cx('py-2.5 whitespace-nowrap', strong ? 'text-up' : 'text-dim')}>{reading}</td>
    </tr>
  )
}

function MarketCard({ p, active, onPick }: { p: PredMarket; active: boolean; onPick: (id: string) => void }) {
  const pct = Math.round(p.prob * 100)
  return (
    <button onClick={() => onPick(p.id)} className={cx('group flex cursor-pointer flex-col rounded-md border bg-panel p-3.5 text-left transition-colors', active ? 'border-mute bg-raise' : 'border-line hover:border-line2')}>
      <div className="mb-2 flex items-center gap-2 text-[11px] text-dim">
        <span className="font-medium text-mute" title={p.venue}>{p.venue.replace(' (illustr.)', '')}</span>
        {p.caution && <span className="ml-auto flex items-center gap-1 text-warn"><AlertTriangle size={10} />thin</span>}
      </div>
      <div className="mb-3 line-clamp-2 min-h-[2.6em] text-[13px] leading-snug text-bone">{p.question}</div>
      <div className="mt-auto flex items-end justify-between gap-3">
        <div>
          <div className="num text-[26px] leading-none text-fct">{pct}%</div>
          {p.chg7 != null && <div className={cx('num mt-1 text-[11px]', p.chg7 > 0 ? 'text-up' : p.chg7 < 0 ? 'text-down' : 'text-dim')}>{p.chg7 > 0 ? '+' : ''}{p.chg7} pts · 7d</div>}
        </div>
        <Spark values={p.probHist} width={110} height={34} color="var(--color-fct)" />
      </div>
      <div className="mt-2.5 h-[3px] bg-line"><div className="h-full bg-fct/70" style={{ width: `${pct}%` }} /></div>
    </button>
  )
}

/** One event in the news ledger: age, type and verification, then the headline; key events add the summary. */
function NewsRow({ n, big, active, onPick }: { n: NewsItem; big?: boolean; active: boolean; onPick: (id: string) => void }) {
  return (
    <li>
      <button onClick={() => onPick(n.id)} className={cx('grid w-full cursor-pointer grid-cols-[44px_minmax(0,1fr)] gap-x-3 px-2 py-2.5 text-left transition-colors sm:grid-cols-[44px_minmax(0,1fr)_minmax(0,220px)]', active ? 'bg-raise' : 'hover:bg-hover')} style={{ boxShadow: `inset 2px 0 0 ${active ? TYPE_COLOR[n.type] : tint(TYPE_COLOR[n.type], 45)}` }}>
        <span className="num pt-px text-right text-[11px] text-dim">{n.ago}</span>
        <span className="min-w-0">
          <span className="flex flex-wrap items-center gap-x-1.5 text-[11px] text-dim">
            <span className="flex items-center gap-1" style={{ color: TYPE_COLOR[n.type] }}><span className="size-1.5 rounded-full" style={{ background: TYPE_COLOR[n.type] }} />{n.type}</span>·
            {weak(n) ? <span className="rounded-[2px] border border-warn/40 px-1 text-warn">{n.verification}</span> : <span>{n.verification}</span>}
          </span>
          <span className={cx('mt-0.5 block leading-snug text-bone', big ? 'text-[14px] font-medium' : 'text-[13px]')}>{n.title}</span>
          {big && <span className="mt-1 line-clamp-2 block text-[12px] leading-relaxed text-mute">{n.summary}</span>}
        </span>
        <span className="col-start-2 mt-1 flex min-w-0 flex-wrap items-center gap-x-2 text-[11px] text-dim sm:col-start-3 sm:mt-0 sm:flex-col sm:items-end sm:pt-px">
          <span className="truncate">{n.regions.map((r) => regionById(r)!.short).join(' · ')}{n.tickers.length > 0 && <> · <span className="num text-mute">{n.tickers.join(' ')}</span></>}</span>
          <span className="max-w-full truncate">{n.source}</span>
        </span>
      </button>
    </li>
  )
}

/** Right-hand slide-over for an event's details. Esc, the close button or the backdrop closes it. */
function Drawer({ onClose, children }: { onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div className="fixed inset-0 z-50 flex justify-end" role="dialog" aria-modal="true">
      <div className="absolute inset-0 bg-ink/60 backdrop-blur-[1px]" onClick={onClose} />
      <aside className="slide-in relative flex h-full w-full max-w-[600px] flex-col border-l border-line2 bg-panel shadow-2xl">
        <button onClick={onClose} aria-label="Close" className="absolute top-3 right-3 z-10 flex size-7 cursor-pointer items-center justify-center rounded-[4px] text-mute hover:bg-hover hover:text-bone"><X size={15} /></button>
        <div className="flex-1 overflow-y-auto p-5 pr-12">{children}</div>
      </aside>
    </div>
  )
}

function NewsDetail({ n }: { n: NewsItem }) {
  return (
    <>
      <div className="mb-1 flex items-center gap-2 text-[11px] text-dim"><span className="font-medium text-mute">{n.type}</span>· <Source source={n.source} time={n.time} /></div>
      <h2 className="mb-3 text-[19px] leading-snug font-semibold text-bone">{n.title}</h2>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        {n.freshness === 'illustrative' ? <Fresh quiet f="illustrative" /> : <TruthTag truth="obs" />}
        <span className={cx('text-[11px]', n.verification === 'Single source' ? 'rounded-[2px] border border-warn/40 px-1.5 text-warn' : 'text-mute')}>{n.verification}</span>
        <span className="text-[11px] text-dim">Impact: {n.impact}</span>
      </div>
      <p className="mb-5 text-[13.5px] leading-relaxed text-bone/90">{n.summary}</p>
      <div className="grid grid-cols-3 gap-3 text-[12px]">
        <div><div className="eyebrow mb-1">Regions</div>{n.regions.map((r) => <div key={r}><EntityLink r={{ kind: 'region', id: r, label: regionById(r)!.name }} /></div>)}</div>
        <div><div className="eyebrow mb-1">Assets</div>{n.tickers.map((t) => <div key={t}><EntityLink r={{ kind: 'security', id: t, label: t }} className="font-mono" /></div>)}</div>
        <div><div className="eyebrow mb-1">Projects</div>{n.projects?.map((p) => <div key={p}><EntityLink r={{ kind: 'project', id: p, label: projectById(p)!.name }} /></div>) ?? <span className="text-dim">—</span>}</div>
      </div>
      <div className="mt-5 rounded-md border border-line bg-ink p-3 text-[12px]">
        <div className="eyebrow mb-1">How FLOPX uses this</div>
        {n.verification === 'Single source' ? 'Treated as weak evidence (weight 0.05) in the Texas pressure estimate. Unconfirmed reports never move an estimate on their own.' : n.verification === 'Confirmed by filing' ? 'Confirmed by filing: used directly as an observation (e.g. milestone dates, regulatory status).' : 'Corroborated by multiple sources: used as supporting evidence.'}
      </div>
    </>
  )
}

function MarketDetail({ p }: { p: PredMarket }) {
  return (
    <>
      <div className="mb-1 text-[11px] text-dim"><span className="font-medium text-mute">Prediction market</span> · {p.venue} · closes {p.closes}{p.freshness === 'illustrative' && <Fresh quiet f="illustrative" className="ml-2" />}</div>
      <h2 className="mb-4 text-[19px] leading-snug font-semibold text-bone">{p.question}</h2>
      <div className="mb-3 flex items-end gap-4">
        <div><div className="text-[11px] font-medium text-dim">Market-implied</div><div className="num text-[34px] leading-none text-fct">{Math.round(p.prob * 100)}%</div></div>
        {p.chg7 != null && <div className={cx('num pb-1 text-[12px]', p.chg7 > 0 ? 'text-up' : p.chg7 < 0 ? 'text-down' : 'text-mute')}>{p.chg7 > 0 ? '+' : ''}{p.chg7} pts 7d</div>}
      </div>
      <TimeChart labels={Array.from({ length: 30 }, (_, i) => `${i - 29}d`)} height={170} yFmt={(v) => `${Math.round(v * 100)}%`} series={[{ id: 'p', label: 'Price (YES)', values: p.probHist, color: 'var(--color-fct)', area: true }]} />
      <div className="mt-3 grid grid-cols-4 gap-3">
        {[[p.freshness === 'illustrative' ? 'Volume' : 'Volume 24h', p.volume != null ? `$${(p.volume / 1000).toFixed(p.volume < 10000 ? 1 : 0)}k` : 'not loaded'], ['Depth', `$${(p.liquidity / 1000).toFixed(1)}k`], ['Spread', p.spread != null ? `${(p.spread * 100).toFixed(0)} pts` : 'not loaded'], ['Closes', p.closes]].map(([k, v]) => <div key={k}><div className="text-[11px] font-medium text-dim">{k}</div><div className="num text-[13px]">{v}</div></div>)}
      </div>
      {p.caution && <div className="mt-3 flex gap-2 rounded-[4px] border border-warn/35 bg-warn/[0.04] p-2.5 text-[12px] text-warn"><AlertTriangle size={14} className="mt-0.5 shrink-0" />{p.caution}</div>}
      <div className="mt-3 text-[12px]"><span className="eyebrow mr-2">Resolution</span>{p.resolution}</div>
      <div className="mt-2 text-[12px]">Related: {p.regions.map((r) => <EntityLink key={r} r={{ kind: 'region', id: r, label: regionById(r)!.short }} className="mr-2" />)}{p.tickers.map((t) => <EntityLink key={t} r={{ kind: 'security', id: t, label: t }} className="mr-2 font-mono" />)}</div>
      <AIBlock className="mt-4">Traders’ beliefs, fees and liquidity; not a calibrated probability. Liquidity-adjusted weight{p.caution ? ', near zero here (thin depth)' : ''}; never treated as fact.</AIBlock>
    </>
  )
}
