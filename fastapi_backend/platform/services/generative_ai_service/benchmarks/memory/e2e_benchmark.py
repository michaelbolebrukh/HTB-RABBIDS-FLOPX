"""
End-to-end memory benchmark — drives a REAL agent through the full engine
(features.memory=True) over the scenario catalog (`scenarios.py`), scoring what
a user experiences. LIVE (costs money); hard-capped by `--ceiling`.

The runner is generic: it interprets `Check` rules and never hard-codes a
category, so `scenarios.CATALOG` is the only place coverage is defined.

Run (from repo root `fastapi_backend/`), as a plain script (NOT `-m`, so the env
defaults below are set before the service package imports Settings):
    .venv/bin/python platform/services/generative_ai_service/benchmarks/memory/e2e_benchmark.py \
        --scale 1 --concurrency 6 --ceiling 1.0
    # --scale N multiplies scenarios per category; --list prints the coverage map.

Needs OPENAI_API_KEY. Cost ≈ $0.0016/scenario (gpt-4o-mini). See README.md.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import traceback
from pathlib import Path

# Env + import path MUST be set before importing the service package (its
# __init__ instantiates Settings). Backend root = parents[5] of this file:
_BACKEND_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(_BACKEND_ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("HASH_KEY", "bench")
os.environ.setdefault("APP_DOMAIN", "test.local")
os.environ.setdefault("SENTRY_DSN", "")

from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from infrastructure.database import Base
from openai import AsyncOpenAI

from platform.services.generative_ai_service.benchmarks.memory import scenarios as S
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
                 features=AgentFeatures(memory=True, summarization=False, rag_search=False))
MEMORY_SCOPE = "assistant"  # the app-supplied partition (passed per run via RunDeps)
JUDGE = AsyncOpenAI()


class Budget:
    """Metering spend from each scenario's own ledger + a hard ceiling."""
    def __init__(self, ceiling_usd):
        self.spent_micro = 0
        self.judge_in = self.judge_out = 0
        self.ceiling_micro = int(ceiling_usd * 1e6)
        self.lock = asyncio.Lock()

    def usd(self):
        return (self.spent_micro + self.judge_in / 1e6 * 0.15 * 1e6 + self.judge_out / 1e6 * 0.60 * 1e6) / 1e6

    async def add(self, micro):
        async with self.lock:
            self.spent_micro += micro

    def over(self):
        return self.usd() > self.ceiling_micro / 1e6


BUDGET = Budget(5.0)


def _fresh_db():
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng, tables=TABLES)
    return eng, sessionmaker(bind=eng)


async def _new_conv(db, owner):
    return (await asyncio.to_thread(create_conversation, db, owner_id=owner)).id


async def _converse(factory, db, owner, conv_id, text):
    deps = RunDeps(db=db, owner_id=owner, conversation_id=conv_id,
                   memory_scope=MEMORY_SCOPE, session_factory=factory)
    r = await engine_mod.run_agent(SPEC, deps, UserInput(text=text))
    return str(r.output or "")


def _ledger_micro(db):
    return int(db.query(func.coalesce(func.sum(AISpendEvent.cost_microusd), 0)).scalar() or 0)


async def _judge(question, answer, criterion):
    resp = await JUDGE.chat.completions.create(
        model="gpt-4o-mini", temperature=0, max_tokens=3,
        messages=[{"role": "system", "content":
                   "You grade an assistant answer against a criterion. Reply with exactly PASS or FAIL."},
                  {"role": "user", "content": f"Question: {question}\nAnswer: {answer}\nCriterion: {criterion}\nVerdict:"}])
    BUDGET.judge_in += resp.usage.prompt_tokens
    BUDGET.judge_out += resp.usage.completion_tokens
    return "PASS" in resp.choices[0].message.content.upper()


