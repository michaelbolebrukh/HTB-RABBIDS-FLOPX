import { useEffect, useState, type ReactNode } from 'react'
import { AlertTriangle, Newspaper, X } from 'lucide-react'
import { regionById, type Freshness } from '@/data/core'
import { NEWS, NEWS_FEED_SUMMARY as LF, PRED, type NewsItem, type NewsType, type PredMarket } from '@/data/finance'
import { projectById } from '@/data/infra'
import { TimeChart } from '@/components/charts'
import { AIBlock, cx, EntityLink, PageHeader, Source, Spark, TruthTag } from '@/components/ui'
import { splitFeed, type FeedItem } from '@/lib/feed'
import { useStore } from '@/lib/store'
import { Fresh, Meter } from '@/components/ui'
import { NEWS_SUMMARY as NS, PM_CALIBRATION as PC, SNAPSHOT } from '@/data/handoff'

const TYPES: ('All' | NewsType)[] = ['All', 'News', 'Corporate', 'Project change', 'Regulatory', 'Prediction market']
/** One colour per event type, used for pills, card edges and timeline dots. */
const TYPE_COLOR: Record<NewsType, string> = {
  News: 'var(--color-obs)',
  Corporate: 'var(--color-dec)',
  'Project change': 'var(--color-up)',
  Regulatory: 'var(--color-warn)',
  'Prediction market': 'var(--color-fct)',
}
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
  const keyNews = feed.key.flatMap((it) => (it.kind === 'news' ? [it.n] : []))
  const otherNews = feed.routine.flatMap((it) => (it.kind === 'news' ? [it.n] : []))
  const allNews = [...keyNews, ...otherNews].sort((a, b) => days(a.ago) - days(b.ago))
  const news = NEWS.find((n) => n.id === sel)
  const pm = PRED.find((p) => p.id === sel)
  // Region filter: every region the current items mention (live ids included), by name.
  const regionOpts = Array.from(new Set([...NEWS.flatMap((n) => n.regions), ...PRED.flatMap((p) => p.regions)])).sort((a, b) => regionById(a)!.name.localeCompare(regionById(b)!.name))
  const freshOfList = (xs: { freshness?: Freshness }[]): Freshness | undefined => (xs.some((x) => x.freshness === 'illustrative') ? 'illustrative' : undefined)

  return (
    <div>
      <PageHeader eyebrow={<><Newspaper size={12} /> News & prediction markets</>} title="Events, sources and market-implied odds" sub="Sourced news, corporate filings, project changes and prediction-market contracts in one feed, each kept distinguishable. A market price is a crowd belief shaped by liquidity, not an objective probability." />
      <div className="mb-4 flex flex-wrap items-center gap-2">
        {TYPES.map((t) => {
          const on = type === t
          const c = t === 'All' ? 'var(--color-bone)' : TYPE_COLOR[t]
          return (
            <button key={t} onClick={() => setType(t)} className={cx('flex cursor-pointer items-center gap-1.5 rounded-full border px-3 py-1 text-[11.5px] transition-colors', on ? 'text-bone' : 'border-line2 text-mute hover:text-bone')}
              style={on ? { borderColor: c, background: `color-mix(in oklab, ${c} 14%, transparent)` } : undefined}>
              {t !== 'All' && <span className="inline-block size-1.5 rounded-full" style={{ background: c }} />}{t}
            </button>
          )
        })}
        <select value={region} onChange={(e) => setRegion(e.target.value)} className="ml-auto h-7 rounded-[4px] border border-line2 bg-panel px-2 font-mono text-[11px]"><option value="all">All regions</option>{regionOpts.map((r) => <option key={r} value={r}>{regionById(r)!.name}</option>)}</select>
      </div>

      <Timeline items={allNews} sel={sel} onPick={open} />

      <div className="mb-5 grid grid-cols-1 gap-3 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <div className="rounded-md border border-line bg-panel p-4">
          <div className="mb-2 flex flex-wrap items-center gap-2"><span className="text-[13px] font-semibold">Prediction-market calibration</span><Fresh f="historical" label={SNAPSHOT.short} /></div>
          <div className="mb-3 flex flex-wrap gap-1.5" title="Prediction-market calibration sample">
            <span className="num rounded-full border border-line2 px-2 py-0.5 text-[11px] text-mute">{PC.markets} markets</span>
            <span className="num rounded-full border border-line2 px-2 py-0.5 text-[11px] text-mute">{PC.events} events</span>
            <span className="num rounded-full border border-line2 px-2 py-0.5 text-[11px] text-mute">${(PC.tradedUsd / 1e6).toFixed(1)}M traded</span>
          </div>
          <div className="space-y-2">
            <div title={`A week before resolution, market prices cut base-rate error by ${PC.week.cutPct}% (${PC.week.from}% → ${PC.week.to}%).`}>
              <div className="mb-0.5 flex justify-between text-[11px] text-mute"><span>1 week out</span><span className="num text-bone">{PC.week.cutPct}%</span></div>
              <Meter value={PC.week.cutPct} max={100} color="var(--color-up)" />
            </div>
            <div title={`A month out the cut is ${PC.month.cutPct}%, not distinguishable from zero.`}>
              <div className="mb-0.5 flex justify-between text-[11px] text-mute"><span>1 month out <span className="text-dim">· not distinguishable from zero</span></span><span className="num text-bone">{PC.month.cutPct}%</span></div>
              <Meter value={PC.month.cutPct} max={100} color="var(--color-dim)" hatched />
            </div>
          </div>
          <p className="mt-2 text-[11px] text-dim">Error cut vs base rate</p>
        </div>
        <div className="rounded-md border border-line bg-panel p-4">
          <div className="mb-2 flex flex-wrap items-center gap-2"><span className="text-[13px] font-semibold">Headlines</span>{LF.live ? <Fresh f="live" label={`Feed ${LF.asOf}`} /> : <Fresh f="historical" label={SNAPSHOT.short} />}</div>
          <div className="flex flex-wrap gap-1.5" title={LF.live ? `${LF.sources.join(', ')}, last ${LF.days} days` : `${NS.source}, last ${NS.days} days`}>
            <span className="num rounded-full border border-line2 px-2 py-0.5 text-[11px] text-bone">{LF.live ? LF.headlines : NS.headlines} headlines</span>
            <span className="num rounded-full border border-line2 px-2 py-0.5 text-[11px] text-bone">{LF.live ? LF.days : NS.days} days</span>
            <span className="rounded-full border border-line2 px-2 py-0.5 text-[11px] text-bone">{LF.live ? `${LF.sources.length} sources` : 'GDELT'}</span>
          </div>
          <div className="mt-2 flex flex-wrap gap-1.5">{(LF.live ? LF.themes : NS.themes).map((t, i) => <span key={t} className="rounded-full border border-line2 px-2 py-0.5 text-[11px] text-mute" style={{ borderLeft: `3px solid var(--color-cat${(i % 3) + 1})` }}>{t}</span>)}</div>
        </div>
      </div>

      {pms.length > 0 && (
        <Section title="Prediction markets" sub="Market-implied odds · thin books flagged" count={pms.length} color={TYPE_COLOR['Prediction market']} fresh={freshOfList(pms)}>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 2xl:grid-cols-4">
            {pms.map((p) => <MarketCard key={p.id} p={p} active={sel === p.id} onPick={open} />)}
          </div>
        </Section>
      )}
      <Section title="Key news" sub="High-impact events" count={keyNews.length} color="var(--color-bone)" fresh={freshOfList(keyNews)}>
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          {keyNews.map((n) => <NewsCard key={n.id} n={n} big active={sel === n.id} onPick={open} />)}
        </div>
      </Section>
      <Section title="Other news" sub="Filings, project updates and lower-impact reports" count={otherNews.length} color="var(--color-mute)" fresh={freshOfList(otherNews)}>
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2 2xl:grid-cols-3">
          {otherNews.map((n) => <NewsCard key={n.id} n={n} active={sel === n.id} onPick={open} />)}
        </div>
      </Section>
      {items.length === 0 && <div className="rounded-md border border-line bg-panel p-8 text-center text-[12px] text-mute">Nothing matches the filters.</div>}

      {(news || pm) && (
        <Drawer onClose={close} color={news ? TYPE_COLOR[news.type] : TYPE_COLOR['Prediction market']}>
          {news && <NewsDetail n={news} />}
          {pm && <MarketDetail p={pm} />}
        </Drawer>
      )}
    </div>
  )
}

