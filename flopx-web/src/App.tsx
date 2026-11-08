import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { StoreProvider, useStore, type PageId } from '@/lib/store'
import { MoreSheet, PhoneTabBar, PhoneTopbar, Sidebar, Ticker, Toasts, Topbar } from '@/components/Shell'
import { Inspector } from '@/components/Inspector'
import { Assistant, DemoGuide, Palette, Ticket } from '@/components/Overlays'
import Overview from '@/pages/Overview'
import XRay from '@/pages/XRay'
import Fund from '@/pages/Fund'
import Markets from '@/pages/Markets'
import Energy from '@/pages/Energy'
import Securities from '@/pages/Securities'
import News from '@/pages/News'
import Research from '@/pages/Research'
import Trading from '@/pages/Trading'
import Procurement from '@/pages/Procurement'
import Handoff from '@/pages/Handoff'
import Intelligence from '@/pages/Intelligence'
import Agents from '@/pages/Agents'
import { ChartStyleProvider } from '@/lib/chartStyle'
import { useIsPhone } from '@/lib/usePhone'

const PAGES: Record<PageId, () => ReactNode> = {
  overview: Overview,
  xray: XRay,
  fund: Fund,
  markets: Markets,
  energy: Energy,
  securities: Securities,
  news: News,
  research: Research,
  trading: Trading,
  procurement: Procurement,
  handoff: Handoff,
  intelligence: Intelligence,
  agents: Agents,
}

function Frame() {
  const { route, setPalette, setInspector, inspector, assistant } = useStore()
  useEffect(() => {
    const fn = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setPalette(true)
      }
      if (e.key === 'Escape' && inspector) setInspector(null)
    }
    window.addEventListener('keydown', fn)
    return () => window.removeEventListener('keydown', fn)
  }, [setPalette, setInspector, inspector])
  // Phone (< 768 px): no sidebar or desktop top bar; a compact header, a swipeable ticker and a bottom
  // tab bar instead, with More as a full-screen sheet. With `phone` false every element below renders
  // exactly as it did, so the desktop layout does not move.
  const phone = useIsPhone()
  const [more, setMore] = useState(false)
  const closeMore = useCallback(() => setMore(false), [])
  useEffect(() => setMore(false), [route.page, phone])
  const Page = PAGES[route.page] ?? Overview
  const full = route.page === 'xray' || route.page === 'agents'
  return (
    <div className="flex h-full overflow-hidden bg-ink text-bone max-md:pr-[env(safe-area-inset-right)] max-md:pl-[env(safe-area-inset-left)]">
      {!phone && <Sidebar />}
      <div className="flex min-w-0 flex-1 flex-col">
        {import.meta.env.VITE_DESIGN_PREVIEW === '1' && (
          <div className="border-b border-line bg-raise px-4 py-1 text-center text-[11px] text-mute">
            Design preview: every figure here is the design's illustrative data, not live FLOPX data. Live wiring lands in the next build.
          </div>
        )}
        {phone ? (
          <div className="sticky top-0 z-40 shrink-0">
            <PhoneTopbar onAccount={() => setMore(true)} />
            <Ticker />
          </div>
        ) : (
          <>
            <Topbar />
            <Ticker />
          </>
        )}
        <div className="flex min-h-0 flex-1">
          <div className="relative min-w-0 flex-1">
            <main id="main-scroll" className={full ? 'h-full overflow-hidden' : 'h-full overflow-y-auto'}>
              {full ? <Page /> : <div key={route.page} className="fade-in mx-auto max-w-[1680px] px-6 py-6 max-md:px-3 max-md:py-4"><Page /></div>}
            </main>
            <Inspector />
          </div>
          <Assistant />
        </div>
        {phone && <PhoneTabBar moreOpen={more} onMore={() => setMore((o) => !o)} />}
      </div>
      {phone && <MoreSheet open={more} onClose={closeMore} />}
      <Palette />
      <Ticket />
      <DemoGuide />
      <Toasts />
    </div>
  )
}

export default function App() {
  return (
    <StoreProvider>
      <ChartStyleProvider>
        <Frame />
      </ChartStyleProvider>
    </StoreProvider>
  )
}
