import type { ReactNode } from 'react'
import { AlertTriangle, ArrowRight, Ban } from 'lucide-react'
import type { Freshness, Truth } from '@/data/core'
import { MONTHS, NOW_IDX, regionById } from '@/data/core'
import { OrderStatus } from './Fund'
import { Foldable, Row } from '@/components/collapse'
import { Btn, cx, Empty, Fresh, PageHeader, Panel, TRUTH, TruthGlyph, TruthTag, Validation } from '@/components/ui'
import { useStore, type Store } from '@/lib/store'
import { CLAIMS, NEVER_CLAIM, SNAPSHOT } from '@/data/handoff'

const ENCODING: Record<Truth, { line: ReactNode; colour: string; mark: string; stroke: string; must: string; examples: string }> = {
  obs: { line: <line x1="0" y1="5" x2="44" y2="5" stroke="var(--color-obs)" strokeWidth="2" />, colour: 'Bone', mark: 'Solid square', stroke: 'Solid', must: 'Source and timestamp', examples: 'Rental quotes, power prices, filings, reported MW' },
  inf: { line: <><rect x="0" y="1" width="44" height="8" fill="none" stroke="var(--color-inf)" /><line x1="4" y1="9" x2="10" y2="1" stroke="var(--color-inf)" /><line x1="14" y1="9" x2="20" y2="1" stroke="var(--color-inf)" /><line x1="24" y1="9" x2="30" y2="1" stroke="var(--color-inf)" /><line x1="34" y1="9" x2="40" y2="1" stroke="var(--color-inf)" /></>, colour: 'Amber', mark: 'Hatched fill, uncertainty band or ring', stroke: 'Hatched', must: '± and its drivers', examples: 'Compute Pressure Index, usable MW, sensitivities' },
  fct: { line: <line x1="0" y1="5" x2="44" y2="5" stroke="var(--color-fct)" strokeWidth="2" strokeDasharray="5 4" />, colour: 'Ice-blue', mark: 'Open circle', stroke: 'Dashed; diagonal-hatched zone after NOW', must: 'Horizon, distribution and validation status', examples: 'Index forecasts, return distributions, scenarios' },
  dec: { line: <rect x="16" y="0" width="10" height="10" transform="rotate(45 21 5)" fill="var(--color-dec)" />, colour: 'Blue', mark: 'Diamond marker', stroke: 'None', must: 'Owner (model or user), reasoning, risk and execution status', examples: 'Proposals, paper orders, decision records' },
}

/** Card 01 reads the live Texas cell (score ± the region's uncertainty) once /flopx/pressure lands; the design's 82 ± 6 only before that, badged. */
function TexasPressureDemo() {
  useStore()
  const r = regionById('us-tx')
  const p = r?.live?.pressure
  const score = p ? Math.round(p.score) : 82
  const unc = r?.unc ?? 6
  return <div className="flex items-center gap-2"><TruthTag truth="inf" /><span className="num text-[18px] text-inf">{score}</span><span className="num text-[11px] text-mute">± {unc}</span>{p ? <span className="font-mono text-[10px] text-dim">{p.band} · live</span> : <Fresh f="illustrative" />}</div>
}

