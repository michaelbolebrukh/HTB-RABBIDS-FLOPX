import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Bot, BookOpen, Briefcase, BrainCircuit, ChartLine, ChevronRight, Command, Cpu, Ellipsis, FlaskConical, LayoutDashboard, Landmark, LogIn, MessageSquareText, Newspaper, ScanLine, Search, ShoppingCart, UserRound, X, Zap } from 'lucide-react'
import { AS_OF, GLOBAL_CPI, GPUS, NOW_IDX, type Freshness, type GpuLive, PRESSURE_MODEL } from '@/data/core'
import { domain as liveDomain } from '@/live/store'
import { closeBadgeOf, W2_LIVE, type CloseBadge } from '@/live/adapt-w2'
import { ENERGY } from '@/data/infra'
import { chg, last, SECURITIES } from '@/data/finance'
import { useStore, type PageId } from '@/lib/store'
import { collapse, panelKey, useCollapsed } from '@/lib/layout'
import { cx, Delta, Fresh, TruthGlyph } from './ui'
import { EdgeToggle, useMinWidth } from './collapse'
import { BlendSwitcher } from './BlendSwitcher'
import { ChartStyleMenu } from './ChartStyleMenu'
import { MapSchemeMenu } from './MapSchemeMenu'
import { SNAPSHOT } from '@/data/handoff'
import { start as startTour } from '@/tour/model'
import { TOUR_IDS, TOURS } from '@/tour/tours'
import { AuthControl, SignInDialog } from './SignIn'
import { useAuth } from '@/live/auth'
import { useIsPhone } from '@/lib/usePhone'

export const NAV: { section: string; items: { id: PageId; label: string; icon: ReactNode; tag?: string }[] }[] = [
  { section: 'Brief', items: [{ id: 'overview', label: 'Global Intelligence', icon: <LayoutDashboard size={15} /> }] },
  {
    section: 'Compute & Intelligence',
    items: [
      { id: 'markets', label: 'GPU Markets', icon: <Cpu size={15} /> },
      { id: 'intelligence', label: 'Intelligence Market', icon: <BrainCircuit size={15} /> },
      { id: 'procurement', label: 'Procurement Desk', icon: <ShoppingCart size={15} />, tag: 'HRT' },
      { id: 'xray', label: 'Compute X-Ray', icon: <ScanLine size={15} />, tag: 'G-R' },
    ],
  },
  { section: 'Agents', items: [{ id: 'agents', label: 'Agent Desk', icon: <Bot size={15} />, tag: 'MV' }] },
  {
    section: 'Coverage',
    items: [
      { id: 'energy', label: 'Energy & Infra', icon: <Zap size={15} /> },
      { id: 'news', label: 'News & Prediction', icon: <Newspaper size={15} /> },
      { id: 'securities', label: 'Equities & Derivs', icon: <Landmark size={15} /> },
      { id: 'fund', label: 'Systematic Fund', icon: <ChartLine size={15} /> },
      { id: 'trading', label: 'Trading Desk', icon: <Briefcase size={15} /> },
      { id: 'research', label: 'Research & Eval', icon: <FlaskConical size={15} /> },
    ],
  },
  { section: 'System', items: [{ id: 'handoff', label: 'Truth System & States', icon: <BookOpen size={15} /> }] },
]

function LogoMark() {
  return (
    <div className="grid grid-cols-2 gap-[2px]">
      <span className="size-[7px] bg-obs" />
      <span className="size-[7px] border border-inf hatch-inf" />
      <span className="size-[7px] rounded-full border border-dashed border-fct" />
      <span className="size-[7px] rotate-45 scale-75 bg-dec" />
    </div>
  )
}

export function Logo() {
  return (
    <div className="flex items-center gap-2.5">
      <LogoMark />
      <span className="text-[15px] font-bold tracking-[-0.01em] text-bone">FLOPX</span>
    </div>
  )
}

