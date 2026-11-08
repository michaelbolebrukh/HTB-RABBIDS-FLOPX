# Phase 5 — Agent Tools: RAG + Native-Tool Fills: Full Development Spec

Status: BUILT (July 2026). Companion to `README.md` (D1–D35).
Agents stop being chat-only: they gain tools, with the registry deciding
native-vs-ours per model (D15), and requirement 7 completes — RAG over the
full conversation even after summarization.

## Goals / non-goals

**Goals**: a tool table (code-registered implementations, spec-selected per
agent); deps-injected tool execution (D11 trust boundary); the
`search_conversation` RAG tool live end-to-end (embedder wired, background
backfill); native capabilities (WebSearch/WebFetch/CodeExecution) enabled per
registry flags with our client-side fills as fallback under the SAME tool
name; MCP servers as tools (D14); registry-driven structured-output mode
selection (closing D20); tool-cost attribution.

**Non-goals (later)**: computer use, image generation, voice (P6);
human-in-the-loop tool approval, deferred/external tools (post-P6 gap list);
cross-conversation RAG.

## Design decisions (proposed here, confirmed by building)

**D33 — Tool registration is CODE; tool selection is DATA.** Implementations
register at import/startup via `register_tool(...)` (platform tools here,
product tools from the product boundary). `AgentSpec.tools` carries NAMES
only. Rationale: the trust boundary stays in code (a spec — data — can never
smuggle in an implementation), while per-agent composition stays declarative
(req. 8). A spec naming an unregistered tool fails agent construction loudly.

**D34 — One tool name, two implementations, registry decides.** For features
providers offer natively (web search/fetch/code execution): if the run's model
has the native capability (registry flag), the adapter enables the provider's
native capability; otherwise the engine injects OUR client-side fill as an
ordinary function tool with the same name and contract. Agents and transcripts
are indifferent to which ran (`meta.implementation = "native" | "ours"` on the
tool items for analytics).

