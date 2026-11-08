// The fund's strategy catalogue (Fund, Portfolio tab): what /flopx/research/strategies lists, each
// with its name, what it trades on and its live forward status. Read-only from the live module
// (src/live/strategies.ts); before the read lands, or when it fails, the panel says so in place of
// rows. Nothing here is a mock figure.
import { cx, Fresh, Panel } from '@/components/ui'
import { useStore } from '@/lib/store'
import { STRATEGIES_LIVE, type StrategyStatusKind } from '@/live/strategies'

const DOT: Record<StrategyStatusKind, string> = { idle: 'bg-mute', proposed: 'bg-dec', active: 'bg-up' }

export function StrategiesPanel({ className }: { className?: string }) {
  useStore() // re-renders when the live module lands (useLiveVersion inside the store)
  const s = STRATEGIES_LIVE
  return (
    <Panel
      className={className}
      title="Strategies"
      eyebrow={<>The fund's catalogue · CPX-1 runs these, not the evaluation's 24 forecast tests {s.status === 'live' ? <Fresh quiet f="live" label={`FLOPX strategy catalogue · ${s.when}`} /> : s.status === 'failed' ? <Fresh quiet f="unavailable" label="Catalogue read failed" /> : <Fresh quiet f="unavailable" label="Catalogue not read yet" />}</>}
      right={s.status === 'live' ? <span className="num text-[11px] text-dim">{s.strategies.length} strategies</span> : undefined}
    >
      {s.status === 'pending' && <div className="text-[12px] text-mute">The strategy catalogue has not answered yet (reading /flopx/research/strategies). No strategy is listed until it does.</div>}
      {s.status === 'failed' && <div className="text-[12px] text-mute">The strategy catalogue could not be read: {s.error}. No strategy is listed in its place.</div>}
      {s.status === 'live' && s.strategies.length === 0 && <div className="text-[12px] text-mute">The backend lists no strategy.</div>}
      {s.status === 'live' && s.strategies.length > 0 && (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
          {s.strategies.map((x) => (
            <div key={x.id} className="min-w-0 border-l-2 border-line2 pl-3" title={x.rule}>
              <div className="text-[12.5px] font-medium text-bone">{x.name}</div>
              <div className="mt-0.5 text-[11.5px] text-mute"><span className="eyebrow mr-1.5">Trades on</span>{x.tradesOn}</div>
              <div className="mt-1 flex items-center gap-1.5 font-mono text-[10.5px] text-dim"><span className={cx('size-1.5 rounded-full', DOT[x.statusKind])} />{x.status}{x.hasBacktest ? '' : ' · no backtest published'}</div>
            </div>
          ))}
        </div>
      )}
    </Panel>
  )
}