export function Sidebar() {
  const { route, navigate, tour, setTour, marketOpen, marketOverridden, marketLabel, toggleMarket } = useStore()
  // Folded, the sidebar keeps an icon rail so every page stays one click away. On phone widths it starts
  // folded for the session, so the page keeps its width; the saved choice applies on wider screens.
  const wide = useMinWidth('48rem')
  const [phoneOpen, setPhoneOpen] = useState(false)
  const saved = !useCollapsed().includes(panelKey('shell', 'sidebar'))
  const open = wide ? saved : phoneOpen
  const toggle = () => (wide ? collapse.toggle('shell', 'sidebar') : setPhoneOpen((o) => !o))
  return (
    <aside data-chrome className={cx('relative flex shrink-0 flex-col border-r border-line bg-ink text-bone transition-[width] duration-300 ease-out motion-reduce:transition-none', open ? 'w-[216px]' : 'w-[52px]')}>
      <EdgeToggle edge="right" open={open} rail label="sidebar" onClick={toggle} />
      <div className="flex h-12 items-center overflow-hidden border-b border-line px-4">
        {open ? <Logo /> : <div className="-ml-0.5"><LogoMark /></div>}
      </div>
      <nav className="flex-1 overflow-x-hidden overflow-y-auto px-2 py-3">
        {NAV.map((s) => (
          <div key={s.section} className={open ? 'mb-4' : 'mb-2 border-b border-line pb-2 last:border-0'}>
            {open && <div className="eyebrow px-2 pb-1.5 whitespace-nowrap">{s.section}</div>}
            {s.items.map((it) => (
              <button
                key={it.id}
                onClick={() => navigate(it.id)}
                title={open ? undefined : it.label}
                aria-label={it.label}
                className={cx('group flex w-full cursor-pointer items-center gap-2.5 rounded-ctl px-2 py-[7px] text-left text-[12.5px] whitespace-nowrap transition-colors', open ? '' : 'justify-center', route.page === it.id ? 'bg-raise text-bone' : 'text-mute hover:bg-panel hover:text-bone')}
              >
                <span className={cx(route.page === it.id ? 'text-bone' : 'text-dim group-hover:text-mute')}>{it.icon}</span>
                {open && <span className="flex-1">{it.label}</span>}
                {open && it.tag && <span className="rounded-badge border border-line2 px-1 text-[9px] font-medium tracking-wide text-dim">{it.tag}</span>}
              </button>
            ))}
          </div>
        ))}
      </nav>
      <div inert={!open} className={cx('border-t border-line px-2 py-3', !open && 'hidden')}>
        <div className="eyebrow px-2 pb-1.5">Demo walkthroughs</div>
        {TOUR_IDS.map((id) => {
          const t = TOURS[id]
          const on = tour?.id === id
          return (
            <button key={id} onClick={() => setTour(on ? null : startTour(id))} title={`${t.audience}. ${t.thesis}`}
              aria-current={on ? 'true' : undefined}
              className={cx('flex w-full cursor-pointer items-center gap-2.5 rounded-ctl px-2 py-[6px] text-left text-[12.5px] transition-colors', on ? 'bg-raise text-bone' : 'text-mute hover:bg-panel hover:text-bone')}>
              <span aria-hidden className={cx('size-[7px] shrink-0 rounded-[1.5px] bg-current', t.tone === 'inf' ? 'text-inf' : t.tone === 'dec' ? 'text-dec' : 'text-fct')} />
              <span className="flex-1 truncate">{t.name}</span>
              <span className="text-[11px] text-dim tabular-nums">{on ? `${tour!.beat + 1}/${t.beats.length}` : `${t.beats.length} beats`}</span>
            </button>
          )
        })}
        <button onClick={toggleMarket} title={`NYSE ${marketLabel}. Demo control: click to override the clock${marketOverridden ? ' (override on; click again to return to the clock)' : ''}`}
          className="mt-3 flex w-full cursor-pointer items-center justify-between rounded-ctl px-2 py-1 text-[12px] text-mute hover:bg-panel hover:text-bone">
          <span>US equities{marketOverridden && <span className="ml-1 font-mono text-[9.5px] text-warn">override</span>}</span>
          <span className="flex items-center gap-1.5 text-[11.5px]">
            <span aria-hidden className={cx('size-1.5 rounded-full', marketOpen ? 'bg-up' : 'bg-dim')} />
            {marketOpen ? 'Open' : 'Closed'}
          </span>
        </button>
      </div>
    </aside>
  )
}