function Section({ title, sub, count, color, fresh, children }: { title: string; sub: string; count: number; color: string; fresh?: Freshness; children: ReactNode }) {
  if (count === 0) return null
  return (
    <section className="mb-6">
      <div className="mb-2.5 flex items-baseline gap-2.5">
        <span className="inline-block h-3 w-[3px] self-center rounded-full" style={{ background: color }} />
        <h2 className="text-[14px] font-semibold tracking-tight text-bone">{title}</h2>
        <span className="num text-[11px] text-dim">{count}</span>
        <span className="text-[11.5px] text-dim">{sub}</span>
        {fresh && <Fresh f={fresh} className="self-center" />}
      </div>
      {children}
    </section>
  )
}

/** Last 60 days as a strip: one dot per event, coloured by type, sized by impact. */
function Timeline({ items, sel, onPick }: { items: NewsItem[]; sel?: string; onPick: (id: string) => void }) {
  const span = 60
  const x = (d: number) => `${100 - (Math.min(d, span) / span) * 100}%`
  const lanes = new Map<number, number>() // stack events landing on the same day
  return (
    <div className="mb-6 rounded-md border border-line bg-panel px-4 pt-3 pb-2">
      <div className="mb-1 flex items-center justify-between text-[11px] text-dim">
        <span className="font-medium text-mute">Last 60 days</span>
        <span className="flex flex-wrap gap-3">{(Object.keys(TYPE_COLOR) as NewsType[]).filter((t) => t !== 'Prediction market').map((t) => <span key={t} className="flex items-center gap-1"><span className="inline-block size-1.5 rounded-full" style={{ background: TYPE_COLOR[t] }} />{t}</span>)}</span>
      </div>
      <div className="relative h-14">
        <div className="absolute inset-x-0 top-1/2 h-px bg-line2" />
        {[0, 15, 30, 45, 60].map((d) => (
          <div key={d} className="absolute top-0 bottom-0" style={{ left: x(d) }}>
            <div className="h-full w-px bg-line/70" />
            <span className="num absolute -bottom-0.5 -translate-x-1/2 text-[9.5px] whitespace-nowrap text-dim" style={d === 0 ? { transform: 'translateX(-100%)' } : undefined}>{d === 0 ? 'today' : `${d}d`}</span>
          </div>
        ))}
        {items.map((n) => {
          const d = days(n.ago)
          const n0 = lanes.get(d) ?? 0
          lanes.set(d, n0 + 1)
          // Same-day events alternate centre, above, below so a busy day stays inside the strip.
          const lane = [0, 1, -1][n0 % 3]
          const size = n.impact === 'high' ? 14 : n.impact === 'medium' ? 10 : 8
          const c = TYPE_COLOR[n.type]
          return (
            <button key={n.id} title={`${n.ago} ago · ${n.title}`} onClick={() => onPick(n.id)}
              className="absolute -translate-x-1/2 -translate-y-1/2 cursor-pointer rounded-full transition-transform hover:scale-125"
              style={{ left: x(d), top: `calc(50% - ${lane * 14}px)`, width: size, height: size, background: weak(n) ? 'transparent' : c, border: `1.5px solid ${c}`, boxShadow: sel === n.id ? `0 0 0 3px color-mix(in oklab, ${c} 35%, transparent)` : undefined }} />
          )
        })}
      </div>
    </div>
  )
}

