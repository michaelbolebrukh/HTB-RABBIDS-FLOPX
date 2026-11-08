# Phase 10 — Durable agent-authored memory: interface spec

Durable, non-rotting, cross-conversation, **agent-authored** memory, built with
literal reference to the frontier (code studied under `scratchpad/frontier/`:
Graphiti, mem0, cognee, Letta, LangMem) and benchmarked live against mem0 —
**23/23 vs 16/23** (BENCHMARK.md). Not a library adoption: we port the proven
mechanisms onto our own portable stack so we own and can extend past them.

## Decisions (D55–D61, see README for the canonical rows)

- **D55** — agent-authored via tools, gated by `features.memory`; usage preamble.
- **D56** — bi-temporal fact store on the portable stack (JSON embedding, RRF).
- **D57** — write-time reconciliation (supersede vs accrue), not append-only.
- **D58** — injected resolver + embedder keep `memory/` SDK-free + offline-testable.
- **D59** — scope is an opaque app-supplied partition string on `RunDeps`.
- **D60** — two-phase write-completeness: real-time `memory_write` + a summary-time
  flush (`extract.py` + `make_memory_extractor`) that captures a compacted span's
  durable facts before they leave context.
- **D61** — per-run `db_lock` serializes concurrent tool DB hops (session race).
- **D62** — pgvector production recall: HNSW ANN + GIN full-text on PostgreSQL
  (`memory/pg_recall.py`, migration `20260713_13`); exact Python-cosine scan is
  the non-PG/test fallback. Portability requirement lifted for this service.

## The model — `ai_memories` (models.py)

A fact row carries two independent time axes (Graphiti, ported relationally):

| axis | columns | meaning |
|------|---------|---------|
| valid-time | `valid_at` / `invalid_at` | when the fact was true **in the world** |
| transaction-time | `recorded_at` / `superseded_at` | when **we** believed it (NULL `superseded_at` = live) |

Plus `content_hash` (md5 exact-dedup), `claim_key` (optional O(1) slot),
`embedding` (portable JSON), `source_turn_ids` (provenance), `superseded_by`.
Scoped `(owner_id, scope)` — durable across conversations. Both keys are opaque
strings the integrating app supplies per run on `RunDeps` (D59); the service
models no users/agents and no relation between them. The app sets the sharing
topology via `scope` (same shares, distinct isolates); `RunDeps.memory_scope`
unset → a neutral `"default"` namespace (zero-config fallback, not an opinion).
Index `ix_ai_memories_scope_live (owner_id, scope, superseded_at)`.

## The package (all `pydantic_ai`-free — D58)

- **`memory/store.py`** — DB + ranking. `content_hash`, Python `cosine`, lexical
  overlap, `rrf_fuse` (Graphiti verbatim), the bi-temporal `_live_at` predicate,
  CRUD (`add_memory`, `find_live_by_hash`, `current_claim_holder`,
  `invalidate` = retire-don't-delete), and `recall(query, as_of, k)` — hybrid
  semantic+lexical fused by RRF over the requested temporal slice.
- **`memory/reconcile.py`** — the write path. `temporal_resolve_contradictions`
  (pure Graphiti-ported arithmetic; every branch unit-tested offline) +
  `reconcile_write` (exact-dedup → recall → injected `resolve_fn` → temporal
  arithmetic → invalidate). Resolver prompt scaffolding (`RESOLVE_INSTRUCTIONS`,
  `build_resolve_prompt`, `parse_resolve_response`) lives here as pure strings.
- **`memory/tools.py`** — `memory_write / memory_search / memory_supersede /
  memory_delete` (Letta-adapted docstrings, uniqueness guards) + the always-on
  `MEMORY_PREAMBLE`. Registered into the tool table at import (D33).

## Engine wiring (engine.py / adapter.py)

`features.memory=True` →
1. the four memory tools are appended to the run's fill list;
2. the engine builds the injected callables through the facade — a shared
   embedder (`make_embedder`) + a cheap-model resolver (`make_memory_resolver`,
   cost-attributed `kind="memory"`) — and puts them on `ToolContext` along with
   `agent_name` and the triggering `current_turn_id` (provenance);
3. `MEMORY_PREAMBLE` is joined into the run's `extra_system` instructions.

Agents without the toggle are byte-for-byte unchanged.

## What we deliberately did NOT build

Letta's `memory_apply_patch` diff mini-language + git-memfs (over-engineered);
mem0's JSONB-blob schema (we promote typed columns); the entity knowledge graph
(needs Neo4j; multi-hop traversal is out of scope for fact memory — a separate
adopt-vs-build if ever needed). See MEMORY.md for the full provenance map.

## Tests

`tests/test_memory.py` (15, offline, $0): store hashing/similarity/RRF, all four
temporal-arithmetic branches, reconciliation (exact-dedup, supersession,
accrual, multi-supersession chain, no-LLM claim_key path), bi-temporal as-of
travel, scope isolation, and the engine-wiring integration (tools exposed +
preamble injected + provenance recorded). Live benchmark: `scratchpad/frontier/`.

## Out of scope (deferred)

Background reflection/consolidation pass (LangMem-style), context-compression
diffing (the earlier "memory ≈ deeper context management" thread), and
agent-collaboration shared memory (the P14 capstone) stay deferred per the plan.