/** Live UTC clock. Data timestamps come from each panel's source label, not from this clock. */
function Clock() {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(id)
  }, [])
  const day = now.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric', timeZone: 'UTC' })
  const time = now.toLocaleTimeString('en-GB', { hour12: false, timeZone: 'UTC' })
  return (
    <span className="flex items-baseline gap-1.5 text-[11px] whitespace-nowrap" title="Current time, UTC. Data timestamps are on each panel and on the ticker.">
      <span className="text-dim">Now</span>
      <span className="text-mute">{day}</span>
      <span className="num text-bone">{time}</span>
      <span className="text-dim">UTC</span>
    </span>
  )
}

export function Topbar() {
  const { setPalette, ask, trail, inspect, setTrail } = useStore()
  return (
    <header data-chrome className="flex h-12 shrink-0 items-center gap-3 border-b border-line bg-ink px-4 text-bone">
      <button onClick={() => setPalette(true)} className="flex h-8 w-[340px] min-w-[180px] shrink cursor-pointer items-center gap-2 rounded-ctl border border-line bg-panel px-2.5 text-left text-[12px] text-dim transition-colors duration-[120ms] hover:border-line2">
        <Search size={13} />
        <span className="flex-1 truncate">Region, GPU, provider, project, ticker, theme…</span>
        <span className="flex items-center gap-0.5 rounded-badge border border-line2 px-1 text-[10.5px]"><Command size={10} />K</span>
      </button>
      {trail.length > 0 && (
        <div className="flex min-w-0 flex-1 items-center gap-1 overflow-hidden">
          <span className="eyebrow mr-1 shrink-0">Trail</span>
          {trail.map((t, i) => (
            <span key={`${t.kind}-${t.id}`} className="flex shrink-0 items-center gap-1">
              {i > 0 && <ChevronRight size={11} className="text-dim" />}
              <button onClick={() => inspect(t)} className="max-w-[140px] cursor-pointer truncate rounded-ctl border border-line px-1.5 py-0.5 text-[11px] text-mute hover:border-line2 hover:text-bone">{t.label}</button>
            </span>
          ))}
          <button onClick={() => setTrail([])} title="Clear trail" className="ml-1 cursor-pointer text-dim hover:text-bone"><X size={12} /></button>
        </div>
      )}
      {trail.length === 0 && <div className="flex-1" />}
      <span className="hidden items-baseline gap-1.5 text-[11px] whitespace-nowrap xl:flex" title={`Numbers marked as a snapshot come from the ${SNAPSHOT.label}; everything else is labelled illustrative. Trades are simulated (paper trading only).`}>
        <span className="text-dim">Engine snapshot</span>
        <span className="text-mute">{SNAPSHOT.short.replace(/^Snapshot /, '')}</span>
      </span>
      <span aria-hidden className="hidden h-4 w-px bg-line xl:block" />
      <Clock />
      <span aria-hidden className="h-4 w-px bg-line" />
      <ChartStyleMenu />
      <MapSchemeMenu />
      <BlendSwitcher />
      <AuthControl />
      <Btn onClick={() => ask()} />
    </header>
  )
}

function Btn({ onClick }: { onClick: () => void }) {
  return (
    <button onClick={onClick} className="flex h-8 shrink-0 cursor-pointer items-center gap-1.5 rounded-ctl border border-line2 bg-raise px-3 text-[12px] font-medium text-bone transition-colors duration-[120ms] hover:bg-hover">
      <MessageSquareText size={13} /> Ask FLOPX
    </button>
  )
}

type TickerItem = { label: string; value: string; d: number | null; truth: 'obs' | 'inf'; onClick: () => void; suffix?: string; fresh: Freshness; title?: string; /** Drawn beside the delta, e.g. "30d · GetDeploying". */ note?: string; /** A row badge derived from the figure's own provenance; without one, 'illustrative' freshness shows "Illus." (WF7 #1). */ badge?: CloseBadge }

/** The CPI ticker badge: 'experimental' once the real cells have landed (cpx-v1 is an exploratory model), else what the data module set, else illustrative. */
export function cpiTickerFreshness(globalReal: boolean, designFresh: Freshness | undefined): Freshness {
  if (globalReal) return 'experimental'
  return designFresh ?? 'illustrative'
}

