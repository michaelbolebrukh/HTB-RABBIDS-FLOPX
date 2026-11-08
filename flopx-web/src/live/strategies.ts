// Live module 'strategies': the fund's strategy catalogue from /flopx/research/strategies (three
// today: divergence, supply-regime, event-reaction). The Fund's Strategies panel lists each one with
// its name, what it trades on and its live forward status, all read from the payload; nothing here
// is authored, so before the read lands the panel says the catalogue has not answered, and a failed
// read says why. (The founder read the evaluation's "24 tests" as 24 strategies; this panel is where
// the actual catalogue shows.)
import type { Getter, LiveModule } from './boot'
import { fmtObserved } from './provenance'

// ---------- Wire types (only the fields read here) ----------
export interface StrategyForwardWire { mode: string; decisions: unknown[]; orders: number; proposed: number; replays: number }
export interface StrategyWire {
  id: string
  name: string
  objective: string
  rule: string
  parameters: Record<string, unknown>
  universe: string[]
  backtest: unknown | null
  forward: StrategyForwardWire | null
}
export interface StrategiesResponse { strategies: StrategyWire[]; freshness: { generatedAt: string; sources?: string[] } }

// ---------- What the page reads ----------
export type StrategyStatusKind = 'idle' | 'proposed' | 'active'
export interface StrategyLive {
  id: string
  name: string
  /** One line: the universe and the driver, e.g. "CRWV · H100 on-demand index vs the CRWV and NBIS pair". */
  tradesOn: string
  universe: string[]
  objective: string
  rule: string
  /** "Forward paper · no decision yet", "Forward paper · 2 proposed", "Forward paper · 3 orders · 1 proposed". */
  status: string
  statusKind: StrategyStatusKind
  hasBacktest: boolean
}
export interface StrategiesState {
  status: 'pending' | 'live' | 'failed'
  strategies: StrategyLive[]
  generatedAt: string | null
  /** "08 Nov 00:40", for the panel's note. */
  when: string | null
  error: string | null
}
export const STRATEGIES_LIVE: StrategiesState = { status: 'pending', strategies: [], generatedAt: null, when: null, error: null }

const GPU_WORD: Record<string, string> = { h100: 'H100', h200: 'H200', b200: 'B200', a100: 'A100', l40s: 'L40S' }

/** "gpu:h100-sxm-80gb:on-demand:global" -> "H100 on-demand index"; anything else is returned as given. */
export function indexWord(id: string): string {
  const parts = id.split(':')
  if (parts[0] !== 'gpu' || parts.length < 3) return id
  const model = parts[1].split('-')[0]
  return `${GPU_WORD[model] ?? parts[1]} ${parts[2]} index`
}

/** What a strategy trades on: its universe, then the driver named by its parameters. */
export function tradesOnOf(s: StrategyWire): string {
  const u = s.universe.length > 4 ? `${s.universe.length} covered names` : s.universe.join(', ')
  const p = s.parameters ?? {}
  if (typeof p.index === 'string') {
    const spread = s.universe.length > 1 ? ` vs the ${s.universe.join(' and ')} pair` : ''
    return `${u} · ${indexWord(p.index)}${spread}`
  }
  if (Array.isArray(p.kinds) && p.kinds.length) return `${u} · verified ${p.kinds.join(' and ')} items`
  return u
}

/** The forward status, from counts only: never a number the payload does not carry. */
export function statusOf(f: StrategyForwardWire | null): { status: string; kind: StrategyStatusKind } {
  if (!f) return { status: 'Not running', kind: 'idle' }
  const mode = f.mode === 'forward' ? 'Forward paper' : f.mode
  if (f.orders > 0) return { status: `${mode} · ${f.orders} order${f.orders === 1 ? '' : 's'}${f.proposed ? ` · ${f.proposed} proposed` : ''}`, kind: 'active' }
  if (f.proposed > 0) return { status: `${mode} · ${f.proposed} proposed`, kind: 'proposed' }
  if (f.decisions.length > 0) return { status: `${mode} · ${f.decisions.length} decision${f.decisions.length === 1 ? '' : 's'}, no order`, kind: 'proposed' }
  return { status: `${mode} · no decision yet`, kind: 'idle' }
}

export function adaptStrategies(p: StrategiesResponse): StrategyLive[] {
  return p.strategies.map((s) => {
    const { status, kind } = statusOf(s.forward)
    return { id: s.id, name: s.name, tradesOn: tradesOnOf(s), universe: s.universe, objective: s.objective, rule: s.rule, status, statusKind: kind, hasBacktest: s.backtest != null }
  })
}

export function applyStrategies(p: StrategiesResponse): void {
  STRATEGIES_LIVE.status = 'live'
  STRATEGIES_LIVE.strategies = adaptStrategies(p)
  STRATEGIES_LIVE.generatedAt = p.freshness.generatedAt
  STRATEGIES_LIVE.when = fmtObserved(p.freshness.generatedAt)
  STRATEGIES_LIVE.error = null
}

/** Test helper. */
export function __resetStrategies(): void {
  STRATEGIES_LIVE.status = 'pending'
  STRATEGIES_LIVE.strategies = []
  STRATEGIES_LIVE.generatedAt = null
  STRATEGIES_LIVE.when = null
  STRATEGIES_LIVE.error = null
}

export const strategiesModule: LiveModule = {
  id: 'strategies',
  async load(get: Getter, apply: () => void) {
    const r = await get<StrategiesResponse>('/flopx/research/strategies')
    if (!r.ok) {
      STRATEGIES_LIVE.status = 'failed'
      STRATEGIES_LIVE.error = r.error
      apply()
      return false
    }
    applyStrategies(r.data)
    apply()
    return true
  },
}
