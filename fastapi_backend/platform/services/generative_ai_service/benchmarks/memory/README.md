# Memory benchmarks (P10)

Three deliberately separate layers. The unit tests prove *logic*; the store
benchmark proves we *beat the frontier*; the e2e benchmark proves the *whole
product behaves* under adversarial pressure. Keep them separate — each answers a
different question, and conflating them hides gaps.

| Layer | File | What it answers | Cost | In CI? |
|-------|------|-----------------|------|--------|
| Unit (logic) | `../../tests/test_memory.py` | Is the temporal/reconciliation logic correct, in isolation? | $0 | ✅ yes |
| Store vs frontier | `store_benchmark.py` + `mem0_baseline.py` + `dataset.json` | Does our engine beat mem0 on the hard cases? | ~cents (live) | ❌ manual |
| End-to-end adversarial | `e2e_benchmark.py` + `scenarios.py` | Does a real agent behave, across conversations, under attack? | ~$0.0016/scenario | ❌ manual |

The store + e2e layers call live model APIs, so they are **not** in CI (cost +
nondeterminism). Run them by hand when changing the memory engine.

## Prerequisites

- `OPENAI_API_KEY` in the environment (uses `gpt-4o-mini` + `text-embedding-3-small`).
- Run from the backend root with the venv: `cd fastapi_backend`.
- The mem0 baseline needs mem0 in a **separate** venv (its deps conflict with ours):
  `python -m venv /tmp/mem0-venv && /tmp/mem0-venv/bin/pip install mem0ai==2.0.12`.

## How to run

```bash
# 1. Unit logic (fast, free, deterministic) — same as CI:
.venv/bin/python -m pytest platform/services/generative_ai_service/tests/test_memory.py -q

# 2. Store benchmark — validate offline first (free), then live:
.venv/bin/python platform/services/generative_ai_service/benchmarks/memory/store_benchmark.py --mode offline
.venv/bin/python platform/services/generative_ai_service/benchmarks/memory/store_benchmark.py --mode live
#    ...and the frontier baseline (separate venv), then compare TOTALS:
/tmp/mem0-venv/bin/python platform/services/generative_ai_service/benchmarks/memory/mem0_baseline.py

# 3. End-to-end adversarial — always dry-run the coverage map first:
.venv/bin/python platform/services/generative_ai_service/benchmarks/memory/e2e_benchmark.py --list
#    small smoke ($ ~0.03), then scale up under a hard cost ceiling:
.venv/bin/python platform/services/generative_ai_service/benchmarks/memory/e2e_benchmark.py --scale 1  --concurrency 6  --ceiling 1.0
.venv/bin/python platform/services/generative_ai_service/benchmarks/memory/e2e_benchmark.py --scale 130 --concurrency 16 --ceiling 5.0
```

`--ceiling` is a hard USD cap: once the metered spend (read from each scenario's
own ledger) crosses it, remaining scenarios are skipped rather than run. Always
set it.

## How to EXPAND without retesting the same thing

The e2e suite is data-driven so adding coverage never means copy-pasting a runner.

1. Open `scenarios.py`. Each `Category` has an `axis` string — the ONE property
   it uniquely tests. Read the existing axes first.
2. If your new case exercises an axis **already listed**, it is redundant — widen
   the existing generator's fixtures instead (more names/cities), don't add a
   category.
3. If it exercises a **new** axis, write a generator returning `Scenario`s and
   append one `Category(key, axis, generator)` to `CATALOG`. The runner picks it
   up automatically; `--list` will show it.
4. Scoring is declarative (`Check`): reuse `substring_all` / `absent` / `judge` /
   `store_current` / `store_count_at_least`. Add a new `Check.kind` (and a branch
   in `e2e_benchmark._evaluate`) only for a genuinely new scoring shape.

The store benchmark expands the same way: add a case to `dataset.json` with a
`category` + `behavior`/`gold`, and (if a new category) a branch in
`store_benchmark._score`.

## Current coverage (e2e axes)

Run `... e2e_benchmark --list` for the live list. As of P10:
recall · supersede · accrual · **as_of (agent time-travel)** · abstention ·
distractor · injection · preference · **exotic (negation + same-turn correction)**.

## Scale harness (D62)

`scale_harness.py` measures recall latency + ranking quality at N=100/1k/10k
memories per scope, on BOTH engines: the pgvector ANN path (needs
`TEST_DATABASE_URL` pointing at a Postgres with the extension) and the Python
fallback (any DB). Offline, $0 (synthetic deterministic vectors):

```bash
TEST_DATABASE_URL=postgresql+psycopg2://dev:dev@localhost:5432/memtest \
  .venv/bin/python platform/services/generative_ai_service/benchmarks/memory/scale_harness.py
```

Measured (local PG 16 + pgvector 0.6.0, July 2026) — identical ranking quality
(recall@5 100% on both engines), ~50× latency separation at 10k:

| N      | engine   | r@5  | p50      | p95      |
|--------|----------|------|----------|----------|
| 100    | pgvector | 100% | 9 ms     | 10 ms    |
| 100    | python   | 100% | 44 ms    | 45 ms    |
| 1,000  | pgvector | 100% | 16 ms    | 17 ms    |
| 1,000  | python   | 100% | 474 ms   | 607 ms   |
| 10,000 | pgvector | 100% | 110 ms   | 320 ms   |
| 10,000 | python   | 100% | 5,386 ms | 6,210 ms |

## Long-conversation flush e2e (D60)

`flush_e2e.py` proves the summary-time flush LIVE, end to end: the chat model's
window is patched to ~2.5k tokens so genuine compaction fires within six verbose
turns; three durable facts are dropped as terse asides (never "please
remember"), the flush extracts them at compaction, and a fresh conversation must
recall all three. Costs ~$0.03. Result (July 2026): **3/3 facts survived**.

## Known gaps (tracked, not yet covered)

- (none currently — scale, concurrency, agent-driven forgetting, and the
  long-conversation flush are all covered; expand via the axis rules above.)