/**
 * The change a GPU index row shows: the 30-day change the Overview cards show (`GpuLive.chg30`,
 * FLOPX's own or GetDeploying's, with its label drawn beside the delta), else the index's own change
 * since its first point, as before.
 */
export function gpuTickerChange(live: Pick<GpuLive, 'chgPct' | 'chgWindow' | 'chg30'>): { d: number | null; note?: string; window: string } {
  const c = live.chg30
  if (c) return { d: c.pct, note: c.label, window: `${c.label} (${c.from} to ${c.to})` }
  return { d: live.chgPct, window: live.chgWindow }
}

/** Ticker inputs. GPU index items are live when the backend index has landed; every other item
 * reads the freshness its own data module sets, so a synthetic value always shows the badge. */
export function tickerItems(nav: (p: PageId) => void, inspect: (r: { kind: 'gpu' | 'energy' | 'security'; id: string; label: string }) => void): TickerItem[] {
  return [
    // The global CPI is the equal-weight mean of the backend's real cpx-v1 cells once /flopx/pressure
    // lands (PRESSURE_MODEL.globalReal): an inferred, experimental reading, not an illustrative one.
    { label: 'FLOPX CPI · GLOBAL', value: GLOBAL_CPI[NOW_IDX].toFixed(1), d: PRESSURE_MODEL.globalReal ? (PRESSURE_MODEL.globalPrev === null ? null : GLOBAL_CPI[NOW_IDX] - PRESSURE_MODEL.globalPrev) : GLOBAL_CPI[NOW_IDX] - GLOBAL_CPI[NOW_IDX - 1], truth: 'inf', onClick: () => nav('xray'), suffix: ' pts', fresh: cpiTickerFreshness(PRESSURE_MODEL.globalReal, (GLOBAL_CPI as number[] & { freshness?: Freshness }).freshness), title: PRESSURE_MODEL.globalReal ? `Inferred · ${PRESSURE_MODEL.method ?? 'cpx-v1'} mean of ${PRESSURE_MODEL.regions} FLOPX regions${PRESSURE_MODEL.globalPrev === null ? ' · no prior reading yet' : ''}` : 'Illustrative: FLOPX pressure not loaded yet' },
    ...GPUS.slice(0, 4).map((g): TickerItem => {
      const onClick = () => inspect({ kind: 'gpu', id: g.id, label: g.name })
      if (g.live) { const c = gpuTickerChange(g.live); return { label: `${g.name} IDX`, value: `$${g.live.current.toFixed(2)}`, d: c.d, note: c.note, truth: 'obs', onClick, fresh: g.live.freshness, title: `FLOPX index ${g.live.observed} UTC · change ${c.window}` } }
      return { label: `${g.name} IDX`, value: `$${g.index[NOW_IDX].toFixed(2)}`, d: ((g.index[NOW_IDX] - g.index[NOW_IDX - 1]) / g.index[NOW_IDX - 1]) * 100, truth: 'obs', onClick, fresh: 'illustrative', title: 'Illustrative: no FLOPX index for this GPU yet' }
    }),
    // The change is the 30-day change (`chg30`, the backend's changePct30D), so the label names the window (WF4 #19).
    ...ENERGY.slice(0, 3).map((e): TickerItem => ({ label: `${e.name} DA 30d`, value: `$${e.priceNow.toFixed(0)}`, d: e.chg30 ?? null, truth: 'obs', onClick: () => inspect({ kind: 'energy', id: e.id, label: e.name }), fresh: e.freshness ?? 'illustrative', title: `${e.operator} day-ahead price, latest; change over 30 days` })),
    // Each equity row is badged from the provenance of the close it shows: the series point placed
    // in the latest week (an archived alpaca-data close reads "Archived · 06 Nov" for a signed-in
    // reader), "Illus." when nothing observed landed there.
    ...SECURITIES.filter((s) => s.group !== 'Index').slice(0, 12).map((s): TickerItem => {
      const badge = closeBadgeOf(W2_LIVE.companies[s.ticker])
      return { label: s.ticker, value: last(s.px).toFixed(2), d: chg(s.px) ?? null, truth: 'obs', onClick: () => inspect({ kind: 'security', id: s.ticker, label: s.ticker }), fresh: badge.f, badge, title: badge.title }
    }),
  ]
}

