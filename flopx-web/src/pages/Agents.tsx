import { useEffect, useRef, useState } from 'react'
import { ArrowUp, Check, ChevronRight, CircleDot, Diamond, Info, Lock, Send, Sparkles, Wrench, X } from 'lucide-react'
import { FactMeta } from '@/components/Overlays'
import { Btn, cx, Fresh, PageHeader, Panel, TruthTag } from '@/components/ui'
import { Row } from '@/components/collapse'
import { AGENT_TOOLS } from '@/data/handoff'
import { AGENT_SUGGESTED, askFlopx, type Answer } from '@/lib/ask'
import { useStore } from '@/lib/store'

type Verdict = 'pending' | 'sent' | 'rejected'
interface Card { id: number; q: string; a: Answer | null; verdicts: Verdict[] }
interface Decision { id: number; at: string; verdict: 'sent' | 'rejected'; label: string; tool?: string }

const stamp = () => new Date().toISOString().slice(11, 16) + ' UTC'
const LIVE_TITLE = 'POST /flopx/ask: a deterministic grammar picks the intent and screen; a composed brief (composed-from-evidence-v1, not a language model) supplies the facts for a named region, company or GPU.'

export default function Agents() {
  const store = useStore()
  const [q, setQ] = useState('')
  const linked = store.route.params.q
  // The thread opens empty: no prepared answer is shown as if it had been asked.
  const [cards, setCards] = useState<Card[]>([])
  const [log, setLog] = useState<Decision[]>([])
  const latest = [...cards].reverse().find((c) => c.a)?.a
  const lastTool = latest?.tool
  const threadRef = useRef<HTMLDivElement>(null)
  useEffect(() => { const t = threadRef.current; if (t && cards.length > 1) t.scrollTo({ top: t.scrollHeight, behavior: 'smooth' }) }, [cards.length])
  const askedRef = useRef<string | null>(null)

  // The backend answers: POST /flopx/ask for the intent and routes, then the named target's composed
  // brief. The design's prepared cards stand in, badged illustrative, only when it does not answer.
  const ask = (text: string) => {
    const t = text.trim()
    if (!t) return
    const id = Date.now() + Math.random()
    setCards((c) => [...c, { id, q: t, a: null, verdicts: [] }])
    setQ('')
    void askFlopx(t, store).then((a) => setCards((cs) => cs.map((c) => (c.id === id ? { ...c, a, verdicts: a.actions.map(() => 'pending' as Verdict) } : c))))
  }

  // Deep link (#/agents?q=...), used by the Metaview walkthrough: ask once per distinct question.
  useEffect(() => {
    if (linked && askedRef.current !== linked) { askedRef.current = linked; ask(linked) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [linked])

  const decide = (card: Card, a: Answer, i: number, verdict: 'sent' | 'rejected') => {
    const act = a.actions[i]
    if (verdict === 'sent') act.run(store)
    setCards((cs) => cs.map((c) => (c.id === card.id ? { ...c, verdicts: c.verdicts.map((v, k) => (k === i ? verdict : v)) } : c)))
    setLog((l) => [{ id: Date.now(), at: stamp(), verdict, label: act.label, tool: a.tool }, ...l])
  }

  const offline = latest?.offline
  const offlineTitle = `The FLOPX backend did not answer (${offline}); the design's prepared cards stand in and are badged illustrative.`
  return (
    <div className="fade-in mx-auto flex h-full max-w-[1680px] flex-col px-6 pt-6">
      <PageHeader
        title="Ask FLOPX"
        sub="An agent answers from engine tools. A person decides."
        right={offline
          ? <span title={offlineTitle}><Fresh f="illustrative" label="Offline · prepared cards" /></span>
          : <span title={LIVE_TITLE}><Fresh f="live" label="FLOPX backend · grammar + composed brief" /></span>}
      />

      <Row cols={[1, '300px']} className="min-h-0 flex-1 xl:grid-rows-[minmax(0,1fr)]">
        <div className="mx-auto flex h-full min-h-0 w-full max-w-[760px] flex-col">
          {cards.length === 0 && (
            <div className="py-16 text-center">
              <div className="text-[20px] font-semibold text-bone">What do you want to know?</div>
              <div className="mt-1 text-[12.5px] text-dim">Ask in plain words; one engine tool answers, with its source.</div>
            </div>
          )}

          <div ref={threadRef} className="mt-1 min-h-0 flex-1 space-y-8 overflow-y-auto pb-4 [scrollbar-width:thin]">
            {cards.map((card) => {
              const { a } = card
              if (!a) {
                return (
                  <article key={card.id} id={`Answer ${card.id}`}>
                    <div className="flex justify-end">
                      <div className="max-w-[80%] rounded-2xl border border-line bg-raise px-4 py-2.5 text-[14.5px] leading-snug text-bone">{card.q}</div>
                    </div>
                    <div className="mt-3 flex items-center gap-2 text-[13.5px] text-dim"><span className="pulse-dot size-1.5 rounded-full bg-fct" /> Asking the FLOPX backend: the intent, then the composed brief for the named target…</div>
                  </article>
                )
              }
              const tool = AGENT_TOOLS.find((t) => t.id === a.tool)
              const sources = [...new Set(a.facts.map((f) => `${f.source} · ${f.time}`))]
              return (
                <article key={card.id} id={`Answer ${card.id}`}>
                  <div className="flex justify-end">
                    <div className="max-w-[80%] rounded-2xl border border-line bg-raise px-4 py-2.5 text-[14.5px] leading-snug text-bone">{card.q}</div>
                  </div>

                  <details className="group mt-3">
                    <summary className="inline-flex cursor-pointer list-none items-center gap-1.5 text-[13.5px] text-dim transition-colors hover:text-mute [&::-webkit-details-marker]:hidden">
                      {tool ? <>Used {tool.label}</> : a.engine ? <>Engine <span className="font-mono">{a.engine}</span></> : a.offline ? <span className="text-warn">Offline · prepared card</span> : <span className="text-warn">No tool matched</span>}
                      {a.facts.length > 0 && <span>· {a.facts.length} {a.facts.length === 1 ? 'fact' : 'facts'}</span>}
                      <ChevronRight size={14} className="transition-transform group-open:rotate-90" />
                    </summary>
                    <div className="mt-2 space-y-1.5 border-l border-line pl-4 text-[12.5px] text-dim">
                      {tool && <div className="flex items-start gap-2"><Wrench size={13} className="mt-0.5 shrink-0" /><span><code className="rounded-[4px] bg-raise px-1.5 py-px font-mono text-[12px] text-mute">{tool.id}</code> {tool.desc}</span></div>}
                      {sources.map((src, k) => <div key={k} className="pl-[21px]">{src}</div>)}
                    </div>
                  </details>
                  <div className="mt-2 border-t border-line" />

                  {a.facts.length > 0 && (
                    <div className="mt-3">
                      <div className="mb-1.5 flex items-center gap-1.5">{a.illustrative ? <Fresh f="illustrative" /> : a.facts.some((f) => f.truth === 'obs') ? <TruthTag truth="obs" /> : <TruthTag truth="inf" />}<span className="text-[11px] text-dim">{a.illustrative ? 'worked example' : 'retrieved facts, badged by evidence row'}</span></div>
                      <div className="space-y-1.5">
                        {a.facts.map((f, k) => (
                          <p key={k} className="text-[15px] leading-relaxed text-bone" title={`${f.source} · ${f.time}`}>
                            {f.text} <sup className="num text-[10px] text-dim">[{k + 1}]</sup>
                            <FactMeta f={f} />
                          </p>
                        ))}
                      </div>
                    </div>
                  )}

                  <div className="mt-4">
                    <div className="mb-1.5 flex items-center gap-1.5 text-[11px] text-mute"><Sparkles size={11} /> Composed interpretation · not a retrieved fact
                      <span className="text-dim">· Confidence <span className={cx(a.confidence === 'Insufficient' ? 'text-warn' : 'text-mute')}>{a.confidence}</span></span>
                    </div>
                    <p className="text-[15px] leading-relaxed text-bone/90">{a.interpretation}</p>
                  </div>

                  {a.actions.map((act, i) => {
                    const v = card.verdicts[i]
                    return (
                      <div key={i} className="mt-4 flex flex-wrap items-center gap-3 text-[14px]">
                        <Diamond size={14} className="shrink-0 fill-dec text-dec" />
                        <span className="text-mute">Proposed action <span className="text-bone">{act.label}</span></span>
                        {v === 'pending' ? (
                          <span className="ml-auto flex gap-1.5">
                            <Btn size="sm" variant="primary" onClick={() => decide(card, a, i, 'sent')}><Send size={11} /> Send</Btn>
                            <Btn size="sm" variant="danger" onClick={() => decide(card, a, i, 'rejected')}><X size={11} /> Reject</Btn>
                          </span>
                        ) : (
                          <span className={cx('ml-auto text-[12px] font-medium', v === 'sent' ? 'text-up' : 'text-down')}>{v === 'sent' ? 'Sent' : 'Rejected'}</span>
                        )}
                      </div>
                    )
                  })}
                </article>
              )
            })}
          </div>

          <div className="shrink-0 pb-4 pt-2">
            {!q && (
              <div className="mb-2 flex gap-1.5 overflow-x-auto [scrollbar-width:none]">
                {AGENT_SUGGESTED.slice(0, 5).map((s) => (
                  <button key={s} type="button" onClick={() => ask(s)}
                    className="shrink-0 cursor-pointer rounded-full border border-line px-3 py-1 text-[12px] whitespace-nowrap text-dim transition-colors hover:border-line2 hover:text-bone">{s}</button>
                ))}
              </div>
            )}
            <div className="mx-5 flex items-center gap-2.5 rounded-t-xl border border-b-0 border-line bg-panel px-4 py-2 text-[12.5px]">
              <CircleDot size={14} className="shrink-0 text-mute" />
              {offline ? (
                <>
                  <span className="shrink-0 font-medium text-bone">Offline · prepared cards</span>
                  <span className="min-w-0 truncate text-dim">The backend did not answer; prepared cards stand in, badged illustrative.</span>
                  <span className="ml-auto shrink-0" title={offlineTitle}><Info size={14} className="text-dim" /></span>
                </>
              ) : (
                <>
                  <span className="shrink-0 font-medium text-bone">FLOPX backend</span>
                  <span className="min-w-0 truncate text-dim">A deterministic grammar picks one engine tool; a composed brief supplies the facts.</span>
                  <span className="ml-auto shrink-0" title={LIVE_TITLE}><Info size={14} className="text-dim" /></span>
                </>
              )}
            </div>
            <form onSubmit={(e) => { e.preventDefault(); ask(q) }}
              className="rounded-2xl border border-line bg-panel px-5 pb-2.5 pt-3.5 transition-colors focus-within:border-line2">
              <textarea value={q} onChange={(e) => setQ(e.target.value)} rows={1} aria-label="Question"
                onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(q) } }}
                placeholder="Ask about token prices, procurement, scarcity, power…"
                className="block w-full resize-none bg-transparent text-[15px] leading-6 text-bone placeholder:text-dim focus:outline-none" />
              <div className="mt-3 flex items-center gap-5 text-[13px] text-mute">
                <span className="flex items-center gap-1.5"><Wrench size={14} className="text-dim" />Auto tool</span>
                <span className="flex items-center gap-1.5"><Lock size={14} className="text-dim" />Actions on Send only</span>
                <button type="submit" disabled={!q.trim()} aria-label="Ask"
                  className="ml-auto grid size-9 cursor-pointer place-items-center rounded-full bg-bone text-ink transition-colors disabled:cursor-default disabled:bg-raise disabled:text-dim">
                  <ArrowUp size={17} strokeWidth={2.25} />
                </button>
              </div>
            </form>
          </div>
        </div>

        <div className="hidden min-h-0 space-y-5 overflow-y-auto xl:block">
          <Panel variant="rail" title="Decisions">
            {log.length === 0 ? (
              <p className="py-2 text-[12px] text-dim">Nothing sent or rejected yet.</p>
            ) : (
              log.map((d) => (
                  <div key={d.id} className="grid grid-cols-[auto_1fr] gap-x-2 py-1.5 text-[12px]">
                    <span className={cx('flex items-center gap-1 font-medium', d.verdict === 'sent' ? 'text-up' : 'text-down')}>{d.verdict === 'sent' ? <Check size={11} /> : <X size={11} />}{d.verdict === 'sent' ? 'Sent' : 'Rejected'}</span>
                    <span className="text-bone/90">{d.label}<span className="block text-[11px] text-dim"><span className="num">{d.at}</span>{d.tool ? <> · <span className="num">{d.tool}</span></> : ''}</span></span>
                  </div>
              ))
            )}
          </Panel>
          <details className="group rounded-[2px] border border-line bg-panel">
            <summary className="flex cursor-pointer list-none items-center gap-1.5 px-4 py-2.5 text-[13px] font-semibold text-bone [&::-webkit-details-marker]:hidden">
              <ChevronRight size={14} className="text-dim transition-transform group-open:rotate-90" />Available tools<span className="num ml-auto text-[11px] font-normal text-dim">{AGENT_TOOLS.length}</span>
            </summary>
            <ul className="border-t border-line-soft">
              {AGENT_TOOLS.map((t) => {
                const on = t.id === lastTool
                return (
                  <li key={t.id} className={cx('border-b border-line-soft px-4 py-2 last:border-b-0', on && 'bg-raise shadow-[inset_2px_0_0_var(--color-bone)]')}>
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-[12.5px] text-bone">{t.label}</span>
                      {on && <span className="text-[10.5px] text-mute">Used for latest answer</span>}
                    </div>
                    <div className="text-[11.5px] text-dim">{t.desc}</div>
                  </li>
                )
              })}
            </ul>
          </details>
        </div>
      </Row>
    </div>
  )
}