function MarketCard({ p, active, onPick }: { p: PredMarket; active: boolean; onPick: (id: string) => void }) {
  const pct = Math.round(p.prob * 100)
  return (
    <button onClick={() => onPick(p.id)} className={cx('group flex cursor-pointer flex-col rounded-md border bg-panel p-3.5 text-left transition-colors', active ? 'border-fct' : 'border-line hover:border-fct/50')}>
      <div className="mb-2 flex items-center gap-2 text-[11px] text-dim">
        <span className="font-medium text-fct">{p.venue}</span>
        {p.caution && <span className="ml-auto flex items-center gap-1 text-warn"><AlertTriangle size={10} />thin book</span>}
      </div>
      <div className="mb-3 line-clamp-2 min-h-[2.6em] text-[13px] leading-snug text-bone">{p.question}</div>
      <div className="mt-auto flex items-end justify-between gap-3">
        <div>
          <div className="num text-[26px] leading-none text-fct">{pct}%</div>
          {p.chg7 != null
            ? <div className={cx('num mt-1 text-[11px]', p.chg7 > 0 ? 'text-up' : p.chg7 < 0 ? 'text-down' : 'text-dim')}>{p.chg7 > 0 ? '+' : ''}{p.chg7} pts 7d</div>
            : <div className="mt-1 text-[11px] text-dim">no 7d history</div>}
        </div>
        <Spark values={p.probHist} width={110} height={34} color="var(--color-fct)" />
      </div>
      <div className="mt-2.5 h-1 overflow-hidden rounded-full bg-line"><div className="h-full rounded-full bg-fct/60" style={{ width: `${pct}%` }} /></div>
    </button>
  )
}