const STATES: { name: string; where: string; demo: ReactNode; go: (s: Store) => void }[] = [
  { name: 'Populated data', where: 'X-Ray · Texas', demo: <TexasPressureDemo />, go: (s) => { s.setXray({ region: 'us-tx', tab: 'brief' }); s.navigate('xray') } },
  { name: 'No-data region', where: 'X-Ray · Johannesburg', demo: <div className="flex items-center gap-2 text-[11.5px] text-dim"><svg width="18" height="18"><circle cx="9" cy="9" r="7" fill="none" stroke="var(--color-dim)" strokeDasharray="2 2" /><line x1="4" y1="14" x2="14" y2="4" stroke="var(--color-dim)" /></svg>No data · no estimate shown</div>, go: (s) => { s.setXray({ region: 'za-jnb' }); s.navigate('xray') } },
  { name: 'Stale data', where: 'X-Ray · Sydney, Markets', demo: <div className="flex items-center gap-2"><Fresh f="stale" /><span className="font-mono text-[10.5px] text-dim">expected update missed (design example)</span></div>, go: (s) => { s.setXray({ region: 'au-syd', tab: 'evidence' }); s.navigate('xray') } },
  { name: 'Source unavailable', where: 'Securities · IREN derivatives', demo: <div className="flex items-center gap-2"><Fresh f="unavailable" /><span className="text-[11.5px] text-mute">Options feed: nothing shown</span></div>, go: (s) => s.navigate('securities', { t: 'IREN' }) },
  { name: 'Conflicting evidence', where: 'X-Ray · Texas anomaly Q2', demo: <div className="rounded-[2px] border border-warn/40 px-2 py-0.5 text-[11px] text-warn">Conflicts · listings <span className="num">+6.1%</span></div>, go: (s) => { s.setXray({ region: 'us-tx', tab: 'brief' }); s.navigate('xray') } },
  { name: 'High uncertainty', where: 'X-Ray · UAE, Mumbai', demo: <svg width="60" height="22"><circle cx="30" cy="11" r="5" fill="var(--color-inf)" opacity=".7" /><circle cx="30" cy="11" r="10" fill="none" stroke="var(--color-inf)" strokeDasharray="1 3" /></svg>, go: (s) => { s.setXray({ region: 'ae-uae' }); s.navigate('xray') } },
  { name: 'Insufficient history', where: 'Fund · 3M horizon', demo: <Validation s="insufficient" />, go: (s) => { s.setFund({ tab: 'signals', signal: 'sig-nordic', horizon: '1M' }); s.navigate('fund') } },
  { name: 'No model edge found', where: 'Fund · NVIDIA signal', demo: <Validation s="no-edge" />, go: (s) => { s.setFund({ tab: 'signals', signal: 'sig-chips' }); s.navigate('fund') } },
  { name: 'Market closed', where: 'Sidebar toggle · Trading', demo: <span className="flex items-center gap-1.5 text-[11.5px] text-warn"><span className="size-1.5 rounded-full bg-warn" />Closed · orders queue</span>, go: (s) => s.navigate('trading') },
  { name: 'Paper order pending', where: 'Fund · Decisions', demo: <OrderStatus s="Pending" />, go: (s) => { s.setFund({ tab: 'decisions' }); s.navigate('fund') } },
  { name: 'Paper order rejected', where: 'Trading · Orders', demo: <div className="flex items-center gap-2"><OrderStatus s="Rejected" /><span className="text-[11px] text-down">No borrow</span></div>, go: (s) => s.navigate('trading') },
  { name: 'Portfolio empty', where: 'Fund · after Liquidate all', demo: <span className="text-[11.5px] text-mute">“All paper positions closed…”</span>, go: (s) => { s.setFund({ tab: 'portfolio' }); s.navigate('fund') } },
  { name: 'Future hypothetical scenario', where: 'X-Ray · What-if', demo: <div className="hatch-sim rounded-[2px] border border-dashed border-dec/50 px-2 py-0.5 text-[11px] text-dec">What-if · TX +12 mo</div>, go: (s) => { s.setXray({ region: 'us-tx', tab: 'whatif', scenario: { region: 'us-tx', slipMonths: 12, powerShock: 0 }, t: NOW_IDX + 6 }); s.navigate('xray') } },
  { name: 'ERCOT power is simulated', where: 'Energy · ERCOT', demo: <div className="flex items-center gap-2"><Fresh f="simulated" /><span className="text-[11.5px] text-mute">No free source carries ERCOT prices</span></div>, go: (s) => s.navigate('energy') },
  { name: 'Intelligence market snapshot', where: 'Intelligence', demo: <div className="flex items-center gap-2"><Fresh f="historical" label={SNAPSHOT.short} /></div>, go: (s) => s.navigate('intelligence') },
  { name: 'Agent proposes, person decides', where: 'Agents', demo: <div className="flex items-center gap-2"><TruthTag truth="dec" /><span className="text-[11.5px] text-mute">Nothing runs unapproved</span></div>, go: (s) => s.navigate('agents') },
]