**D35 — Embeddings run in the background, never on the hot path.** After a
run persists its items, the engine schedules embedding backfill as a detached
task with a FRESH session (never the request's); search degrades gracefully to
text matching until vectors exist. Embedder = OpenAI embeddings behind the P1
`EmbedderProtocol`, constructed in the adapter (sanctioned file), model
`openai:text-embedding-3-small` (registry-priced; spend recorded
`kind="embedding"`).

## Module layout (additions/changes)

```
generative_ai_service/
  engine/
    tool_table.py      # register_tool / resolve_tools / the global table (NO pydantic_ai)
    adapter.py         # + bind resolved tools into Agent(tools=[...]);
                       # + make_embedder(); + native-capability selection (D34);
                       # + output-mode selection per registry (D20)
    engine.py          # + tool resolution per run; + background embed scheduling (D35)
    types.py           # + ToolBinding, ToolContext; AgentFeatures.rag_search wired for real
  platform_tools.py    # built-in implementations: search_conversation, web_fetch fill
  billing/             # (unchanged) — embedding + web-search-per-call spend entries
```

## Interfaces (normative)

```python
# engine/tool_table.py — the trust boundary in code (D33):
@dataclass
class ToolContext:
    """What every tool implementation receives (never model-controlled, D11)."""
    db: Session                    # fresh-session discipline documented per call path
    owner_id: str
    conversation_id: UUID
    features: AgentFeatures        # e.g. rag_scope_all_branches (D30)
    extra: Any                     # product extension slot (from RunDeps)

ToolFn = Callable[..., Awaitable[Any]]   # async def fn(ctx: ToolContext, **model_args)

def register_tool(name: str, fn: ToolFn, *, description: str,
                  parameters_schema: dict | None = None) -> None:
    """Registering an implementation (import-time; duplicate names raise)."""

def resolve_tools(names: list[str]) -> list[RegisteredTool]:
    """Resolving spec names -> registered implementations; unknown name raises
    ToolNotRegisteredError at agent construction (loud, per D33)."""


# platform_tools.py — first residents:
async def search_conversation(ctx: ToolContext, query: str, k: int = 8) -> list[dict]:
    """THE req.-7 tool: searching this conversation's full transcript (semantic
    when vectors exist, text otherwise). Scope: active path unless the agent
    was granted rag_scope_all_branches (D30). Returns compact item digests
    (role, kind, text excerpt, timestamp) — never raw items."""

async def web_fetch(ctx: ToolContext, url: str) -> str:
    """Our client-side fill for providers without native WebFetch: httpx GET
    with timeouts, size cap, and text extraction. Same name/contract as the
    native capability (D34)."""


# engine/adapter.py additions:
def bind_tools(resolved: list[RegisteredTool], ctx: ToolContext) -> list:  # -> pydantic_ai Tools
    """Wrapping each implementation so the model sees (name, description,
    parameters) while ctx is injected by us — model args never reach ctx (D11)."""

def make_embedder() -> EmbedderProtocol:
    """OpenAI embeddings behind the P1 protocol (sanctioned pydantic_ai/SDK spot)."""

def select_native_capabilities(spec, info: ModelInfo) -> list:   # D34 native side
def choose_output_mode(schema: dict, info: ModelInfo):           # D20: Native vs Tool vs Prompted
```

## Engine flow changes (per run)

1. Resolve `spec.tools` (+ implicit `search_conversation` when
   `features.rag_search`) via the tool table; build `ToolContext` from RunDeps.
2. D34 split: for web_search/web_fetch — registry says native? -> adapter
   enables the native capability; else -> inject our fill as a function tool.
3. Run as today; tool calls/results already persist as items (P2) — now they
   carry `meta.implementation`.
4. Post-persist: schedule background embedding backfill for the conversation
   (fresh session; failures log warning, never affect the run) + record
   embedding spend.
5. Web-search-per-call costs (registry `cost.web_search_per_call`) added to the
   run's spend event when the native tool ran.

## Structured-output mode (closing D20)

`choose_output_mode`: `capabilities.structured_output` (native JSON schema
supported) -> NativeOutput; else tool-capable -> default ToolOutput; else
PromptedOutput. Selected at agent construction per run model; golden tests per
branch.

## Security notes (tools are the new attack surface)

- `web_fetch`: block private/link-local address ranges (SSRF guard), cap
  response size, strip scripts — a tool fetching attacker-controlled URLs must
  not become a proxy into our network.
- Tool results are untrusted model input, not instructions — already true via
  the transcript design; documented on ToolContext.
- `search_conversation` returns digests, never `raw` payloads (no
  provider-internal leakage into model-visible text).

## Tests (offline; FunctionModel/TestModel drive tool calls)

- Tool table: register/duplicate/unknown-name; spec->resolution; ctx injection
  (model args cannot override ctx fields).
- search_conversation: text + semantic paths, D30 scoping, digest shape.
- web_fetch fill: SSRF guard, size cap (httpx mocked).
- D34 selection: native chosen when registry flag set; fill injected when not;
  same tool name either way; `meta.implementation` recorded.
- Embedder backfill: background task embeds with a fresh session; failure does
  not affect the run; embedding spend recorded.
- Output-mode selection: three golden branches.
- End-to-end: FunctionModel calls search_conversation and answers from an
  earlier (summarized-away) turn — the req.-7 acceptance test.

## Build order

1. `tool_table.py` + ToolContext + tests (pure, no SDK).
2. `platform_tools.py` search_conversation (text path) + adapter `bind_tools`
   — first end-to-end tool run.
3. Embedder + background backfill + semantic path (D35).
4. D34 native/fill selection + web_fetch fill + WebSearch native wiring.
5. Output-mode selection (D20) + MCP capability pass-through.
6. Spend attribution for tools/embeddings; FEATURE_COVERAGE ticks; docs.

## Done criteria

An agent with `tools=["search_conversation"]` answers a question about
summarized-away content by calling the tool (acceptance test); a model without
native web search transparently uses our fill under the same name; embeddings
backfill in the background and semantic search returns ranked results;
unknown tool names fail construction loudly; every tool/embedding cost lands
in the ledger; facade boundary still holds (tool_table has zero pydantic_ai).
