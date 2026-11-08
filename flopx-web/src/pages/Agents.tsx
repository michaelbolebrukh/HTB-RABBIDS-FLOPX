import { useEffect, useRef, useState } from 'react'
import { ArrowRight, Bot, Check, CornerDownLeft, Send, X } from 'lucide-react'
import { FactMeta } from '@/components/Overlays'
import { AIBlock, Btn, cx, Fresh, PageHeader, Panel, TruthTag } from '@/components/ui'
import { AGENT_TOOLS } from '@/data/handoff'
import { AGENT_SUGGESTED, askFlopx, type Answer } from '@/lib/ask'
import { useStore } from '@/lib/store'

type Verdict = 'pending' | 'sent' | 'rejected'
interface Card { id: number; q: string; a: Answer | null; verdicts: Verdict[] }
interface Decision { id: number; at: string; verdict: 'sent' | 'rejected'; label: string; tool?: string }

const stamp = () => new Date().toISOString().slice(11, 16) + ' UTC'

export default function Agents() {
  const store = useStore()
  const [q, setQ] = useState('')
  const [cards, setCards] = useState<Card[]>([])
  const [log, setLog] = useState<Decision[]>([])
  const latest = cards.find((c) => c.a)?.a
  const lastTool = latest?.tool
  const askedRef = useRef<string | null>(null)

  // The backend answers: POST /flopx/ask for the intent and routes, then the named target's composed
  // brief. The design's prepared cards stand in, badged illustrative, only when it does not answer.
  const ask = (text: string) => {
    const t = text.trim()
    if (!t) return
    const id = Date.now() + Math.random()
    setCards((c) => [{ id, q: t, a: null, verdicts: [] }, ...c])
    setQ('')
    void askFlopx(t, store).then((a) => setCards((cs) => cs.map((c) => (c.id === id ? { ...c, a, verdicts: a.actions.map(() => 'pending' as Verdict) } : c))))
  }

  // Deep link (#/agents?q=...), used by the Metaview walkthrough: ask once per distinct question.
  const linked = store.route.params.q
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
  return (
    <div>
      <PageHeader
        eyebrow={<><Bot size={12} /> Agents · Metaview</>}
        title="An agent answers from engine tools. A person decides."
        sub="Ask in plain words; the agent calls one engine tool and shows its source."
        right={offline
          ? <span title={`The FLOPX backend did not answer (${offline}); the design's prepared cards stand in and are badged illustrative.`}><Fresh f="illustrative" label="Offline · prepared cards" /></span>
          : <span title="POST /flopx/ask: a deterministic grammar picks the intent and screen; a composed brief (composed-from-evidence-v1, not a language model) supplies the facts for a named region, company or GPU."><Fresh f="live" label="FLOPX backend · grammar + composed brief" /></span>}
      />
      <div className="mb-4 flex flex-wrap gap-x-6 gap-y-1 text-[11.5px] text-mute">
        <span className="flex items-center gap-1.5"><TruthTag truth="obs" compact /> Facts: backend evidence, badged per row</span>
        <span className="flex items-center gap-1.5"><TruthTag truth="fct" compact /> Reading: composed</span>
        <span className="flex items-center gap-1.5"><TruthTag truth="dec" compact /> Actions: on Send only</span>
      </div>

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-[320px_1fr]">
        <div className="space-y-5 self-start">
          <Panel title="Engine tools" right={<span className="num text-[10.5px] text-dim">{AGENT_TOOLS.length}</span>} bodyClass="!p-0">
            <ul>
              {AGENT_TOOLS.map((t) => {
                const on = t.id === lastTool
                return (
                  <li key={t.id} className={cx('border-b border-line px-3 py-2 last:border-b-0', on && 'bg-[color-mix(in_oklab,var(--accent)_10%,transparent)]')}>
                    <div className="flex items-center justify-between gap-2">
                      <span className={cx('text-[12.5px]', on ? 'text-[var(--accent)]' : 'text-bone')}>{t.label}</span>
                      {on && <span className="text-[10px] text-[var(--accent)]">Used for latest answer</span>}
                    </div>
                    <div className="text-[11.5px] text-dim">{t.desc}</div>
                  </li>
                )
              })}
            </ul>
          </Panel>
          <Panel title="Decisions" right={<span className="num text-[10.5px] text-dim">{log.length}</span>}>
            {log.length === 0 ? (
              <p className="text-[12px] text-dim">Nothing sent or rejected yet.</p>
            ) : (
              <ul className="space-y-1.5">
                {log.map((d) => (
                  <li key={d.id} className="grid grid-cols-[auto_1fr] gap-x-2 text-[12px]">
                    <span className={cx('flex items-center gap-1 font-medium', d.verdict === 'sent' ? 'text-up' : 'text-down')}>{d.verdict === 'sent' ? <Check size={11} /> : <X size={11} />}{d.verdict === 'sent' ? 'Sent' : 'Rejected'}</span>
                    <span className="text-bone/90">{d.label}<span className="block font-mono text-[9.5px] text-dim">{d.at}{d.tool ? ` · ${d.tool}` : ''}</span></span>
                  </li>
                ))}
              </ul>
            )}
          </Panel>
        </div>

        <div className="space-y-4">
          <Panel title="Ask the agent">
            <form onSubmit={(e) => { e.preventDefault(); ask(q) }} className="flex items-center gap-2">
              <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask about a region, a GPU, a company, procurement, news…" aria-label="Question"
                className="h-9 min-w-0 flex-1 rounded-[4px] border border-line2 bg-transparent px-3 text-[13px] text-bone placeholder:text-dim focus:border-mute focus:outline-none" />
              <Btn type="submit" variant="primary" disabled={!q.trim()}><Send size={12} /> Ask</Btn>
            </form>
            <div className="mt-3 flex flex-wrap gap-1.5">
              {AGENT_SUGGESTED.map((s) => (
                <button key={s} onClick={() => ask(s)} className="inline-flex cursor-pointer items-center gap-1.5 rounded-[4px] border border-line px-2.5 py-1.5 text-left text-[11.5px] text-bone hover:border-line2 hover:bg-hover">{s}<ArrowRight size={11} className="shrink-0 text-dim" /></button>
              ))}
            </div>
          </Panel>

          {cards.length === 0 && <p className="px-1 text-[12px] text-dim"><CornerDownLeft size={11} className="mr-1 inline" />Pick a question or type your own. Answers appear here, newest first.</p>}

          {cards.map((card) => {
            const { a } = card
            if (!a) {
              return (
                <Panel key={card.id} title={card.q} right={<span className="text-[11px] text-mute">Asking the FLOPX backend</span>} collapsible={false}>
                  <div className="flex items-center gap-2 font-mono text-[10.5px] text-dim"><span className="pulse-dot size-1.5 rounded-full bg-fct" /> Retrieving the intent, then the composed brief for the named target…</div>
                </Panel>
              )
            }
            const tool = AGENT_TOOLS.find((t) => t.id === a.tool)
            const head = tool
              ? <span className="text-[11px] text-mute">Tool: {tool.label} <span className="font-mono text-dim">{tool.id}</span></span>
              : a.engine
                ? <span className="text-[11px] text-mute">Engine: <span className="font-mono text-dim">{a.engine}</span></span>
                : a.offline
                  ? <span className="text-[11px] text-warn">Offline · prepared card</span>
                  : <span className="text-[11px] text-warn">No tool matched</span>
            return (
              <Panel key={card.id} title={card.q} right={head} collapsible={false}>
                <div className="space-y-3">
                  {a.facts.length > 0 && (
                    <div>
                      <div className="mb-1.5 flex items-center gap-1.5">{a.illustrative ? <Fresh f="illustrative" /> : a.facts.some((f) => f.truth === 'obs') ? <TruthTag truth="obs" /> : <TruthTag truth="inf" />}<span className="text-[11px] font-medium text-dim">· {a.illustrative ? 'worked example' : 'retrieved facts, badged by evidence row'}</span></div>
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
                  <AIBlock>
                    <div className="mb-1 text-[11px] text-mute">Confidence: <span className={cx(a.confidence === 'Insufficient' ? 'text-warn' : 'text-bone')}>{a.confidence}</span></div>
                    {a.interpretation}
                  </AIBlock>
                  {a.actions.length > 0 && (
                    <div>
                      <div className="mb-1.5 flex items-center gap-1.5"><TruthTag truth="dec" /><span className="text-[11px] font-medium text-dim">· proposed action, waiting for you</span></div>
                      <ul className="space-y-1.5">
                        {a.actions.map((act, i) => {
                          const v = card.verdicts[i]
                          return (
                            <li key={i} className="flex flex-wrap items-center justify-between gap-2 rounded-[4px] border border-line px-3 py-2">
                              <span className="text-[12.5px] text-bone">{act.label}</span>
                              {v === 'pending' ? (
                                <span className="flex gap-1.5">
                                  <Btn size="sm" variant="primary" onClick={() => decide(card, a, i, 'sent')}><Send size={11} /> Send</Btn>
                                  <Btn size="sm" variant="danger" onClick={() => decide(card, a, i, 'rejected')}><X size={11} /> Reject</Btn>
                                </span>
                              ) : (
                                <span className={cx('text-[11.5px] font-medium', v === 'sent' ? 'text-up' : 'text-down')}>{v === 'sent' ? 'Sent' : 'Rejected'}</span>
                              )}
                            </li>
                          )
                        })}
                      </ul>
                    </div>
                  )}
                </div>
              </Panel>
            )
          })}
        </div>
      </div>
    </div>
  )
}