const SPECS: { area: string; items: [string, string][] }[] = [
  { area: 'Compute X-Ray', items: [
    ['Reveal control', 'The button animates the raw→inferred blend over 1.4s (ease-in-out). The slider gives manual control. Raw layers fade to 25% opacity and are never fully hidden.'],
    ['Perspectives', 'Layers combine additively. The pressure layer is amber and hatched, observed layers are bone. Each toggle carries its truth glyph.'],
    ['Pressure rings', 'Ring radius grows with capacity and inferred pressure; the glow and dotted uncertainty ring scale with it. Sparse or experimental coverage adds a second, wider dotted ring.'],
    ['Continent callouts', 'With the pressure layer on, each continent gets one callout on its biggest ring: region, pressure, level and H100 premium. Callouts are placed to avoid region labels, recompute as the scrubber moves, and fade with Reveal.'],
    ['Time scrubber', `Monthly from ${MONTHS[0]} to ${MONTHS[MONTHS.length - 1]}. Past NOW the track is diagonally hatched, the thumb becomes an open ring, and the map banner switches to FORECAST. Uncertainty rings widen with √horizon.`],
    ['Region select', 'Opens the right panel with Anomaly, Evidence, Pipeline, Exposure and What-if tabs. The investigation is kept in global state, so leaving and returning restores it.'],
    ['Compare', 'Click two regions (A/B badges) or pick two GPU categories. The overlay chart puts both on one axis with forecast bands.'],
    ['What-if', 'Sliders mutate only estimated quantities. A blue dashed ring marks affected regions. Assumptions are always listed, and bands widen 25%.'],
  ] },
  { area: 'Systematic Fund', items: [
    ['Equity curve', 'Weekly NAV candles over volume and drawdown panes on one time axis with a shared crosshair. The paper period is shaded blue after the PAPER line. ◆ annotations open the decision record (information → prediction → decision → outcome). Hollow ◆ = backtest replay.'],
    ['Horizon switch', 'Changing the horizon changes the distribution, hit rate, n and validation status together. There is never a single confidence number.'],
    ['Proposals', 'Send to paper, Modify qty or Reject. Every action is logged with its origin. Fills take ~1.4s. Closed market → Queued. Limit breach → Rejected with a reason.'],
    ['Positions', 'Reduce 50%, Close and Liquidate all (with confirm). Open orders disable row actions to prevent double-submission.'],
  ] },
  { area: 'Charts', items: [
    ['Chart type', 'The equity curve’s type menu offers 13 views of the same weekly data: bars, candles, hollow, volume and Heikin Ashi candles; line, markers and step; area, HLC area and baseline; columns and high-low.'],
    ['Indicators', '22 technical indicators in a searchable dialog. Overlays (moving averages, bands, SAR, VWAP) draw on price; oscillators (RSI, MACD, ADX, OBV…) each get their own pane. Active ones show as removable chips and in the readout. They are descriptive only and never feed signals.'],
    ['Chart style', 'The Charts menu (bottom right) sets curve, line weight, grid, points, area fill, label font, tick density and animation for every chart. Saved per browser, with a Reset.'],
  ] },
  { area: 'Global', items: [
    ['Theme', 'Dark (terminal) and Light (paper) themes switch from the bottom-right toggle and persist. Truth colours deepen on paper so they keep contrast; map pressure glows use a lighter ramp.'],
    ['Inspector', 'Any underlined entity opens a 440px drawer showing the chain infrastructure → economics → companies → forecasts → decisions.'],
    ['Trail', 'The last 6 inspected entities appear in the top bar as breadcrumbs so the investigation is kept across screens.'],
    ['⌘K', 'Search across regions, GPUs, providers, projects, energy markets, tickers, baskets, signals, events and themes.'],
    ['Assistant', 'Context-aware. Retrieved facts (numbered, sourced) are kept apart from generated interpretation (dashed, ice-blue) and come with a confidence label and suggested next steps.'],
  ] },
]