function NewsCard({ n, big, active, onPick }: { n: NewsItem; big?: boolean; active: boolean; onPick: (id: string) => void }) {
  const c = TYPE_COLOR[n.type]
  return (
    <button onClick={() => onPick(n.id)} className={cx('relative flex cursor-pointer flex-col overflow-hidden rounded-md border bg-panel py-3 pr-3.5 pl-4 text-left transition-colors', active ? 'border-line2 bg-raise' : 'border-line hover:border-line2 hover:bg-hover')}>
      <span aria-hidden className="absolute inset-y-0 left-0 w-[3px]" style={{ background: c }} />
      <div className="mb-1.5 flex items-center gap-2 text-[11px] text-dim">
        <span className="font-medium" style={{ color: c }}>{n.type}</span><span>· {n.ago} ago</span>
        <span className={cx('ml-auto rounded-[3px] border px-1.5', weak(n) ? 'border-warn/40 text-warn' : 'border-line2')}>{n.verification}</span>
      </div>
      <div className={cx('leading-snug text-bone', big ? 'text-[15px] font-medium' : 'text-[13px]')}>{n.title}</div>
      {big && <p className="mt-1.5 line-clamp-2 text-[12px] leading-relaxed text-mute">{n.summary}</p>}
      <div className="mt-auto flex flex-wrap items-center gap-1.5 pt-2 font-mono text-[10px] text-dim">
        {n.regions.map((r) => <span key={r} className="rounded-[2px] bg-raise px-1">{regionById(r)!.short}</span>)}
        {n.tickers.map((t) => <span key={t} className="text-mute">{t}</span>)}
        <span className="ml-auto truncate">{n.source}</span>
      </div>
    </button>
  )
}

/** Right-hand slide-over for an event's details. Esc, the close button or the backdrop closes it. */
function Drawer({ onClose, color, children }: { onClose: () => void; color: string; children: ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div className="fixed inset-0 z-50 flex justify-end" role="dialog" aria-modal="true">
      <div className="absolute inset-0 bg-ink/60 backdrop-blur-[1px]" onClick={onClose} />
      <aside className="slide-in relative flex h-full w-full max-w-[600px] flex-col border-l border-line2 bg-panel shadow-2xl">
        <div className="h-[3px] shrink-0" style={{ background: color }} />
        <button onClick={onClose} aria-label="Close" className="absolute top-3 right-3 z-10 flex size-7 cursor-pointer items-center justify-center rounded-[4px] text-mute hover:bg-hover hover:text-bone"><X size={15} /></button>
        <div className="flex-1 overflow-y-auto p-5 pr-12">{children}</div>
      </aside>
    </div>
  )
}

function NewsDetail({ n }: { n: NewsItem }) {
  return (
    <>
      <div className="mb-1 flex items-center gap-2 text-[11px] text-dim"><span className="font-medium" style={{ color: TYPE_COLOR[n.type] }}>{n.type}</span>· <Source source={n.source} time={n.time} />{n.url && <a href={n.url} target="_blank" rel="noreferrer" className="ml-auto text-mute underline hover:text-bone">Original ↗</a>}</div>
      <h2 className="mb-3 text-[19px] leading-snug font-semibold text-bone">{n.title}</h2>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        {n.freshness === 'illustrative' ? <Fresh f="illustrative" /> : <TruthTag truth="obs" />}
        <span className={cx('rounded-[3px] border px-1.5 font-mono text-[10px]', n.verification === 'Single source' ? 'border-warn/40 text-warn' : 'border-line2 text-mute')}>{n.verification}</span>
        <span className="font-mono text-[10px] text-dim">impact: {n.impact}</span>
      </div>
      <p className="mb-5 text-[13.5px] leading-relaxed text-bone/90">{n.summary}</p>
      <div className="grid grid-cols-3 gap-3 text-[12px]">
        <div><div className="eyebrow mb-1">Regions</div>{n.regions.map((r) => <div key={r}><EntityLink r={{ kind: 'region', id: r, label: regionById(r)!.name }} /></div>)}</div>
        <div><div className="eyebrow mb-1">Assets</div>{n.tickers.map((t) => <div key={t}><EntityLink r={{ kind: 'security', id: t, label: t }} className="font-mono" /></div>)}</div>
        <div><div className="eyebrow mb-1">Projects</div>{n.projects?.map((p) => <div key={p}><EntityLink r={{ kind: 'project', id: p, label: projectById(p)!.name }} /></div>) ?? <span className="text-dim">—</span>}</div>
      </div>
      <div className="mt-5 rounded-md border border-line bg-ink p-3 text-[12px]">
        <div className="eyebrow mb-1">How FLOPX uses this</div>
        {n.verification === 'Single source' ? 'One source only, so it is weak evidence. A single report never moves an estimate on its own.' : n.verification === 'Confirmed by filing' ? 'Confirmed by filing: usable directly as an observation (e.g. milestone dates, regulatory status).' : n.verification === 'Unverified' ? 'Discussion or an unverified report: shown for context, not used as evidence.' : 'Corroborated by multiple sources: used as supporting evidence.'}
      </div>
    </>
  )
}

