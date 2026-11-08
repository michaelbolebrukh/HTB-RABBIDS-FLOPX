"""
Long-conversation flush e2e (D60) — LIVE proof that the summary-time flush
captures facts the agent never explicitly saved, end to end.

The trick that makes this affordable: the chat model's registry entry is patched
to a ~2.5k-token context window, so a handful of verbose turns triggers GENUINE
compaction (real summarizer, real extractor, real reconciliation) without
burning 128k-context volumes. Everything else is the production path: real
gpt-4o-mini agent, real memory tools, real flush task. Cost: ~$0.03.

Flow: one long conversation drops three durable facts as terse asides buried in
filler chatter (never "please remember") → compaction fires mid-conversation →
flush extracts + reconciles → a NEW conversation asks for the facts → judge.

Run:  .venv/bin/python platform/services/generative_ai_service/benchmarks/memory/flush_e2e.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("HASH_KEY", "bench")
os.environ.setdefault("APP_DOMAIN", "test.local")
os.environ.setdefault("SENTRY_DSN", "")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from infrastructure.database import Base
from openai import AsyncOpenAI

from platform.services.generative_ai_service.engine import AgentFeatures, AgentSpec, RunDeps, UserInput
from platform.services.generative_ai_service.engine import engine as engine_mod
from platform.services.generative_ai_service.memory import store
from platform.services.generative_ai_service.models import (
    AIBudget, AIConversation, AIMemory, AISpendEvent, AITranscriptItem, AITranscriptSummary,
)
from platform.services.generative_ai_service.transcript import create_conversation

TABLES = [AIConversation.__table__, AITranscriptItem.__table__, AITranscriptSummary.__table__,
          AISpendEvent.__table__, AIBudget.__table__, AIMemory.__table__]
SPEC = AgentSpec(name="assistant", model="openai:gpt-4o-mini",
                 features=AgentFeatures(memory=True, rag_search=False))
SCOPE = "assistant"
SMALL_WINDOW = 2500

FILLER = ("Anyway, I was also thinking about the weather this week, it has been "
          "surprisingly changeable, and I still need to sort out my errands list, "
          "reorganize the garage shelving, and plan the weekend groceries properly. ") * 3

# Three durable facts as terse asides (deliberately NOT "please remember"):
TURNS = [
    f"Busy week! By the way, my daughter Mia is allergic to penicillin. {FILLER}",
    f"Thanks. Oh and for context, I work at a fintech called Nuvei. {FILLER}",
    f"Good points. Random aside — my cat is named Sushi. {FILLER}",
    f"Makes sense. What's a simple dinner idea for tonight? {FILLER}",
    f"Nice. And a good podcast for commutes? {FILLER}",
    f"Cool. Any tips for better sleep? {FILLER}",
]
QUESTIONS = [
    ("What is my daughter allergic to?", "states penicillin (daughter Mia's allergy)"),
    ("Where do I work?", "states the fintech Nuvei"),
    ("What is my cat's name?", "states Sushi"),
]

JUDGE = AsyncOpenAI()


async def _judge(q, a, criterion):
    r = await JUDGE.chat.completions.create(
        model="gpt-4o-mini", temperature=0, max_tokens=3,
        messages=[{"role": "system", "content": "Grade the answer against the criterion. Reply PASS or FAIL."},
                  {"role": "user", "content": f"Q: {q}\nA: {a}\nCriterion: {criterion}\nVerdict:"}])
    return "PASS" in r.choices[0].message.content.upper()


async def main():
    # Shrinking ONLY the chat model's window so compaction genuinely fires
    # (summarizer/resolver/extractor models keep their real entries):
    real_get = engine_mod.get_model_info
    def small_window(model_id):
        info = real_get(model_id)
        if "gpt-4o-mini" in model_id:
            return info.model_copy(update={"context_window": SMALL_WINDOW})
        return info
    engine_mod.get_model_info = small_window

    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng, tables=TABLES)
    factory = sessionmaker(bind=eng)
    db = factory()

    conv = create_conversation(db, owner_id="u")
    deps = RunDeps(db=db, owner_id="u", conversation_id=conv.id,
                   memory_scope=SCOPE, session_factory=factory)
    for i, turn in enumerate(TURNS):
        r = await engine_mod.run_agent(SPEC, deps, UserInput(text=turn))
        print(f"turn {i+1}/{len(TURNS)} ok ({str(r.output)[:50]!r}...)")

    # Waiting for detached flush tasks, then reporting what memory holds:
    pending = list(engine_mod._FLUSH_TASKS)
    print(f"\nflush tasks fired: {len(pending)}")
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    live = store.live_memories(db, "u", SCOPE)
    print(f"live memories ({len(live)}):")
    for m in live:
        print(f"  - {m.text[:80]}  (sources={len(m.source_turn_ids or [])})")

    # The real test: a NEW conversation must answer from durable memory:
    print("\nrecall from a fresh conversation:")
    ok_count = 0
    conv2 = create_conversation(db, owner_id="u")
    deps2 = RunDeps(db=db, owner_id="u", conversation_id=conv2.id,
                    memory_scope=SCOPE, session_factory=factory)
    for q, criterion in QUESTIONS:
        r = await engine_mod.run_agent(SPEC, deps2, UserInput(text=q))
        ok = await _judge(q, str(r.output), criterion)
        ok_count += ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {q!r} -> {str(r.output)[:90]!r}")

    print(f"\nRESULT: {ok_count}/{len(QUESTIONS)} facts survived compaction "
          f"(flush fired: {'yes' if pending else 'NO — window not exceeded?'})")
    db.close()


if __name__ == "__main__":
    asyncio.run(main())
