import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { ArrowRight, Bot, CornerDownLeft, Search, Send, Sparkles, X } from 'lucide-react'
import { regionById } from '@/data/core'
import { secByTicker } from '@/data/finance'
import { lastPx } from '@/data/fund'
import { useStore, type EntityRef, type Order, type PageId, type Store } from '@/lib/store'
import { TourStage } from '@/tour/Stage'
import { ALL_ENTITIES } from './Inspector'
import { NAV } from './Shell'
import { useIsPhone } from '@/lib/usePhone'
import { Btn, cx, Fresh, Seg, SHEET_CLOSE, SHEET_FULL, SheetBackdrop, SheetHandle, TruthGlyph, TruthTag } from './ui'

// ---------------- Command palette ----------------
export function Palette() {
  const { palette, setPalette, inspect, navigate } = useStore()
  const [q, setQ] = useState('')
  const [sel, setSel] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)
  const phone = useIsPhone()
  const sheetRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (palette) { setQ(''); setSel(0); setTimeout(() => inputRef.current?.focus(), 10) }
  }, [palette])
  const pages = NAV.flatMap((s) => s.items).map((p) => ({ type: 'page' as const, id: p.id, label: p.label }))
  const results = useMemo(() => {
    const s = q.trim().toLowerCase()
    const ents = ALL_ENTITIES.filter((e) => !s || e.label.toLowerCase().includes(s) || e.id.toLowerCase().includes(s) || e.kind.includes(s)).map((e) => ({ type: 'entity' as const, e }))
    const pg = pages.filter((p) => !s || p.label.toLowerCase().includes(s))
    return [...pg.map((p) => ({ type: 'page' as const, p })), ...ents].slice(0, 14)
  }, [q])
  if (!palette) return null
  const choose = (i: number) => {
    const r = results[i]
    if (!r) return
    if (r.type === 'page') navigate(r.p.id as PageId)
    else inspect(r.e)
    setPalette(false)
  }
  return (
    <div className="fixed inset-0 z-[90] flex items-start justify-center bg-black/60 pt-[12vh] backdrop-blur-[2px] max-md:z-[95] max-md:pt-0" onClick={() => setPalette(false)}>
      <div ref={sheetRef} className={cx('fade-in w-[620px] overflow-hidden rounded-lg border border-line2 bg-panel shadow-2xl', SHEET_FULL, 'max-md:flex max-md:flex-col', phone && 'm-sheet')} onClick={(e) => e.stopPropagation()}>
        {phone && <SheetHandle sheet={sheetRef} onClose={() => setPalette(false)} />}
        <div className="flex items-center gap-2 border-b border-line px-4 max-md:shrink-0">
          <Search size={15} className="text-dim" />
          <input
            ref={inputRef}
            value={q}
            onChange={(e) => { setQ(e.target.value); setSel(0) }}
            onKeyDown={(e) => {
              if (e.key === 'ArrowDown') { e.preventDefault(); setSel((s) => Math.min(results.length - 1, s + 1)) }
              if (e.key === 'ArrowUp') { e.preventDefault(); setSel((s) => Math.max(0, s - 1)) }
              if (e.key === 'Enter') choose(sel)
              if (e.key === 'Escape') setPalette(false)
            }}
            placeholder="Search regions, GPUs, providers, projects, energy markets, tickers, themes…"
            className="h-12 flex-1 bg-transparent text-[14px] text-bone outline-none placeholder:text-dim max-md:min-w-0 max-md:text-[16px]"
          />
          {phone && <button type="button" onClick={() => setPalette(false)} className={cx('cursor-pointer text-dim hover:text-bone', SHEET_CLOSE)} aria-label="Close search"><X size={16} /></button>}
        </div>
        <div className="max-h-[420px] overflow-y-auto p-1.5 max-md:max-h-none max-md:min-h-0 max-md:flex-1 max-md:overscroll-contain">
          {results.length === 0 && <div className="p-6 text-center text-[12px] text-mute">No matches. FLOPX tracks 17 regions, 6 GPU categories, 16 securities and 12 projects.</div>}
          {results.map((r, i) => (
            <button key={i} onMouseEnter={() => setSel(i)} onClick={() => choose(i)} className={cx('flex w-full cursor-pointer items-center gap-3 rounded-[4px] px-3 py-2 text-left max-md:min-h-11', sel === i ? 'bg-raise' : '')}>
              <span className="w-20 shrink-0 text-[11px] text-dim font-medium">{r.type === 'page' ? 'Screen' : r.e.kind}</span>
              <span className="flex-1 truncate text-[13px] text-bone">{r.type === 'page' ? r.p.label : r.e.label}</span>
              {sel === i && <CornerDownLeft size={12} className="text-dim" />}
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}

// ---------------- Assistant ----------------
import { askFlopx, SUGGESTED, type Answer, type AnswerFact } from '@/lib/ask'
export { SUGGESTED }

/** One fact row's source line: source · time, then (backend answers) its statement class and provenance badge. */
export function FactMeta({ f }: { f: AnswerFact }) {
  return (
    <span className="mt-0.5 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 font-mono text-[9.5px] text-dim">
      <span>{f.source} · {f.time}</span>
      {f.freshness && (
        <>
          {f.truth ? <TruthTag truth={f.truth} compact className="!text-[9.5px]" /> : <span title="Statement class observation, but the evidence is the backend's authored sample set: observed nowhere, so no OBS glyph">sample</span>}
          <Fresh f={f.freshness} label={f.freshLabel} />
        </>
      )}
    </span>
  )
}

function contextLabel(s: Store) {
  const parts = [NAV.flatMap((n) => n.items).find((i) => i.id === s.route.page)?.label]
  if (s.inspector) parts.push(s.inspector.label)
  else if (s.route.page === 'xray' && s.xray.region) parts.push(regionById(s.xray.region)!.name)
  else if (s.route.page === 'fund') parts.push(s.fund.signal)
  return parts.filter(Boolean).join(' › ')
}

export function Assistant() {
  const store = useStore()
  const { assistant, setAssistant } = store
  const [thread, setThread] = useState<(Answer & { pending?: boolean; id?: number })[]>([])
  const [input, setInput] = useState('')
  const endRef = useRef<HTMLDivElement>(null)
  const lastNonce = useRef(0)
  const phone = useIsPhone()
  const sheetRef = useRef<HTMLElement>(null)
  const send = (q: string) => {
    if (!q.trim()) return
    const ctx = contextLabel(store)
    const id = Date.now() + Math.random()
    const placeholder: Answer & { pending?: boolean; id?: number } = { q, context: ctx, facts: [], interpretation: '', confidence: 'Insufficient', actions: [], pending: true, id }
    setThread((t) => [...t, placeholder])
    setInput('')
    // The backend answers (POST /flopx/ask, then the named target's composed brief); the design's canned
    // answer stands in, badged illustrative, only when it does not.
    void askFlopx(q, store, ctx).then((a) => setThread((t) => t.map((x) => (x.id === id ? { ...a, pending: false, id } : x))))
  }
  useEffect(() => {
    if (assistant.open && assistant.seed && assistant.nonce !== lastNonce.current) {
      lastNonce.current = assistant.nonce
      send(assistant.seed)
    }
  }, [assistant])
  useEffect(() => endRef.current?.scrollIntoView({ behavior: 'smooth' }), [thread])
  if (!assistant.open) return null
  const closeAssistant = () => setAssistant((a) => ({ ...a, open: false }))
  return (
    <>
    {phone && <SheetBackdrop onClose={closeAssistant} />}
    <aside ref={sheetRef} className={cx('slide-in flex w-[400px] shrink-0 flex-col border-l border-line bg-panel', SHEET_FULL, phone && 'm-sheet')}>
      {phone && <SheetHandle sheet={sheetRef} onClose={closeAssistant} />}
      <header className="flex items-center justify-between border-b border-line px-4 py-3 max-md:pt-1">
        <div className="flex items-center gap-2 text-[13px] font-semibold"><Bot size={15} className="text-fct" /> Research Assistant</div>
        <button onClick={() => setAssistant((a) => ({ ...a, open: false }))} className={cx('cursor-pointer text-dim hover:text-bone', SHEET_CLOSE)} aria-label="Close assistant"><X size={15} /></button>
      </header>
      <div className="border-b border-line px-4 py-2 font-mono text-[10px] text-dim">Context · <span className="text-mute">{contextLabel(store) || '—'}</span></div>
      <div className="flex-1 space-y-5 overflow-y-auto p-4">
        {thread.length === 0 && (
          <div>
            <p className="mb-3 text-[12.5px] leading-relaxed text-mute">I answer from FLOPX’s observed data, inferred states and forecasts, and I always separate <span className="text-bone">retrieved facts</span> from my <span className="text-fct">generated interpretation</span>. I can point you to screens and proposed actions, but I don’t place trades.</p>
            <div className="space-y-1.5">
              {SUGGESTED.map((s) => (
                <button key={s} onClick={() => send(s)} className="flex w-full cursor-pointer items-center justify-between rounded-[4px] border border-line px-3 py-2 text-left text-[12px] text-bone hover:border-line2 hover:bg-hover max-md:min-h-11">{s}<ArrowRight size={12} className="text-dim" /></button>
              ))}
            </div>
          </div>
        )}
        {thread.map((a, i) => (
          <div key={i} className="fade-in">
            <div className="mb-2 ml-8 rounded-md bg-raise px-3 py-2 text-[12.5px] text-bone">{a.q}</div>
            {a.pending ? (
              <div className="flex items-center gap-2 font-mono text-[10.5px] text-dim"><span className="pulse-dot size-1.5 rounded-full bg-fct" /> Retrieving sources in context: {a.context}…</div>
            ) : (
              <div className="space-y-3">
                {a.facts.length > 0 && (
                  <div>
                    <div className="mb-1.5 flex items-center gap-1.5">
                      {a.illustrative ? <TruthTag truth="obs" /> : a.facts.some((f) => f.truth === 'obs') ? <TruthTag truth="obs" /> : <TruthTag truth="inf" />}
                      <span className="text-[11px] text-dim font-medium">{a.illustrative ? '· worked example, not FLOPX data' : '· retrieved facts, badged by evidence row'}</span>
                      {a.illustrative && <Fresh f="illustrative" />}
                    </div>
                    <ol className="space-y-1.5">
                      {a.facts.map((f, k) => (
                        <li key={k} className="grid grid-cols-[18px_1fr] gap-1 text-[12px] leading-snug">
                          <span className="font-mono text-[10px] text-dim">[{k + 1}]</span>
                          <span className="text-bone/90">{f.text}<FactMeta f={f} /></span>
                        </li>
                      ))}
                    </ol>
                  </div>
                )}
                <div className="rounded-md border border-dashed border-fct/30 bg-fct/[0.03] p-3">
                  <div className="mb-1 flex items-center justify-between gap-2 text-[11px] text-fct font-medium"><span className="flex min-w-0 items-center gap-1"><Sparkles size={10} className="shrink-0" /> <span className="truncate" title={a.engine}>Interpretation · {a.engine ? a.engine : a.offline ? 'offline worked example' : 'generated'}</span></span><span className={cx('shrink-0', a.confidence === 'Insufficient' ? 'text-warn' : 'text-mute')}>Confidence: {a.confidence}</span></div>
                  <p className="text-[12.5px] leading-relaxed text-bone/90">{a.interpretation}</p>
                </div>
                {a.actions.length > 0 && (
                  <div className="flex flex-wrap gap-1.5">
                    {a.actions.map((ac) => <Btn key={ac.label} size="sm" variant="subtle" onClick={() => ac.run(store)}>{ac.label} <ArrowRight size={11} /></Btn>)}
                  </div>
                )}
              </div>
            )}
          </div>
        ))}
        <div ref={endRef} />
      </div>
      <form onSubmit={(e) => { e.preventDefault(); send(input) }} className="border-t border-line p-3">
        <div className="flex items-center gap-2 rounded-[5px] border border-line2 bg-ink px-3 focus-within:border-fct/50">
          <input value={input} onChange={(e) => setInput(e.target.value)} placeholder="Ask about this investigation…" className="h-9 flex-1 bg-transparent text-[12.5px] outline-none placeholder:text-dim max-md:h-11 max-md:min-w-0 max-md:text-[16px]" />
          <button type="submit" className="cursor-pointer text-fct disabled:text-dim max-md:-mr-3 max-md:flex max-md:size-11 max-md:items-center max-md:justify-center" disabled={!input.trim()} aria-label="Send"><Send size={14} /></button>
        </div>
      </form>
    </aside>
    </>
  )
}

// ---------------- Demo walkthroughs ----------------
// The three pitches (G-Research, HRT, Metaview) are the presenter tour in src/tour: a dimmed page,
// a spotlight on the real element each beat talks about, an animated card beside it, a progress
// rail, keyboard, autoplay and a Present mode. Tour state lives in the store (`tour`, `setTour`).
export function DemoGuide() {
  return <TourStage />
}

// ---------------- Paper order ticket ----------------
export function Ticket() {
  const { ticket, setTicket, submitOrder, nav, cash, positions, marketOpen } = useStore()
  const [ticker, setTicker] = useState('VST')
  const [side, setSide] = useState<Order['side']>('Buy')
  const [qty, setQty] = useState(100)
  const [type, setType] = useState<'Market' | 'Limit'>('Market')
  const [limit, setLimit] = useState(0)
  const phone = useIsPhone()
  const sheetRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (ticket) {
      setTicker(ticket.ticker)
      const pos = positions.find((p) => p.ticker === ticket.ticker)
      setSide(ticket.side ?? (pos && pos.qty < 0 ? 'Cover' : 'Buy'))
      setQty(ticket.qty ?? 100)
      setType('Market')
      setLimit(+lastPx(ticket.ticker).toFixed(2))
    }
  }, [ticket])
  if (!ticket) return null
  const px = lastPx(ticker)
  const notional = qty * (type === 'Limit' ? limit : px)
  const pos = positions.find((p) => p.ticker === ticker)
  const checks: { ok: boolean | 'warn'; text: ReactNode }[] = [
    { ok: notional <= nav * 0.15, text: <>Single-order limit · {((notional / nav) * 100).toFixed(1)}% of NAV (max 15%)</> },
    { ok: !(side === 'Short' && ['IREN', 'APLD'].includes(ticker)), text: side === 'Short' ? 'Borrow locate (simulated)' : 'No borrow needed' },
    { ok: side === 'Sell' || side === 'Cover' ? !!pos : notional <= cash + nav * 0.5, text: side === 'Sell' || side === 'Cover' ? (pos ? `Position ${pos.qty.toLocaleString()} sh` : 'No position to reduce') : 'Buying power' },
    { ok: marketOpen ? true : 'warn', text: marketOpen ? 'Market open — fills at last + 3 bps slippage' : 'Market closed — order will queue for next open' },
  ]
  const tickers = ['NVDA', 'AMD', 'TSM', 'MSFT', 'AMZN', 'GOOGL', 'ORCL', 'CRWV', 'NBIS', 'IREN', 'APLD', 'EQIX', 'DLR', 'VST', 'CEG', 'NRG']
  return (
    <div className="fixed inset-0 z-[85] flex items-center justify-center bg-black/60 backdrop-blur-[2px] max-md:z-[95] max-md:items-end" onClick={() => setTicket(null)}>
      <div ref={sheetRef} className={cx('fade-in w-[440px] rounded-lg border border-dec/30 bg-panel shadow-2xl', SHEET_FULL, 'max-md:overflow-y-auto max-md:overscroll-contain', phone && 'm-sheet')} onClick={(e) => e.stopPropagation()}>
        {phone && <SheetHandle sheet={sheetRef} onClose={() => setTicket(null)} />}
        <header className="flex items-center justify-between border-b border-line px-5 py-3">
          <div>
            <div className="flex items-center gap-2"><TruthGlyph truth="dec" /><span className="text-[14px] font-semibold">Paper order ticket</span></div>
            <div className="mt-0.5 flex gap-1.5"><Fresh f="simulated" label="Simulated execution" /><Fresh f="illustrative" /></div>
          </div>
          <button onClick={() => setTicket(null)} className={cx('cursor-pointer text-dim hover:text-bone', SHEET_CLOSE)} aria-label="Close ticket"><X size={15} /></button>
        </header>
        <div className="space-y-4 px-5 py-4">
          <div className="grid grid-cols-2 gap-3">
            <label className="block"><span className="eyebrow">Instrument</span>
              <select value={ticker} onChange={(e) => { setTicker(e.target.value); setLimit(+lastPx(e.target.value).toFixed(2)) }} className="mt-1 h-9 w-full rounded-[4px] border border-line2 bg-ink px-2 font-mono text-[13px] outline-none max-md:h-11 max-md:text-[16px]">
                {tickers.map((t) => <option key={t} value={t}>{t} · {secByTicker(t)!.name}</option>)}
              </select>
            </label>
            <div><span className="eyebrow">Last (delayed)</span><div className="num mt-1 flex h-9 items-center text-[18px]">${px.toFixed(2)}</div></div>
          </div>
          <div>
            <span className="eyebrow">Side</span>
            <Seg className="mt-1 w-full" value={side} onChange={setSide} options={(['Buy', 'Sell', 'Short', 'Cover'] as const).map((s) => ({ id: s, label: s }))} />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <label className="block"><span className="eyebrow">Quantity (sh)</span><input type="number" min={1} value={qty} onChange={(e) => setQty(Math.max(1, +e.target.value))} className="num mt-1 h-9 w-full rounded-[4px] border border-line2 bg-ink px-2 text-[13px] outline-none focus:border-dec/50 max-md:h-11 max-md:text-[16px]" /></label>
            <div>
              <span className="eyebrow">Type</span>
              <Seg className="mt-1" value={type} onChange={setType} options={[{ id: 'Market', label: 'Market' }, { id: 'Limit', label: 'Limit' }]} />
            </div>
          </div>
          {type === 'Limit' && <label className="block"><span className="eyebrow">Limit price</span><input type="number" step="0.01" value={limit} onChange={(e) => setLimit(+e.target.value)} className="num mt-1 h-9 w-full rounded-[4px] border border-line2 bg-ink px-2 text-[13px] outline-none max-md:h-11 max-md:text-[16px]" /></label>}
          {pos && (
            <div className="flex gap-1.5">
              {[0.25, 0.5, 1].map((f) => <Btn key={f} size="sm" variant="subtle" onClick={() => { setSide(pos.qty > 0 ? 'Sell' : 'Cover'); setQty(Math.max(1, Math.round(Math.abs(pos.qty) * f))) }}>{f === 1 ? 'Close all' : `Reduce ${f * 100}%`}</Btn>)}
            </div>
          )}
          <div className="rounded-md border border-line bg-ink p-3">
            <div className="mb-2 flex justify-between font-mono text-[11px]"><span className="text-dim">Est. notional</span><span>${Math.round(notional).toLocaleString()}</span></div>
            <div className="space-y-1">
              {checks.map((c, i) => <div key={i} className={cx('flex items-center gap-2 font-mono text-[10.5px]', c.ok === true ? 'text-mute' : c.ok === 'warn' ? 'text-warn' : 'text-down')}><span>{c.ok === true ? '✓' : c.ok === 'warn' ? '!' : '✕'}</span>{c.text}</div>)}
            </div>
          </div>
        </div>
        <footer className="flex items-center justify-between border-t border-line px-5 py-3">
          <span className="font-mono text-[10px] text-dim">Origin: User · not a model recommendation</span>
          <Btn variant="decision" onClick={() => { submitOrder({ ticker, side, qty, type, limit: type === 'Limit' ? limit : undefined, origin: 'User' }); setTicket(null) }}>Submit paper order</Btn>
        </footer>
      </div>
    </div>
  )
}

export type { EntityRef }
