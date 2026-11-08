import { useEffect, type ReactNode } from 'react'
import { StoreProvider, useStore, type PageId } from '@/lib/store'
import { Sidebar, Ticker, Toasts, Topbar } from '@/components/Shell'
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
import { BlendSwitcher } from '@/components/BlendSwitcher'
import { ChartStyleMenu } from '@/components/ChartStyleMenu'
import { ChartStyleProvider } from '@/lib/chartStyle'

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
  const Page = PAGES[route.page] ?? Overview
  const full = route.page === 'xray'
  return (
    <div className="flex h-full overflow-hidden bg-ink text-bone">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        {import.meta.env.VITE_DESIGN_PREVIEW === '1' && (
          <div className="border-b border-line bg-raise px-4 py-1 text-center text-[11px] text-mute">
            Design preview: every figure here is the design's illustrative data, not live FLOPX data. Live wiring lands in the next build.
          </div>
        )}
        <Topbar />
        <Ticker />
        <div className="flex min-h-0 flex-1">
          <div className="relative min-w-0 flex-1">
            <main id="main-scroll" className={full ? 'h-full overflow-hidden' : 'h-full overflow-y-auto'}>
              {full ? <Page /> : <div key={route.page} className="fade-in mx-auto max-w-[1680px] px-6 py-6"><Page /></div>}
            </main>
            <Inspector />
          </div>
          <Assistant />
        </div>
      </div>
      <Palette />
      <Ticket />
      <DemoGuide />
      <Toasts />
      <div className={assistant.open ? 'hidden' : 'fixed right-4 bottom-4 z-[60] flex items-center gap-2'}>
        <ChartStyleMenu />
        <BlendSwitcher />
      </div>
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
