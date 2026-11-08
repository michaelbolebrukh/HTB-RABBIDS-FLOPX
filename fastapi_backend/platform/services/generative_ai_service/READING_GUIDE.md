# Reading Guide — understanding the AI service build

A guided path through everything built in P1 (transcript) and P2 (engine), with
a one-time primer on the unusual Python/library syntax used throughout. Read the
primer first, then walk the files in order. Every file is thoroughly commented;
this guide gives you the *order* and the *why*, so the comments land in context.

---

## Part 0 — Syntax & patterns primer (read once)

These constructs recur across the code. Understanding them here means the files
read smoothly.

**Pydantic `BaseModel` vs `@dataclass`.** Both define "a class with typed
fields". We use `BaseModel` when the object crosses a boundary and must be
*validated* or *serialized to/from JSON* (e.g. `Item`, `AgentSpec`, every event).
We use `@dataclass` for internal, in-process value bags that never get validated
or serialized (e.g. `RunDeps`, `BuiltModel`, `RenderedContext`) — lighter, no
validation overhead.

**Discriminated union** — `Annotated[Union[A, B, C], Field(discriminator="type")]`.
A field that can be one of several shapes, where a literal tag field (`type`)
says which. When pydantic deserializes JSON, it reads `type` and picks the exact
class — no guessing. We use it for `ContentPart` (text/media/data) and `RunEvent`
(the event stream). `Literal["text"]` on a field means "this field's value is
exactly the string `text`" — that's the tag.

**`TypeAdapter`** — `content_parts_adapter = TypeAdapter(list[ContentPart])`.
Pydantic validation/serialization for a type that isn't a `BaseModel` subclass
(here, a *list of a union*). `.validate_python(x)` parses, `.dump_python(x,
mode="json")` serializes. It's how we round-trip the `parts` list to/from a JSON
DB column.

**`@model_validator(mode="after")`** (pydantic). A method that runs *after* a
model's fields are populated, to enforce cross-field rules. In `Item` it enforces
"tool items must carry `tool_name`; message items must have a `role`" — invariants
a plain type hint can't express.

**`Protocol`** (structural typing) — `EmbedderProtocol`. "Any object with an
`embed(texts) -> vectors` method qualifies", without inheritance. Lets tests pass
a fake embedder and P2 pass Pydantic AI's real one, while `search.py` depends on
neither — the seam that keeps the AI SDK out of the transcript layer.

**`async def` + `AsyncIterator` + `async for` + `yield`.** An `async def` with
`yield` is an *async generator* — it produces values lazily over time. The run
engine is one: `async for event in stream_agent(...)` pulls events as the model
produces them. Crucially, if the consumer stops (client disconnects), the
generator is *cancelled* mid-flight — that's how we stop paying for tokens when a
user closes the tab (D29).

**`asyncio.to_thread(fn, *args)`.** Runs a *blocking* function on a worker thread
so it doesn't freeze the event loop. Our DB session is synchronous SQLAlchemy, so
every DB call from async code is wrapped this way (D29). Millisecond holds, so the
thread pool is never a bottleneck.

**Injected callbacks (dependency injection without a framework).** The renderer
takes `summarize`, `cache_get`, `cache_put` as *function arguments* rather than
importing the model or the DB. This keeps it pure policy (fully testable with
fakes) and is why `renderer.py` has zero `pydantic_ai` and zero ORM imports.

**`from __future__ import annotations`.** At the top of most files. Makes all
type hints lazy strings, so you can reference types not yet defined and avoid
import cycles. Purely a typing convenience; no runtime effect.