export function Ticker() {
  const { inspect, navigate } = useStore()
  const items = tickerItems(navigate, inspect)
  const gpu = liveDomain('gpu')
  const phone = useIsPhone()
  if (phone) return <PhoneTicker items={items} gpu={gpu} />
  const row = (k: string) => (
    <div key={k} className="flex shrink-0 items-center">
      {items.map((it) => (
        <button key={k + it.label} onClick={it.onClick} title={it.title} className="flex h-7 shrink-0 cursor-pointer items-center gap-2 border-r border-line-soft px-3.5 text-[11px] whitespace-nowrap hover:bg-panel">
          <TruthGlyph truth={it.truth} size={6} />
          <span className="text-mute">{it.label}</span>
          <span className="num text-bone">{it.value}</span>
          {it.d == null || !Number.isFinite(it.d) ? <span className="num text-[10.5px] text-dim">n/a</span> : <Delta v={it.d} d={it.suffix ? 1 : 2} suffix={it.suffix ?? '%'} className="text-[10.5px]" />}
          {it.note && <span className="text-[10px] text-dim">{it.note}</span>}
          {it.badge ? <Fresh f={it.badge.f} label={it.badge.label} quiet /> : it.fresh === 'illustrative' && <Fresh f="illustrative" label="Illus." quiet />}
        </button>
      ))}
    </div>
  )
  return (
    <div data-chrome className="relative flex h-7 shrink-0 items-center overflow-hidden border-b border-line bg-ink text-bone">
      <div className="z-10 flex h-full shrink-0 items-center gap-1.5 border-r border-line bg-ink px-3 text-[11px] whitespace-nowrap text-dim" title={gpu.status === 'failed' ? `FLOPX backend unreachable (${gpu.error ?? 'error'}); values are illustrative` : undefined}>
        <span className={cx('size-1.5 rounded-full', gpu.status === 'live' ? 'bg-up' : gpu.status === 'failed' ? 'bg-down' : 'bg-warn')} />{' '}
        {gpu.status === 'live' ? <><span>FLOPX feed</span><span className="text-mute">{AS_OF.replace(' UTC', '')} UTC</span></> : gpu.status === 'failed' ? <span>Feed unavailable <span className="text-mute">· illustrative</span></span> : <><span>Connecting</span><span className="text-mute">{AS_OF.split(' ').slice(0, 3).join(' ')}</span></>}
      </div>
      <div className="min-w-0 flex-1 overflow-hidden">
        <div className="marquee flex w-max">{row('a')}{row('b')}</div>
      </div>
    </div>
  )
}

/**
 * Phone ticker: one row of the same items, swiped sideways instead of scrolling by itself (a marquee
 * under a thumb fights the swipe). The feed status leads the row. Rows are 44 px tall so each figure is
 * a tap target. The outer div keeps `data-chrome` and `h-7` because the walkthroughs find the ticker by them.
 */
function PhoneTicker({ items, gpu }: { items: TickerItem[]; gpu: ReturnType<typeof liveDomain> }) {
  return (
    <div data-chrome className="relative flex h-7 shrink-0 items-center overflow-hidden border-b border-line bg-ink text-bone max-md:h-[45px]">
      <div className="no-scrollbar flex h-full min-w-0 flex-1 snap-x snap-proximity items-center overflow-x-auto overscroll-x-contain" aria-label="Ticker, swipe sideways for more">
        <div className="flex h-full shrink-0 snap-start items-center gap-1.5 border-r border-line px-3 text-[12px] whitespace-nowrap text-dim" title={gpu.status === 'failed' ? `FLOPX backend unreachable (${gpu.error ?? 'error'}); values are illustrative` : undefined}>
          <span className={cx('size-1.5 rounded-full', gpu.status === 'live' ? 'bg-up' : gpu.status === 'failed' ? 'bg-down' : 'bg-warn')} />{' '}
          {gpu.status === 'live' ? <><span>FLOPX feed</span><span className="text-mute">{AS_OF.replace(' UTC', '')} UTC</span></> : gpu.status === 'failed' ? <span>Feed unavailable <span className="text-mute">· illustrative</span></span> : <><span>Connecting</span><span className="text-mute">{AS_OF.split(' ').slice(0, 3).join(' ')}</span></>}
        </div>
        {items.map((it) => (
          <button key={it.label} onClick={it.onClick} title={it.title} className="flex h-full shrink-0 cursor-pointer snap-start items-center gap-2 border-r border-line-soft px-3.5 text-[12px] whitespace-nowrap active:bg-panel">
            <TruthGlyph truth={it.truth} size={6} />
            <span className="text-mute">{it.label}</span>
            <span className="num text-bone">{it.value}</span>
            {it.d == null || !Number.isFinite(it.d) ? <span className="num text-[12px] text-dim">n/a</span> : <Delta v={it.d} d={it.suffix ? 1 : 2} suffix={it.suffix ?? '%'} className="text-[12px]" />}
            {it.note && <span className="text-[12px] text-dim">{it.note}</span>}
            {it.badge ? <Fresh f={it.badge.f} label={it.badge.label} quiet className="max-md:text-[12px]" /> : it.fresh === 'illustrative' && <Fresh f="illustrative" label="Illus." quiet className="max-md:text-[12px]" />}
          </button>
        ))}
      </div>
    </div>
  )
}

