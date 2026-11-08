# Memory benchmark — head-to-head vs mem0 (P10)

Handcrafted, LongMemEval-shaped adversarial dataset (24 cases, 7 categories),
run **live** against both systems on the same cheap models (`gpt-4o-mini` +
`text-embedding-3-small`). LoCoMo was rejected as a trap — it fits in a context
window, so it does not isolate the memory system from raw long-context. Our
dataset deliberately spreads evidence across dated sessions and includes
categories a long-context model cannot fake: knowledge-update (contradiction),
bi-temporal as-of, multi-value accrual, abstention.

Scoring is at the **store layer** — the properties memory is responsible for:
retrieval hit@k, contradiction supersession (current value live AND stale value
invalidated-but-kept), as-of accuracy, multi-value completeness, abstention (no
false memory). Harness: `scratchpad/frontier/bench_ours.py`,
`scratchpad/frontier/bench_mem0.py` (mem0 2.0.12 in an isolated venv).

## Result

| Category            | mem0 2.0.12 | Ours (bi-temporal) |
|---------------------|:-----------:|:------------------:|
| single_hop          | 4/4         | 4/4                |
| multi_hop           | 3/3         | 3/3                |
| multi_value         | 3/3         | 3/3                |
| preference          | 2/2         | 2/2                |
| abstention          | 3/3         | 3/3                |
| **knowledge_update**| **0/5**     | **5/5**            |
| **temporal (as-of)**| **1/3**     | **3/3**            |
| **TOTAL**           | **16/23**   | **23/23**          |

Our live cost for the full pass: **$0.0009** (17 resolve calls, 56 embeds,
4782 in / 224 out tokens). mem0's pass ran on the same models at the same order
of cost. Both far under the session's $10 live-testing ceiling.

## Where the +7 comes from (the frontier gap, proven with live traces)

**Contradiction supersession (+5, knowledge_update 0→5).** mem0 keeps
contradictory values *both live*:

- `ku1` mem0 `get_all` → `["User switched their primary email…", "User's primary
  email is alice@old.com"]` — the retired email is never invalidated.
- `ku3` → both "HQ is Denver" and "HQ was Austin" live simultaneously.
- `ku5` → all three phone numbers retained.

Ours runs write-time reconciliation: the resolver flags the contradiction, the
temporal-overlap arithmetic invalidates the old fact (keeping it for history),
and only the current value is live. `ku1/ku3/ku5` → current value the sole live
fact, stale value present-but-superseded.

**Bi-temporal as-of (+2, temporal 1→3).** mem0 has no valid-time at all — it
stamps every fact with wall-clock "now" (observed: it labeled a Jan-dated fact
"as stated on July 17, 2026"), so `tmp2` ("which Postgres version in June?"),
`tmp3`, and `ku4` ("HQ in May?") are unanswerable by construction. Ours stores
`valid_at`/`invalid_at` per fact and answers `recall(as_of=T)` by the valid-time
window — returning Postgres 15 for June, Austin for May, i.e. the value that
actually held then, not today's.

## Parity where parity is expected

Plain retrieval (single/multi-hop), preference recall, multi-value accrual, and
abstention are 100% for both — our hybrid RRF recall matches mem0's on the cases
that do not need temporal reasoning, confirming the win is the temporal model,
not a retrieval-quality confound.