async def _evaluate(check: S.Check, answer, db, owner) -> bool:
    """Interpreting a declarative Check against the answer and/or the store."""
    a = answer.lower()
    live = store.live_memories(db, owner, "assistant")
    if check.kind == "substring_all":
        return all(x.lower() in a for x in check.args)
    if check.kind == "absent":
        return not any(x.lower() in a for x in check.args)
    if check.kind == "judge":
        return await _judge("", answer, check.args[0])
    if check.kind == "store_current":
        new, old = check.args
        return any(new.lower() in m.text.lower() for m in live) and not any(old.lower() in m.text.lower() for m in live)
    if check.kind == "store_count_at_least":
        n, tokens = check.args
        blob = " ".join(m.text.lower() for m in live)
        return sum(1 for t in tokens if t.lower() in blob) >= n
    if check.kind == "judge_and_gone":
        criterion, banned = check.args
        gone = not any(banned.lower() in m.text.lower() for m in live)
        return gone and await _judge("", answer, criterion)
    raise ValueError(f"unknown check kind {check.kind!r}")


async def _run_one(sem, idx, scen: S.Scenario):
    async with sem:
        if BUDGET.over():
            return {"cat": scen.category, "ok": None, "skipped": True}
        eng, factory = _fresh_db()
        db = factory()
        owner = f"u{idx}"
        try:
            write_conv = await _new_conv(db, owner)
            for turn in scen.setup:
                await asyncio.wait_for(_converse(factory, db, owner, write_conv, turn), timeout=90)
            q_conv = await _new_conv(db, owner)
            ans = await asyncio.wait_for(_converse(factory, db, owner, q_conv, scen.query), timeout=90)
            await BUDGET.add(_ledger_micro(db))
            ok = await _evaluate(scen.check, ans, db, owner)
            return {"cat": scen.category, "ok": ok, "q": scen.query, "ans": ans[:130],
                    "nmem": len(store.live_memories(db, owner, "assistant"))}
        except Exception as e:  # noqa: BLE001 — a scenario error is a data point, not a crash:
            return {"cat": scen.category, "ok": False, "error": f"{type(e).__name__}: {e}",
                    "tb": traceback.format_exc()[-300:]}
        finally:
            db.close()
            eng.dispose()


async def main(scale, concurrency, only=None):
    scen = S.build(scale)
    if only:
        wanted = {c.strip() for c in only.split(",")}
        scen = [s for s in scen if s.category in wanted]
    print(f"scenarios: {len(scen)}  concurrency: {concurrency}  ceiling: ${BUDGET.ceiling_micro/1e6:.2f}\n")
    print("coverage:\n" + S.coverage_table() + "\n")
    sem = asyncio.Semaphore(concurrency)
    t0 = time.time()
    results = await asyncio.gather(*[_run_one(sem, i, s) for i, s in enumerate(scen)])

    by_cat, fails, skipped = {}, [], 0
    for r in results:
        if r.get("skipped"):
            skipped += 1
            continue
        by_cat.setdefault(r["cat"], []).append(bool(r["ok"]))
        if not r["ok"]:
            fails.append(r)

    print("=" * 60)
    tot_ok = tot = 0
    for cat, oks in sorted(by_cat.items()):
        tot_ok += sum(oks); tot += len(oks)
        print(f"  {cat:12s} {sum(oks):4d}/{len(oks):<4d}")
    print(f"  {'-'*22}\n  {'TOTAL':12s} {tot_ok:4d}/{tot:<4d}   (skipped {skipped})")
    print(f"\n  spend: ${BUDGET.usd():.4f}   wall: {time.time()-t0:.0f}s")
    if fails:
        print(f"\n  FAILURES ({len(fails)}, first 30):")
        for f in fails[:30]:
            if f.get("error"):
                print(f"    [{f['cat']}] ERROR {f['error']}")
            else:
                print(f"    [{f['cat']}] q={f.get('q','')[:34]!r} ans={f.get('ans','')!r} nmem={f.get('nmem')}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=int, default=1)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--ceiling", type=float, default=5.0, help="hard USD cap; scenarios skip once exceeded")
    ap.add_argument("--only", type=str, default=None, help="comma-separated category filter (targeted runs)")
    ap.add_argument("--list", action="store_true", help="print the coverage map and exit")
    args = ap.parse_args()
    if args.list:
        print("Memory e2e coverage (category — unique axis):\n" + S.coverage_table())
    else:
        BUDGET.ceiling_micro = int(args.ceiling * 1e6)
        asyncio.run(main(args.scale, args.concurrency, only=args.only))