/** The four pages a phone reaches in one tap; every other page lives behind More. */
export const PHONE_TABS: { id: PageId; label: string; icon: ReactNode }[] = [
  { id: 'overview', label: 'Brief', icon: <LayoutDashboard size={20} strokeWidth={1.75} /> },
  { id: 'xray', label: 'X-Ray', icon: <ScanLine size={20} strokeWidth={1.75} /> },
  { id: 'markets', label: 'Markets', icon: <Cpu size={20} strokeWidth={1.75} /> },
  { id: 'fund', label: 'Fund', icon: <ChartLine size={20} strokeWidth={1.75} /> },
]

/** The page's name as the sidebar shows it, for the phone header. */
export function pageTitle(page: PageId): string {
  for (const s of NAV) for (const it of s.items) if (it.id === page) return it.label
  return 'FLOPX'
}

/** The pages behind More: every NAV page that is not a tab, grouped as the sidebar groups them. */
export function morePages(): { section: string; items: (typeof NAV)[number]['items'] }[] {
  const tabbed = new Set(PHONE_TABS.map((t) => t.id))
  return NAV.map((s) => ({ section: s.section, items: s.items.filter((it) => !tabbed.has(it.id)) })).filter((s) => s.items.length > 0)
}

const iconBtn = 'flex size-11 shrink-0 cursor-pointer items-center justify-center rounded-ctl text-mute transition-colors duration-[120ms] active:bg-raise active:text-bone'

/** Phone sign-in: an icon that opens the same dialog; signed in, it shows a green dot and opens More (where Sign out lives). */
function PhoneAuthButton({ onSignedIn }: { onSignedIn: () => void }) {
  const auth = useAuth()
  const [open, setOpen] = useState(false)
  if (!auth.signedIn || !auth.email) {
    return (
      <>
        <button onClick={() => setOpen(true)} aria-label="Sign in" title="Sign in to see the paper account and the archive series" className={iconBtn}><LogIn size={19} /></button>
        {open && <SignInDialog onClose={() => setOpen(false)} />}
      </>
    )
  }
  return (
    <button onClick={onSignedIn} aria-label={`Signed in as ${auth.email}. Account`} title={`Signed in as ${auth.email}`} className={cx(iconBtn, 'relative')}>
      <UserRound size={19} />
      <span aria-hidden className="absolute top-2.5 right-2.5 size-2 rounded-full border border-ink bg-up" />
    </button>
  )
}

/** Phone header: logo, the page's title, then search, Ask FLOPX and sign-in as 44 px icons. Sits under the status bar. */
export function PhoneTopbar({ onAccount }: { onAccount: () => void }) {
  const { route, setPalette, ask } = useStore()
  return (
    <header data-chrome className="pt-safe flex shrink-0 items-center gap-1 border-b border-line bg-ink pr-1 pl-3.5 text-bone">
      <div className="flex h-12 min-w-0 flex-1 items-center gap-2.5">
        <LogoMark />
        <h1 className="min-w-0 truncate text-[15px] font-semibold tracking-[-0.01em] text-bone">{pageTitle(route.page)}</h1>
      </div>
      <button onClick={() => setPalette(true)} aria-label="Search: region, GPU, provider, project, ticker, theme" title="Search" className={iconBtn}><Search size={19} /></button>
      <button onClick={() => ask()} aria-label="Ask FLOPX" title="Ask FLOPX" className={iconBtn}><MessageSquareText size={19} /></button>
      <PhoneAuthButton onSignedIn={onAccount} />
    </header>
  )
}

