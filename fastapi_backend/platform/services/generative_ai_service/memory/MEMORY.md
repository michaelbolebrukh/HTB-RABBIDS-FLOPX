# Agent-authored memory — design & frontier provenance (P10)

Durable, non-rotting, agent-authored, cross-conversation memory, built with
**literal reference** to the frontier implementations (mem0, Graphiti, Letta,
cognee, LangMem — code studied under `scratchpad/frontier/`). We are NOT
adopting a library (only mem0 was adoptable, and its value is thin: it already
dropped write-time reconciliation in v3, drags qdrant, and has no bi-temporal
model or provenance). Instead we port the *proven mechanisms* onto our own
Postgres/JSON-embedding stack so we own — and can extend past — them.

## The model: a bi-temporal fact store (Graphiti, relationally)

Graphiti's whole temporal-memory value is **relationally expressible** — the
only irreducibly graph-shaped feature is multi-hop traversal, which fact-memory
doesn't need. So a `memories` table with four timestamps IS the frontier:

- **valid-time** — `valid_at` / `invalid_at`: when the fact was true *in the world*.
- **transaction-time** — `recorded_at` / `superseded_at`: when *we* believed it.

"Invalidate, don't delete": a superseded fact keeps its row with `superseded_at`
+ `invalid_at` set, so "what's true now", "what was true at date T" (valid-time
travel), and "what did we believe at time TT" (audit) are all one SQL WHERE.
(Ported from `graphiti_core/edges.py:263-285` + `search_filters.py`.)

## Write-time reconciliation (the part mem0/cognee/Letta all lack)

`memory_write(fact)` →
1. **Exact-dedup fast path** — normalized `content_hash`; a live row with the
   same hash just appends the source turn id, no LLM. (mem0 md5 dedup +
   Graphiti verbatim short-circuit.)
2. **Candidate recall** — semantic (Python cosine over the scope's live rows) +
   lexical (token overlap), fused by **RRF** (ported from
   `graphiti_core/search/search_utils.py:1780`); plus the current holder of the
   fact's `claim_key` slot if one is given (cognee deterministic-slot lookup).
3. **LLM resolve** — the candidates are shown integer-indexed with a side
   uuid map (mem0 anti-hallucination remap, `main.py:889`); the model returns
   `duplicate_facts` vs `contradicted_facts` (Graphiti `resolve_edge` prompt,
   `prompts/dedupe_edges.py:24`). Injected as a `ResolveFn` so offline tests run
   with a fake resolver (zero live cost), live uses `gpt-4o-mini`.
4. **Temporal-overlap arithmetic** — ported near-verbatim from Graphiti's
   `resolve_edge_contradictions` (`edge_operations.py:538-573`): a contradiction
   only supersedes when the validity windows *overlap* and the old fact started
   first; **sequential truths ("Paris 2018-2020" then "London since 2021") stay
   both-live**. This is the supersede-vs-accrue decision, done on timestamps —
   the exact case where mem0 (prose-merges non-deterministically) and cognee
   (first-write-wins) fail.

## Retrieval

Hybrid semantic + lexical fused by RRF, filtered to the requested temporal slice
(`live()` / `as_of_valid(t)` / `as_of_tx(tt)`), returned as compact digests. An
`episode_mentions`-style tiebreak rewards facts corroborated by many turns
(`cardinality(source_turn_ids)`, Graphiti `search_utils.py:1860`).

## Provenance (which frontier does poorly, we do natively)

Every memory carries `source_turn_ids` (append-on-dedup, Graphiti `episodes[]`),
linking back to the transcript items that produced it — for audit and conflict
resolution. mem0 OSS can't do this (no memory→message link); we already have
stable transcript item ids.

## Tools (Letta docstrings, adapted)

`memory_write`, `memory_search`, `memory_supersede`, `memory_delete` — docstrings
adapted from Letta's battle-tuned `archival_memory_insert/_search` +
`core_memory_replace` (`function_sets/base.py`), including the **uniqueness
guard** on supersede/delete. Background embedding reuses our D35 backfill.

## What we deliberately did NOT copy

Letta's `memory_apply_patch` diff mini-language + git-memfs (over-engineered);
mem0's JSONB-blob-everything schema (we promote real typed columns); the entity
knowledge graph (needs Neo4j; multi-hop is out of scope for fact memory — a
separate adopt-vs-build if ever needed).

## Benchmark

Handcrafted, LongMemEval-shaped (LoCoMo is a trap — it fits in a context window,
so it doesn't isolate memory). Categories: single-hop, multi-hop, temporal,
knowledge-update/contradiction, multi-value, abstention, preference. We score
retrieval hit@k, contradiction-resolution correctness (return current AND
invalidate stale — tracked separately), bi-temporal as-of accuracy, multi-value
completeness, abstention rate, token use — head-to-head against **live mem0** as
the baseline. See `tests/` and the benchmark harness.