**SQLAlchemy `Column(...)`, `ForeignKey(..., ondelete="CASCADE")`, portable
types.** `models.py` declares tables as classes. `Uuid`/`JSONColumn` come from
`infrastructure/db_types` and render correctly on both PostgreSQL and SQLite (the
repo's portability rule). `ondelete="CASCADE"` = deleting a conversation deletes
its items at the DB level.

**Comment style.** Repo convention: inline comments are present-continuous and
end with a colon ("Summing the parts:"). Docstrings describe the unit. `# noqa:
BLE001` silences the "blind except" linter where catching everything is
*deliberate* (e.g. the summarizer failure policy).

---

## Part 1 — P1: the transcript store (the data foundation)

Read in this order. Nothing here imports `pydantic_ai`.

1. **`README.md`** (skim D1–D30) — the decision record. You don't need all of it;
   note D3 (facade rule), D4/D24 (item transcript + two-lane `raw`), D30 (RAG
   scope). The decisions are referenced by id throughout the code.

2. **`transcript/types.py`** — START HERE. The `Item` model and the two-lane
   rendering invariant (top-of-file docstring is the single most important
   concept in the codebase). See: `ItemKind`, `ContentPart` union, `Usage`, the
   `@model_validator`, `extract_text()`.

3. **`models.py`** — the three ORM tables (`ai_conversations`,
   `ai_transcript_items`, `ai_transcript_summaries`). Note how the columns mirror
   `Item` one-to-one, and the `seq` column comment (why timestamps can't order).

4. **`transcript/store.py`** — the only writer. Read `_row_to_item`/`_item_to_row`
   (the dumb field-copy conversion), then `append_item` → `append_items` (atomic)
   → `branch_from` (editing = sibling branch) → `get_path` (root→leaf walk) →
   `full_history`. Finish with `get_summary`/`put_summary` (the cache).

5. **`transcript/search.py`** — one public `search_conversation`, two backends
   (text + embedding), the D30 `scope` filter, the `EmbedderProtocol` seam.

6. **`exceptions.py`** — tiny; the framework-free error types (D26).

7. **`migrations/versions/20260704_01…` + `20260705_01…`** — the schema in SQL
   form, and the idempotency guards (why they exist: create_all-then-migrate).

→ Good checkpoint to converse: *how does an edited message coexist with
append-only, and how does search avoid resurfacing the edited-away branch?*

---

## Part 2 — P2: the engine (turning a spec into a streamed run)

Now the layer that uses P1 and hides Pydantic AI. Read the boundary from the
outside in.

8. **`engine/types.py`** — the vocabulary: `AgentSpec` (+ `AgentFeatures`,
   `AgentLimits`), `RunDeps` (the trust boundary, D11), `UserInput`, and the
   `RunEvent` discriminated union. This is the entire public contract.

9. **`engine/model_info.py`** — the small static model registry (windows,
   tokenizer families, structured-output support). The P3 seam: signature is
   stable, backend gets replaced.

10. **`engine/renderer.py`** — pure summarization *policy* (no SDK, no ORM). Read
    the top docstring, then `render_context`: the trigger/target thresholds, the
    turn-seam snapping (`_snap_to_seam`), the summary-cache lookup, and the
    failure→truncation policy. The injected-callback pattern from the primer is
    everywhere here.

11. **`engine/providers.py`** — model *construction*. `build_model` →
    `BuiltModel`, the OpenAI→Responses choice, xAI-via-OpenAI-compatible, and the
    `FallbackModel` chain (D28). One of the two files allowed to import
    `pydantic_ai`.

12. **`engine/adapter.py`** — THE seam. The densest file; read it slowly. Four
    jobs: `build_agent` (spec→SDK Agent), `to_message_history` (our items→SDK
    messages, the two-lane translation in code), `translate_event` (SDK events→our
    events), `items_from_messages` (SDK output→our items). Then `stream_run`
    drives it all.

13. **`engine/engine.py`** — the orchestrator that ties P1+P2 together.
    `stream_agent`: append user item → render → **release DB connection** → stream
    → append outputs atomically → complete. This is the whole flow on one screen.

14. **`routers.py`** — the thin HTTP surface: create-conversation + the SSE run
    endpoint. Note `event_source` (async generator → SSE) and how FastAPI DI
    (`db`, `user`) feeds `RunDeps`.

→ Good checkpoint to converse: *trace one user message from HTTP request to
persisted assistant reply — which module owns each step, and where (exactly) is
Pydantic AI touched?*

---

## Part 3 — how it's proven

15. **`tests/test_facade_boundary.py`** — the machine-enforced rule: greps the
    source so nothing above the adapter imports `pydantic_ai`.

16. **`tests/test_review_fixes.py`** — each test pins one adversarial-review
    finding (orphan tool_call, atomic persistence, cache-key, xAI routing…). The
    best tour of the subtle failure modes we designed against.

17. Skim the rest (`test_types`, `test_store`, `test_search`, `test_renderer`,
    `test_adapter`, `test_engine`) as reference for how each unit behaves.

---

## Reading tips

- The **decision ids** (D1–D30, O6.x) in comments cross-reference `README.md` —
  when a comment says "(D24)", that's where the *why* lives.
- The **two seams** (`adapter.py`, `providers.py`) are the only `pydantic_ai`
  importers. If you understand those two files, you understand exactly how
  swappable the SDK is.
- If a single concept is worth pausing on: the **two-lane rendering invariant**
  (types.py docstring + adapter's `to_message_history`). It's the idea the whole
  provider-independence claim rests on.