/** Phone bottom tab bar: Brief, X-Ray, Markets, Fund and More. Clears the home indicator. */
export function PhoneTabBar({ moreOpen, onMore }: { moreOpen: boolean; onMore: () => void }) {
  const { route, navigate } = useStore()
  const tabbed = PHONE_TABS.some((t) => t.id === route.page)
  const tab = (key: string, label: string, icon: ReactNode, on: boolean, onClick: () => void, extra?: Record<string, string>) => (
    <button key={key} onClick={onClick} aria-current={on && !extra ? 'page' : undefined} {...extra}
      className={cx('flex min-h-14 min-w-0 flex-1 cursor-pointer flex-col items-center justify-center gap-1 text-[12px] font-medium transition-colors duration-[120ms]', on ? 'text-bone' : 'text-dim active:text-mute')}>
      <span aria-hidden className={cx('flex h-6 w-12 items-center justify-center rounded-full transition-colors duration-[120ms]', on && 'bg-raise')}>{icon}</span>
      <span className="truncate">{label}</span>
    </button>
  )
  return (
    <nav data-chrome data-tabbar aria-label="Main" className="pb-safe flex shrink-0 border-t border-line bg-ink text-bone">
      {PHONE_TABS.map((t) => tab(t.id, t.label, t.icon, !moreOpen && route.page === t.id, () => navigate(t.id)))}
      {tab('more', 'More', <Ellipsis size={20} strokeWidth={1.75} />, moreOpen || !tabbed, onMore, { 'aria-expanded': String(moreOpen), 'aria-haspopup': 'dialog' })}
    </nav>
  )
}

/**
 * Phone More sheet, full screen: every page that is not a tab, the theme and chart controls, the
 * account, the three demo walkthroughs and the market clock override, so nothing the desktop sidebar
 * and top bar carry is out of reach on a phone.
 */
/**
 * Phone sheets and the back button: opening a sheet pushes a history entry with the same URL, so the
 * system back gesture closes the sheet and stays on the page, as a native app does (WF12 #6). Closing
 * the sheet any other way pops that entry again; a navigation from inside the sheet leaves it behind as
 * a harmless duplicate of the page it was opened on. Only phone-only components call this.
 */
export function useSheetBack(open: boolean, onClose: () => void) {
  const closeRef = useRef(onClose)
  closeRef.current = onClose
  useEffect(() => {
    if (!open) return
    let popped = false
    window.history.pushState({ mSheet: true }, '', window.location.href)
    const onPop = () => { popped = true; closeRef.current() }
    window.addEventListener('popstate', onPop)
    return () => {
      window.removeEventListener('popstate', onPop)
      if (!popped && (window.history.state as { mSheet?: boolean } | null)?.mSheet) window.history.back()
    }
  }, [open])
}

