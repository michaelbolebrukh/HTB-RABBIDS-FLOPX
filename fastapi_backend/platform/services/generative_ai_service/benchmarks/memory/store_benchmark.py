"""
Store-level memory benchmark — scores OUR memory engine directly (bypassing the
agent) on the handcrafted `dataset.json`, on the properties the store owns:
retrieval hit@k, contradiction supersession, bi-temporal as-of, multi-value
accrual, abstention. This is the apples-to-apples counterpart to
`mem0_baseline.py` (run both, compare the totals).

Two modes so logic is validated offline before spending a cent:
    --mode offline : fake resolver + hash embedder ($0; the knowledge-update
                     cases legitimately fail offline — they need the real LLM).
    --mode live    : gpt-4o-mini resolver + text-embedding-3-small (a few cents).

Run (from repo root `fastapi_backend/`), as a plain script (NOT `-m`):
    .venv/bin/python platform/services/generative_ai_service/benchmarks/memory/store_benchmark.py --mode live
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Env + path before the service package imports Settings (backend root = parents[5]):
sys.path.insert(0, str(Path(__file__).resolve().parents[5]))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("HASH_KEY", "bench")
os.environ.setdefault("APP_DOMAIN", "test.local")
os.environ.setdefault("SENTRY_DSN", "")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from infrastructure.database import Base
from platform.services.generative_ai_service.memory import ResolveResult, reconcile_write, store
from platform.services.generative_ai_service.memory.reconcile import (
    RESOLVE_INSTRUCTIONS, build_resolve_prompt, parse_resolve_response,
)
from platform.services.generative_ai_service.models import AIMemory

DATASET = Path(__file__).parent / "dataset.json"
CALL_CEILING = 400
OWNER_AGENT = "assistant"
_STOP = {"speaks", "child", "owns", "prefers", "hosted", "moved", "started", "current", "valid"}


def D(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


class Counter:
    def __init__(self):
        self.resolve = self.embed = self.tin = self.tout = 0


def _offline_embed(texts):
    out = []
    for t in texts:
        v = [0.0] * 32
        for tok in t.lower().split():
            v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % 32] += 1.0
        out.append(v)
    return out


def _offline_resolver(counter):
    async def _r(new_text, cands):
        counter.resolve += 1
        key = new_text.lower().split()[:3]
        return ResolveResult(contradicted_idxs=[i for i, c in enumerate(cands) if c.lower().split()[:3] == key])
    return _r


def _live_doubles(counter):
    from openai import OpenAI
    client = OpenAI()

    def embed(texts):
        counter.embed += 1
        resp = client.embeddings.create(model="text-embedding-3-small", input=texts)
        counter.tin += resp.usage.total_tokens
        return [d.embedding for d in resp.data]

    async def resolve(new_text, cands):
        if not cands:
            return ResolveResult()
        counter.resolve += 1
        if counter.resolve > CALL_CEILING:
            raise RuntimeError(f"call ceiling {CALL_CEILING} exceeded")
        resp = client.chat.completions.create(
            model="gpt-4o-mini", temperature=0, response_format={"type": "json_object"},
            messages=[{"role": "system", "content": RESOLVE_INSTRUCTIONS},
                      {"role": "user", "content": build_resolve_prompt(new_text, cands)}])
        counter.tin += resp.usage.prompt_tokens
        counter.tout += resp.usage.completion_tokens
        return parse_resolve_response(resp.choices[0].message.content, len(cands))

    return embed, resolve


def _sig(phrase):
    out = []
    for w in phrase.split():
        w = w.strip(".,()").lower()
        if len(w) <= 3 or w in _STOP or re.match(r"\d{4}-\d{2}-\d{2}", w):
            continue
        out.append(w)
    return out


def _value_token(phrase):
    toks = [w.strip(".,()").lower() for w in phrase.split()]
    for t in toks:
        if "@" in t or any(c.isdigit() for c in t):
            return t
    return max(toks or [phrase.lower()], key=len)


def _recall(db, owner, q, embed, k=5, as_of=None):
    return store.recall(db, owner_id=owner, scope=OWNER_AGENT, query=q,
                        query_embedding=embed([q])[0], k=k, as_of=as_of)


def _gold_ok(hits, case):
    golds = case.get("gold", [])
    if not golds:
        return bool(hits)
    text = " ".join(m.text.lower() for m, _ in hits)
    return all(any(t in text for t in _sig(g)) or not _sig(g) for g in golds)


def _score(db, owner, case, embed):
    cat, q = case["category"], case["question"]
    if cat in ("single_hop", "multi_hop", "preference"):
        return _gold_ok(_recall(db, owner, q, embed), case)
    if cat == "knowledge_update":
        beh = case.get("behavior", {})
        if beh.get("asof"):
            hits = _recall(db, owner, q, embed, as_of=D(case["asof"]))
            want = _value_token(beh["current_at_date"])
            return any(want in m.text.lower() for m, _ in hits)
        live, allm = store.live_memories(db, owner, OWNER_AGENT), store.all_memories(db, owner, OWNER_AGENT)
        cur = _value_token(beh.get("current", ""))
        stale = [_value_token(s) for s in beh.get("supersede", [])]
        return (any(cur in m.text.lower() for m in live)
                and all(not any(s in m.text.lower() for m in live) for s in stale)
                and all(any(s in m.text.lower() for m in allm) for s in stale))
    if cat == "temporal":
        if case.get("asof"):
            hits = _recall(db, owner, q, embed, as_of=D(case["asof"]))
            return any(max(case["expected"].split(), key=len).lower() in m.text.lower() for m, _ in hits)
        return _gold_ok(_recall(db, owner, q, embed), case)
    if cat == "multi_value":
        live = store.live_memories(db, owner, OWNER_AGENT)
        vals = case["behavior"]["accrue"]
        return all(any(v.lower() in m.text.lower() for m in live) for v in vals)
    if cat == "abstention":
        hits = _recall(db, owner, q, embed)
        banned = {"wife": ["wife"], "coffee brand": ["brand"], "car": ["drive"]}
        toks = [t for k, v in banned.items() if k in q.lower() for t in v]
        return not any(any(t in m.text.lower() for t in toks) for m, _ in hits)
    return False


async def run(mode):
    cases = json.loads(DATASET.read_text())["cases"]
    counter = Counter()
    embed, resolve = _live_doubles(counter) if mode == "live" else (_offline_embed, _offline_resolver(counter))
    by_cat, fails = {}, []
    for case in cases:
        eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(eng, tables=[AIMemory.__table__])
        db = sessionmaker(bind=eng)()
        owner = case["id"]
        for i, sess in enumerate(case["sessions"]):
            await reconcile_write(db, owner_id=owner, scope=OWNER_AGENT, text=sess["text"],
                                  valid_at=D(sess["date"]), source_turn_ids=[f"{owner}-{i}"],
                                  embed_fn=embed, resolve_fn=resolve)
        ok = _score(db, owner, case, embed)
        by_cat.setdefault(case["category"], []).append(ok)
        if not ok:
            fails.append(case["id"])
        db.close(); eng.dispose()

    print(f"\nSTORE BENCHMARK (ours) — mode={mode}\n" + "=" * 40)
    tot_ok = tot = 0
    for cat, oks in sorted(by_cat.items()):
        tot_ok += sum(oks); tot += len(oks)
        print(f"  {cat:18s} {sum(oks)}/{len(oks)}")
    print(f"  {'-'*26}\n  {'TOTAL':18s} {tot_ok}/{tot}")
    cost = counter.tin / 1e6 * 0.15 + counter.tout / 1e6 * 0.60
    print(f"\n  resolve_calls={counter.resolve} tokens(in/out)={counter.tin}/{counter.tout} est_cost=${cost:.4f}")
    if fails:
        print(f"  failures: {fails}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["offline", "live"], default="offline")
    asyncio.run(run(ap.parse_args().mode))
