# Phase 3 — Model Registry: Development Spec

Status: spec for review (July 2026). Companion to `README.md` (D1–D30),
`PHASE1.md` (transcript), `PHASE2.md` (engine). Replaces the static
`engine/model_info.py` seed with a synced, richer registry — without changing
any caller.

## Goals / non-goals

**Goals**: a maintained registry of model capabilities, costs, context windows,
and settings for every model we might use; synced from three machine-readable
feeds; hand-overrides win; a query API ("models with reasoning + video input +
≥400k context under $X/Mtok"); and it feeds P4's cost meter with real prices.

**Non-goals**: budget *enforcement* (P4 — this phase only supplies the prices);
the scheduled-agent "research a brand-new model" loop (a later enhancement — P3
ships the deterministic feed-sync + reconciler-PR skeleton).

## Design decisions

**No DB table — a committed data file is the source of truth (D26-friendly).**
The registry is read on nearly every run and written rarely (a periodic sync).
Rather than a DB table + per-run query, the registry is a committed JSON file
loaded into memory once at process start:
- `registry/data/models.json` — the merged registry (runtime source of truth).
- `registry/overrides.json` — hand-pinned entries/fields that always win.
- Travels with the package on extraction (no host DB coupling), and a run needs
  zero DB round-trips to price/route a model.

**Sync is a script that rewrites the file; the reconciler opens a PR (D6).**
The scheduled agent is a *reconciler*, not the runtime source: it runs the sync,
diffs against the committed file, and opens a PR for human review. Runtime never
writes the registry. This is what keeps "auto-updated" from meaning "silently
mutated in prod".

**Backward-compatible superset (no caller changes).** `get_model_info(model_id)`
keeps its signature and `ModelInfo` keeps every current field; P3 only *adds*
optional fields (all defaulted), so the renderer/providers/adapter are untouched.

## Package layout (additions)

```
generative_ai_service/
  registry/
    __init__.py          # public: get_model_info, find_models, all_models
    schema.py            # ModelInfo (superset), Cost, Capabilities, Modalities
    loader.py            # load models.json + overrides at import; the in-memory index
    query.py             # find_models(**filters) predicate engine
    sync.py              # fetch 3 feeds -> merge -> write models.json (offline job)
    feeds/
      models_dev.py      # models.dev api.json adapter -> our schema
      litellm.py         # LiteLLM model_prices_and_context_window.json adapter
      openrouter.py      # OpenRouter GET /models adapter
    data/models.json     # committed merged registry
    overrides.json       # committed hand-pins
  engine/model_info.py   # becomes a thin re-export of registry.get_model_info (keeps the P2 import path)
```

`engine/model_info.py` stays as a shim re-exporting from `registry` so P2's
existing imports keep working (and the facade rule is unaffected — registry has
no `pydantic_ai` import either).

## Schema (`registry/schema.py`) — superset of the current ModelInfo

```python
class Cost(BaseModel):
    # USD per MILLION tokens (matches LiteLLM/models.dev units):
    input: float | None = None
    output: float | None = None
    cache_read: float | None = None
    cache_write: float | None = None
    reasoning: float | None = None
    input_audio: float | None = None
    output_audio: float | None = None
    # Per-unit frontier-feature costs (LiteLLM has these; P4 uses them):
    web_search_per_call: float | None = None
    per_request: float | None = None


class Modalities(BaseModel):
    input: list[str] = ["text"]         # text | image | audio | video | pdf
    output: list[str] = ["text"]


class Capabilities(BaseModel):
    tool_call: bool = True
    structured_output: bool = True      # native JSON-schema mode (D20)
    reasoning: bool = False             # == current supports_thinking
    vision: bool = False
    web_search: bool = False
    prompt_caching: bool = False
    parallel_tool_calls: bool = False


class ModelInfo(BaseModel):
    # --- existing P2 fields (unchanged, callers depend on these) ---
    id: str
    provider: str
    context_window: int = 128_000
    max_output_tokens: int = 16_384
    tokenizer_family: str = "generic"
    supports_structured_output: bool = True
    supports_thinking: bool = False
    # --- P3 additions (all optional/defaulted) ---
    cost: Cost = Cost()
    capabilities: Capabilities = Capabilities()
    modalities: Modalities = Modalities()
    lifecycle: str = "stable"           # alpha | beta | stable | deprecated
    knowledge_cutoff: str | None = None
    source: str = "seed"                # which feed won: overrides|models_dev|litellm|openrouter|seed
    synced_at: str | None = None
```

`supports_thinking`/`supports_structured_output` stay as-is and are kept in sync
with `capabilities.reasoning`/`.structured_output` by the loader, so old and new
call sites agree.

## Loader (`registry/loader.py`)

- At import: read `data/models.json` (list of ModelInfo dicts) into an in-memory
  `dict[str, ModelInfo]`; layer `overrides.json` on top (per-field merge, override
  wins); validate through the pydantic schema.
- `get_model_info(model_id)` — exact hit → return; miss → the same conservative
  provider-prefix default as today (a new model still runs day-one with safe
  window assumptions until the next sync).
- Reload is process-restart (matches the committed-file model; a deploy ships new
  data). No runtime mutation.

## Query API (`registry/query.py`)

```python
def find_models(
    *, reasoning: bool | None = None, vision: bool | None = None,
    input_modalities: list[str] | None = None, min_context: int | None = None,
    max_input_cost: float | None = None, provider: str | None = None,
    exclude_deprecated: bool = True,
) -> list[ModelInfo]: ...
```

A plain predicate filter over the in-memory index — this is the surface a future
router/agent uses to pick a model by capability+budget.

## Sync (`registry/sync.py` + `feeds/`) — offline job, never runtime

Each feed adapter fetches its source and maps it into our `ModelInfo`, keyed by
our canonical `provider:model` id. Merge order (later overrides earlier on a
per-field basis), documented and testable:

1. OpenRouter `/models` (broad ids, modalities, supported params)
2. LiteLLM JSON (widest coverage, richest cost dimensions)
3. models.dev (clean capability booleans, lifecycle)
4. `overrides.json` (hand-pins always win)

`sync.py` writes `data/models.json` deterministically (sorted keys, stable
formatting) so a diff is reviewable. Network lives ONLY here, behind `httpx`
with timeouts; a feed that fails is skipped with a logged warning (partial sync
beats no sync). Id-mapping mismatches between feeds are the known hard part —
handled by a per-feed id-normalizer with an explicit unmatched-ids report.

## Reconciler (skeleton this phase)

A scheduled job (cron) that runs `sync.py`, and if `models.json` changed, opens
a PR with the diff for human review — never commits to `main` unattended.
This phase ships the sync + a documented manual/cron invocation; wiring it to an
actual scheduler (and the "agent researches a brand-new model's specs" step) is
a follow-up.

## Tests (offline; recorded feed fixtures, no live network)

- Feed adapters: each maps a saved sample payload → correct `ModelInfo` fields.
- Merge: per-field priority (override > models.dev > litellm > openrouter);
  unmatched-id reporting; partial-sync-on-feed-failure.
- Loader: overrides win; unknown id → conservative default; old/new field sync
  (`supports_thinking` ⇔ `capabilities.reasoning`).
- Query: each filter selects correctly; `exclude_deprecated` default.
- Back-compat: every model in the current P2 seed still resolves with the same
  window/tokenizer values via the new loader (a golden test so P2 can't regress).

## Done criteria

`get_model_info` returns registry-backed data (cost + capabilities populated)
for our core models; `find_models` answers capability+budget queries; `sync.py`
regenerates `models.json` from the three feeds offline and deterministically;
P2 callers unchanged; all offline tests green; no `pydantic_ai` import in
`registry/`.