export function MoreSheet({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { route, navigate, tour, setTour, marketOpen, marketOverridden, marketLabel, toggleMarket } = useStore()
  useEffect(() => {
    if (!open) return
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', esc)
    return () => window.removeEventListener('keydown', esc)
  }, [open, onClose])
  useSheetBack(open, onClose)
  if (!open) return null
  const row = 'flex min-h-11 w-full cursor-pointer items-center gap-3 rounded-ctl px-3 text-left text-[14px] transition-colors duration-[120ms]'
  return (
    <div role="dialog" aria-modal="true" aria-label="More" className="pt-safe fixed inset-0 z-[70] flex flex-col bg-ink text-bone fade-in">
      <div className="flex h-12 shrink-0 items-center justify-between border-b border-line pr-1 pl-4">
        <span className="text-[15px] font-semibold">More</span>
        <button onClick={onClose} aria-label="Close" className={iconBtn}><X size={20} /></button>
      </div>
      <div className="pb-safe min-h-0 flex-1 overflow-y-auto overscroll-contain px-3 py-3">
        {morePages().map((s) => (
          <section key={s.section} className="mb-4">
            <div className="eyebrow px-3 pb-1 text-[12px]">{s.section}</div>
            {s.items.map((it) => (
              <button key={it.id} onClick={() => { navigate(it.id); onClose() }} aria-current={route.page === it.id ? 'page' : undefined}
                className={cx(row, route.page === it.id ? 'bg-raise text-bone' : 'text-mute active:bg-panel active:text-bone')}>
                <span className={route.page === it.id ? 'text-bone' : 'text-dim'}>{it.icon}</span>
                <span className="flex-1">{it.label}</span>
                {it.tag && <span className="rounded-badge border border-line2 px-1 text-[12px] font-medium tracking-wide text-dim">{it.tag}</span>}
              </button>
            ))}
          </section>
        ))}

        <section className="mb-4 border-t border-line pt-3">
          <div className="eyebrow px-3 pb-1 text-[12px]">Demo walkthroughs</div>
          {TOUR_IDS.map((id) => {
            const t = TOURS[id]
            const on = tour?.id === id
            return (
              <button key={id} onClick={() => { setTour(on ? null : startTour(id)); onClose() }} title={`${t.audience}. ${t.thesis}`} aria-current={on ? 'true' : undefined}
                className={cx(row, on ? 'bg-raise text-bone' : 'text-mute active:bg-panel active:text-bone')}>
                <span aria-hidden className={cx('size-2 shrink-0 rounded-[1.5px] bg-current', t.tone === 'inf' ? 'text-inf' : t.tone === 'dec' ? 'text-dec' : 'text-fct')} />
                <span className="min-w-0 flex-1 truncate">{t.name}</span>
                <span className="text-[12px] text-dim tabular-nums">{on ? `${tour!.beat + 1}/${t.beats.length}` : `${t.beats.length} beats`}</span>
              </button>
            )
          })}
        </section>

        <section className="mb-4 border-t border-line pt-3">
          <div className="eyebrow px-3 pb-2 text-[12px]">Display and account</div>
          <div className="m-touch-controls flex flex-wrap items-center gap-2 px-3">
            <BlendSwitcher />
            <ChartStyleMenu />
            <MapSchemeMenu />
            <AuthControl />
          </div>
          <button onClick={toggleMarket} title={`NYSE ${marketLabel}. Demo control: tap to override the clock${marketOverridden ? ' (override on; tap again to return to the clock)' : ''}`}
            className={cx(row, 'mt-2 justify-between text-mute active:bg-panel active:text-bone')}>
            <span>US equities{marketOverridden && <span className="ml-1 font-mono text-[12px] text-warn">override</span>}</span>
            <span className="flex items-center gap-1.5 text-[13px]">
              <span aria-hidden className={cx('size-1.5 rounded-full', marketOpen ? 'bg-up' : 'bg-dim')} />
              {marketOpen ? 'Open' : 'Closed'}
            </span>
          </button>
        </section>

        <section className="m-sheet-meta flex flex-col gap-2 border-t border-line px-3 pt-3 text-[12px]">
          <Clock />
          <span className="flex items-baseline gap-1.5 text-[12px]" title={`Numbers marked as a snapshot come from the ${SNAPSHOT.label}; everything else is labelled illustrative. Trades are simulated (paper trading only).`}>
            <span className="text-dim">Engine snapshot</span>
            <span className="text-mute">{SNAPSHOT.short.replace(/^Snapshot /, '')}</span>
          </span>
        </section>
      </div>
    </div>
  )
}

export function Toasts() {
  const { toasts } = useStore()
  return (
    <div className="pointer-events-none fixed bottom-4 left-1/2 z-[80] flex -translate-x-1/2 flex-col items-center gap-2 max-md:bottom-[calc(4.5rem+env(safe-area-inset-bottom))]">
      {toasts.map((t) => (
        <div key={t.id} className={cx('fade-in rounded-ctl border bg-raise px-3.5 py-2 text-[12px] shadow-2xl', t.tone === 'ok' && 'border-up/40 text-up', t.tone === 'bad' && 'border-down/40 text-down', t.tone === 'warn' && 'border-warn/40 text-warn', t.tone === 'info' && 'border-line2 text-bone')}>
          {t.text}
        </div>
      ))}
    </div>
  )
}