export default function Handoff() {
  const store = useStore()
  return (
    <div>
      <PageHeader eyebrow="System · design handoff" title="Truth system" sub="Every number or mark in FLOPX belongs to one of four truth categories, each encoded in colour, shape and line." about={<>Each category has a redundant encoding in colour, shape and line style, so the meaning survives greyscale, colour-blindness and a 30-second glance. <b className="font-medium text-bone">Hue means a truth category or a direction; orange marks only your current selection</b>; category colours appear only inside charts that carry a legend.</>} />

      <Foldable label="Truth categories" className="mb-6">
      <div className="overflow-x-auto rounded-md border border-line bg-panel">
        <table className="w-full text-[12px]">
          <thead>
            <tr className="h-8 border-b border-line text-left text-[11px] font-medium text-dim">
              {['Category', 'Colour', 'Mark', 'Line', 'Must show', 'e.g.'].map((h) => <th key={h} className="px-3 font-medium">{h}</th>)}
            </tr>
          </thead>
          <tbody className="divide-y divide-line-soft">
            {(Object.keys(TRUTH) as Truth[]).map((t) => (
              <tr key={t} className="align-top">
                <td className="px-3 py-3">
                  <div className="flex items-center gap-2"><TruthGlyph truth={t} size={14} /><span className="text-[16px] font-semibold tracking-tight" style={{ color: TRUTH[t].color }}>{TRUTH[t].label}</span></div>
                  <div className="mt-1 max-w-[260px] text-[11.5px] text-bone/90">{TRUTH[t].desc}</div>
                </td>
                <td className="px-3 py-3 text-mute">{ENCODING[t].colour}</td>
                <td className="px-3 py-3 text-mute">{ENCODING[t].mark}</td>
                <td className="px-3 py-3"><svg width="44" height="10" className="mb-1">{ENCODING[t].line}</svg><div className="text-[11.5px] text-mute">{ENCODING[t].stroke}</div></td>
                <td className="px-3 py-3 text-mute">Always: {ENCODING[t].must}</td>
                <td className="px-3 py-3 text-[11px] text-dim">{ENCODING[t].examples}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      </Foldable>

      <Row cols={[1, 1]} className="mb-6">
        <Panel title="Data status" eyebrow="Independent of truth category">
          <div className="grid grid-cols-2 gap-3">
            {([
              ['live', 'Streaming, < 5 min old'], ['delayed', 'Licensed delay (e.g. 15 min)'], ['historical', 'Point-in-time record'], ['stale', 'Expected update missed'],
              ['unavailable', 'Source down or never covered'], ['experimental', 'Model or coverage in trial'], ['simulated', 'Paper execution, not real'], ['illustrative', 'Fictional design data'],
            ] as [Freshness, string][]).map(([f, d]) => <div key={f} className="flex items-center gap-3"><Fresh f={f} className="w-[110px] justify-center" /><span className="text-[11.5px] text-mute">{d}</span></div>)}
          </div>
          <p className="mt-3 text-[11.5px] text-mute">A figure with no status label is real: observed or reported, with its source. A panel whose rows all share one status says so once, in its eyebrow or foot, as a muted note (<Fresh quiet f="illustrative" />); a row that differs from the rest keeps its own chip. The bordered chip is kept for a single headline figure.</p>
        </Panel>
        <Panel title="Validation status" eyebrow="For every forecast at every horizon">
          <div className="space-y-2.5">
            {([['validated', 'Beats baseline out-of-sample with ≥ 12 independent windows; CI excludes zero.'], ['exploratory', 'Promising but under-sampled. Sized at half or less, manual approval required.'], ['insufficient', 'Too little history. Shown for research, never used for sizing.'], ['no-edge', 'Tested and rejected. Kept visible so it isn’t re-discovered.']] as const).map(([s, d]) => <div key={s} className="flex items-center gap-3"><span className="w-[160px]"><Validation s={s} /></span><span className="text-[11.5px] text-mute">{d}</span></div>)}
          </div>
          <p className="mt-3 text-[11.5px] text-mute">Validation icons are one ring in the badge’s colour, filled by how much evidence there is, so each status reads by fill alone: full = validated, half = exploratory, quarter = insufficient history, empty = no edge.</p>
          <div className="mt-4 border-t border-line pt-3">
            <div className="eyebrow mb-1.5">Relationships</div>
            <div className="flex items-center gap-3 text-[11.5px] text-mute"><span className="inline-block h-px w-6 bg-bone" />Documented tie (filing, press): fact</div>
            <div className="mt-1 flex items-center gap-3 text-[11.5px] text-mute"><span className="inline-block w-6 border-t border-dashed border-inf" />Modelled sensitivity (β, R²): association, never shown as causal</div>
          </div>
        </Panel>
      </Row>

      <Row cols={[2, 1]} className="mb-8">
        <Panel title="Honest numbers" eyebrow={<Fresh f="historical" label={SNAPSHOT.short} />} bodyClass="p-0">
          <table className="w-full text-[12px]">
            <thead>
              <tr className="h-8 border-b border-line text-[11px] text-dim">
                {['Claim', 'Number', 'File', ''].map((h, i) => <th key={i} className="px-3 text-left font-medium" title={i === 3 ? 'Re-check before demo' : undefined}>{h}</th>)}
              </tr>
            </thead>
            <tbody>
              {CLAIMS.map((c) => (
                <tr key={c.claim} className="border-b border-line-soft align-top">
                  <td className="px-3 py-1.5 font-medium">{c.claim}</td>
                  <td className="num px-3 py-1.5 text-[11px] text-mute">{c.number}</td>
                  <td className="px-3 py-1.5 font-mono text-[10.5px] text-dim">{c.file}</td>
                  <td className="px-3 py-1.5" title={c.recheck ? 'Re-check before demo' : 'No re-check needed'}><span className={cx('inline-block h-2 w-2 rounded-full', c.recheck ? 'bg-warn' : 'border border-line2')} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
        <Panel variant="rail" title="Never claim">
          {NEVER_CLAIM.map((n) => <div key={n} title={n} className="flex gap-2 py-1.5 text-[12px] text-mute"><span className="shrink-0 text-warn" aria-hidden>✕</span><span className="truncate">{n.split(':')[0]}</span></div>)}
        </Panel>
      </Row>

      <h2 className="mb-1 text-[16px] font-semibold tracking-tight" title="Each state is live in the product. Use “Show me” to jump to it.">Required states</h2>
      <p className="mb-4 text-[12.5px] text-mute" title="Each state is live in the product. Use “Show me” to jump to it.">No state may suggest stronger evidence than exists.</p>
      <Foldable label="State gallery" className="mb-8">
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
        {STATES.map((s, i) => (
          <div key={s.name} className="flex flex-col rounded-md border border-line bg-panel p-4">
            <div className="mb-3 flex items-center justify-between"><span className="text-[13px] font-semibold">{s.name}</span><span className="num text-[10px] text-dim">{String(i + 1).padStart(2, '0')}</span></div>
            <div className="mb-3 flex min-h-[40px] items-center rounded-[4px] border border-line/60 bg-ink px-3 py-2">{s.demo}</div>
            <div className="mt-auto flex items-center justify-between"><span className="text-[11px] text-dim">{s.where}</span><button onClick={() => s.go(store)} className="flex cursor-pointer items-center gap-1 text-[11.5px] text-mute hover:text-bone">Show me <ArrowRight size={11} /></button></div>
          </div>
        ))}
      </div>
      </Foldable>

      <Row cols={[1, 1, 1]} labels={['No data', 'Degraded source', 'Action rejected']} className="mb-8">
        <Empty icon={<Ban size={18} />} title="Empty: no data" body="Used where coverage is absent. Says what is unknown and why. Never extrapolates." />
        <Empty tone="warn" icon={<AlertTriangle size={18} />} title="Warning: degraded source" body="Used for stale feeds or thin liquidity. Estimates widen automatically." />
        <Empty tone="bad" icon={<Ban size={18} />} title="Error: action rejected" body="Used for rejected paper orders. Always gives the specific reason (limit, borrow, buying power)." action={<Btn size="sm" variant="ghost">Retry with smaller size</Btn>} />
      </Row>

      <h2 className="mb-4 text-[16px] font-semibold tracking-tight">Interaction specification</h2>
      <div className="mb-8 grid grid-cols-1 gap-5 md:grid-cols-2">
        {SPECS.map((sp) => (
          <Panel key={sp.area} title={sp.area}>
            <dl className="space-y-3">
              {sp.items.map(([k, v]) => <div key={k}><dt className="text-[12px] font-semibold text-bone">{k}</dt><dd className="text-[11.5px] leading-relaxed text-mute">{v}</dd></div>)}
            </dl>
          </Panel>
        ))}
      </div>

      <h2 className="mb-4 text-[16px] font-semibold tracking-tight">The unifying chain</h2>
      <Foldable label="Chain stages">
      <div className="grid grid-cols-2 gap-px overflow-hidden rounded-md border border-line bg-line md:grid-cols-6">
        {[
          ['Physical infrastructure', 'Energy & Infra · X-Ray pipeline', 'obs'],
          ['Compute economics', 'Markets · X-Ray pressure', 'inf'],
          ['Companies & instruments', 'Equities · X-Ray exposure', 'obs'],
          ['Forecasts', 'Fund signals · Research', 'fct'],
          ['Investment decisions', 'Fund decisions · Trading', 'dec'],
          ['Observed outcomes', 'Decision records · Evaluation', 'obs'],
        ].map(([k, v, t], i) => (
          <div key={k} className={cx('relative bg-panel p-4')}>
            <div className="num mb-1 text-[10px] text-dim">0{i + 1}</div>
            <TruthGlyph truth={t as Truth} size={10} />
            <div className="mt-2 text-[13px] font-semibold">{k}</div>
            <div className="mt-0.5 text-[11px] text-dim">{v}</div>
          </div>
        ))}
      </div>
      </Foldable>
    </div>
  )
}