function MarketDetail({ p }: { p: PredMarket }) {
  return (
    <>
      <div className="mb-1 flex items-center gap-1 text-[11px] text-dim"><span className="font-medium text-fct">Prediction market</span> · {p.venue} · closes {p.closes}{p.freshness === 'illustrative' && <Fresh f="illustrative" className="ml-2" />}{p.url && <a href={p.url} target="_blank" rel="noreferrer" className="ml-auto text-mute underline hover:text-bone">Venue ↗</a>}</div>
      <h2 className="mb-4 text-[19px] leading-snug font-semibold text-bone">{p.question}</h2>
      <div className="mb-3 flex items-end gap-4">
        <div><div className="text-[11px] font-medium text-dim">Market-implied</div><div className="num text-[34px] leading-none text-fct">{Math.round(p.prob * 100)}%</div></div>
        {p.chg7 != null
          ? <div className={cx('num pb-1 text-[12px]', p.chg7 > 0 ? 'text-up' : p.chg7 < 0 ? 'text-down' : 'text-mute')}>{p.chg7 > 0 ? '+' : ''}{p.chg7} pts 7d</div>
          : <div className="pb-1 text-[12px] text-dim">{p.updated ? `last update ${p.updated}` : 'no 7d history'}</div>}
      </div>
      {p.probHist.length > 1
        ? <TimeChart labels={Array.from({ length: p.probHist.length }, (_, i) => `${i - p.probHist.length + 1}d`)} height={170} yFmt={(v) => `${Math.round(v * 100)}%`} series={[{ id: 'p', label: 'Price (YES)', values: p.probHist, color: 'var(--color-fct)', area: true }]} />
        : <div className="flex h-[170px] items-center justify-center rounded-md border border-line text-[12px] text-dim">The venue publishes no price history for this market yet.</div>}
      <div className="mt-3 grid grid-cols-4 gap-3">
        {[[p.freshness === 'illustrative' ? 'Volume' : 'Volume 24h', p.volume != null ? `$${(p.volume / 1000).toFixed(p.volume < 10000 ? 1 : 0)}k` : '—'], ['Depth', `$${(p.liquidity / 1000).toFixed(1)}k`], ['Spread', p.spread != null ? `${(p.spread * 100).toFixed(0)} pts` : '—'], ['Closes', p.closes]].map(([k, v]) => <div key={k}><div className="text-[11px] font-medium text-dim">{k}</div><div className="num text-[13px]">{v}</div></div>)}
      </div>
      {p.caution && <div className="mt-3 flex gap-2 rounded-[4px] border border-warn/35 bg-warn/[0.04] p-2.5 text-[12px] text-warn"><AlertTriangle size={14} className="mt-0.5 shrink-0" />{p.caution}</div>}
      <div className="mt-3 text-[12px]"><span className="eyebrow mr-2">Resolution</span>{p.resolution}</div>
      <div className="mt-2 text-[12px]">Related: {p.regions.map((r) => <EntityLink key={r} r={{ kind: 'region', id: r, label: regionById(r)!.short }} className="mr-2" />)}{p.tickers.map((t) => <EntityLink key={t} r={{ kind: 'security', id: t, label: t }} className="mr-2 font-mono" />)}</div>
      <AIBlock className="mt-4">This price reflects traders’ beliefs, fees and liquidity. It is not a calibrated probability. FLOPX gives it a liquidity-adjusted weight{p.caution ? ' that is near zero here because of thin depth' : ''} and never treats it as a fact.</AIBlock>
    </>
  )
}
