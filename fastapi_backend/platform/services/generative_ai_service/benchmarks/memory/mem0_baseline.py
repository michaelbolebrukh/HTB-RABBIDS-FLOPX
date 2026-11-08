"""
Benchmark baseline — mem0 2.0.12 (live) on the SAME adversarial dataset, scored
the same way. Runs in baseline-venv. mem0 extracts facts internally and makes its
own update decisions, so we feed it the raw dated session text (date-prefixed, so
it has the same information our valid_at carries) and score its search/get_all.

mem0 has NO bi-temporal model: no valid-time, no as-of query, and its "update"
is a prose-merge decided per-add. The temporal/as-of cases therefore cannot be
answered by construction — that is the documented gap, recorded here as data.
"""

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("OPENAI_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
# Pin cheap models so the baseline costs the same order as ours:
os.environ.setdefault("MEM0_TELEMETRY", "false")

from mem0 import Memory  # noqa: E402

DATASET = Path(__file__).parent / "dataset.json"


def _value_token(phrase):
    toks = [w.strip(".,()").lower() for w in phrase.split()]
    for t in toks:
        if "@" in t or any(ch.isdigit() for ch in t):
            return t
    return max(toks or [phrase.lower()], key=len)


def _sig_tokens(phrase):
    import re
    stop = {"speaks", "child", "owns", "prefers", "hosted", "moved", "started", "current", "valid"}
    out = []
    for w in phrase.split():
        w = w.strip(".,()").lower()
        if len(w) <= 3 or w in stop or re.match(r"\d{4}-\d{2}-\d{2}", w):
            continue
        out.append(w)
    return out


def _config(collection):
    """gpt-4o-mini + text-embedding-3-small, per-case qdrant path (no lock contention):"""
    import tempfile
    path = os.path.join(tempfile.gettempdir(), "qmem", collection)
    return {
        "llm": {"provider": "openai", "config": {"model": "gpt-4o-mini", "temperature": 0}},
        "embedder": {"provider": "openai", "config": {"model": "text-embedding-3-small"}},
        "vector_store": {"provider": "qdrant", "config": {"collection_name": collection, "path": path}},
    }


def _all_texts(mem, uid):
    got = mem.get_all(filters={"user_id": uid})
    rows = got.get("results", got) if isinstance(got, dict) else got
    return [r.get("memory", "") for r in rows]


def _search_texts(mem, uid, query, k=5):
    res = mem.search(query, filters={"user_id": uid}, limit=k)
    rows = res.get("results", res) if isinstance(res, dict) else res
    return [r.get("memory", "") for r in rows]


def score(case, mem, uid):
    cat = case["category"]
    q = case["question"]

    if cat in ("single_hop", "multi_hop", "preference"):
        text = " ".join(_search_texts(mem, uid, q)).lower()
        golds = case.get("gold", [])
        ok = all(any(t in text for t in _sig_tokens(g)) for g in golds) if golds else bool(text)
        return ok, f"search={text[:80]}"

    if cat == "knowledge_update":
        beh = case.get("behavior", {})
        if beh.get("asof"):
            # mem0 has no as-of — it can only return its CURRENT belief. By
            # construction it cannot return a past value → fail:
            return False, "no-asof-capability (mem0 has no valid-time)"
        allt = " ".join(_all_texts(mem, uid)).lower()
        live_texts = _all_texts(mem, uid)
        cur = _value_token(beh.get("current", ""))
        stale = [_value_token(s) for s in beh.get("supersede", [])]
        has_cur = any(cur in t.lower() for t in live_texts)
        stale_gone = all(not any(s in t.lower() for t in live_texts) for s in stale)
        return (has_cur and stale_gone), f"all={[t[:30] for t in live_texts]}"

    if cat == "temporal":
        if case.get("asof"):
            return False, "no-asof-capability (mem0 has no valid-time)"
        text = " ".join(_search_texts(mem, uid, q)).lower()
        golds = case.get("gold", [])
        ok = all(any(t in text for t in _sig_tokens(g)) for g in golds) if golds else bool(text)
        return ok, f"search={text[:80]}"

    if cat == "multi_value":
        texts = _all_texts(mem, uid)
        vals = case["behavior"]["accrue"]
        blob = " ".join(texts).lower()
        present = [v for v in vals if v.lower() in blob]
        return (len(present) == len(vals)), f"{len(present)}/{len(vals)}; all={[t[:25] for t in texts]}"

    if cat == "abstention":
        text = " ".join(_search_texts(mem, uid, q)).lower()
        false_tokens = {"wife": ["wife"], "coffee": ["brand"], "car": ["drive"]}
        toks = []
        for kk, vv in false_tokens.items():
            if kk in q.lower():
                toks += vv
        ok = not any(t in text for t in toks)
        return ok, f"search={text[:60]}"

    return False, "?"


def main():
    cases = json.loads(DATASET.read_text())["cases"]
    results = []
    for case in cases:
        uid = case["id"]
        mem = Memory.from_config(_config(f"c_{uid}"))
        for i, sess in enumerate(case["sessions"]):
            # Date-prefixed so mem0 has the same info our valid_at carries:
            mem.add(f"On {sess['date']}: {sess['text']}", user_id=uid)
        ok, detail = score(case, mem, uid)
        results.append({"id": uid, "category": case["category"], "ok": ok, "detail": detail})
        print(f"  [{'PASS' if ok else 'FAIL'}] {uid:6s} {case['category']:16s} {detail[:90]}")

    by_cat = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r["ok"])
    print(f"\n{'='*60}\nMEM0 2.0.12 baseline\n{'='*60}")
    for cat, oks in sorted(by_cat.items()):
        print(f"  {cat:18s} {sum(oks)}/{len(oks)}")
    print(f"  {'-'*30}\n  {'TOTAL':18s} {sum(r['ok'] for r in results)}/{len(results)}")
    Path(__file__).parent.joinpath("mem0_results.json").write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
