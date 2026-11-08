# Roadmap — future phases (planning reference)

**Status: living planning doc, removable.** Captures WHAT each future phase does,
not how. Built phases (P1–P8) are recorded in `README.md` / `PHASE*.md`; this
file is only the forward plan and the shared vocabulary behind it. Order is a
recommendation, not a contract — reshuffle as priorities move.

Built so far (P1–P8): transcript store · engine facade over Pydantic AI ·
model registry · cost meter + budgets · RAG tool + client-side native-tool
fills · voice + sandbox + image · output-modality supplementation ·
HITL tool approval. The frontier scan (late-2025/2026) that informed the plan
below is summarized inline where it changed a decision.

---

## The organizing contract — "agent capability-improvement tools" (internal)

Every capability we add is one composable thing that improves task performance
by shaping *what enters, persists in, or is derived from the context window* —
without touching model weights. Each is fully specified by three axes, and the
middle axis is literally our three existing seams:

- **Family** (what it augments): Memory · Retrieval · Compression · Planning ·
  Reflection · Verification · Tool-use/Action · Isolation.
- **Attachment** (how it plugs in): a **Tool** (model decides when) · a **Hook**
  (fires at a lifecycle point) · a **Context-Processor** (deterministic
  transform on the message list — our renderer is this).
- **State locus**: in-context · file · vector · knowledge-graph · stateless.

Rule: every new capability declares its (Family × Attachment × State) and plugs
into exactly one seam. This keeps the layer composable as the frontier moves.

**Capability scouting (ongoing practice, not a phase):** use the taxonomy to
survey research-backed tools (e.g. claude-mem, mem0, Letta, Reflexion-style
critique, Anthropic memory tool + context editing) and ship the good ones as
**optional capabilities with no hard reliance** — each behind a per-agent toggle,
each swappable. We adopt patterns, never depend on one vendor's implementation.

---

## Phases

### P9 — Extensibility: MCP + Agent Skills — **BUILT** (July 2026, see PHASE9.md / D50–D54)
- Developer adds **MCP servers** and **Skills** to agents built on the service.
- MCP via the facade's existing capability, with proper trust/scoping (OAuth
  2.1, resource-scoped tokens, a dev-controlled allowlist — not "any URL").
- Skills as progressive-disclosure bundles; skill scripts execute in our
  sandbox with **network denied by default**.
- **User-added** MCP/Skills gated by a **developer-controlled allowlist**,
  scoped per-user × per-agent, new tools routed through P8 approval.
- Trust is two-axis (who added it × what it may do) — extends the access-control
  rule; "user is admin" never means "user's server is trusted."
- Reuses: tool table, P8, sandbox, feature toggles, access-control tiers.

### P10 — Memory & context capabilities — **BUILT** (July 2026, see PHASE10.md / D55–D58)
- **Agent-authored memory** — DONE, and it landed as a **bi-temporal fact store**
  rather than any of the earlier sketches (KV / globs / one-big-file /
  timestamp). The write path we lacked is now `memory_write`, and the key
  insight from the frontier code study was that memory's real hard problem is
  not storage shape but **write-time reconciliation** (supersede vs accrue) —
  which we ported from Graphiti's temporal arithmetic and proved against mem0
  (23/23 vs 16/23, BENCHMARK.md). Semantic-search-with-any-body-as-key, provenance
  back to transcript turns, and as-of time-travel all shipped.
- Still deferred to their own later slices (kept out of P10 scope deliberately):
  a **background reflection/consolidation** pass (LangMem-style), **context-editing**
  / compression-diffing (the "memory ≈ deeper context management" thread), a
  **structured scratchpad/plan** surviving compaction, and file-based memory once
  the P11 workspace filesystem exists.
- Reuses: tool table, adapter facade (injected resolver/embedder), billing
  ledger, portable DB types, transcript provenance ids.

### P11 — Containerized workspace + full agent execution + GitHub
- Upgrade the minimal Docker `code_execution` fill into a **real per-session
  isolated workspace**: default-deny egress proxy, per-session **scoped GitHub
  credential injected host-side** (never in the container), snapshot/resume.
- **Agentic codebase tools** (first-pass, no embedding index — the frontier
  moved off Cursor-style indexing because it's stale/privacy-heavy/costly to
  keep fresh): `glob` → `grep` → `read_file`, plus `ast_search` (ast-grep) and
  `repo_map` (tree-sitter tags → PageRank) and def/ref lookup. Semantic code
  search optional/last, reusing our embedder. ~70% reuses tool table + sandbox.
- **Provider-neutral action + consent + audit seam**: a per-action consent gate
  and an immutable action log in the transcript store, behind an `ActionTarget`
  abstraction — groundwork that computer-use (P12) and future user-screen
  control reuse by only swapping the execution target.
- Runtime pluggable behind the sandbox port (managed backend option; Docker as
  self-hosted fallback). The infra keystone the execution-heavy phases depend on.

### P12 — Computer use
- Two capabilities: `browser_control` (DOM/CDP — cheaper, more reliable) first,
  then `computer_use` (vision screenshot→action loop over the workspace display).
- Rendered to the user via **noVNC now**, WebRTC later, behind a render seam.
- Rides P11's ActionTarget/consent/audit. **User's own screen stays deferred** —
  its seam already exists from P11; only the execution target changes later.

### P13 — Harness integration (Cursor SDK / Claude Code / Codex)
- Model a coding-agent **harness as a second registry** distinct from the model
  registry: an async job contract (start / stream / result / cancel + webhook
  ingress), because a harness owns its own loop, tools, and sandbox.
- **Cost is reported, not computed** → pre-authorized caps + post-hoc
  reconciliation into the ledger. **Approval moves to the boundary** (approve the
  launch + repo scope; mirror emitted events for audit) since P8 can't see a
  harness's internal tool calls.
- In-process harness first (runs inside the P11 sandbox), remote black-box
  (task-in → PR-out) second.

### P14 (capstone / north star) — Agent collaboration & orchestration
- **Beyond the frontier's tree-of-agents delegation** — the endgame the whole
  platform is being perfected toward: sub-agents that genuinely **collaborate**,
  not just hand off. Exact behavior + the features layered on top come from the
  owner's manifesto; treated as its own deep design when we reach it.
- Everything above is foundation for this; it is sequenced last on purpose.
- Reuses everything: transcript store, tool table, budget ledger, toggles, the
  capabilities from P10, and the isolation primitive introduced here.

---

## Not phases

- **"Deep research"** — NOT a distinct architecture (the frontier scan
  confirmed: it's plan → fan-out → verify → synthesize, and token spend explains
  most of the quality). It ships as a **configured recipe** over the P14
  orchestration primitives (with a budget cap governing the ~15× spend), not as
  its own phase. A lightweight version can also be a skill.
- **Cursor-style codebase embedding index** — superseded by the P11 agentic
  tools; only revisit if real usage shows a fuzzy-recall gap grep/ast can't fill.

## Deferred (unchanged)

Prompt-injection defense (D49 research track) · intra-run durable execution ·
batch API · explicit cache breakpoints · logprobs / N-candidates · user-screen
*execution* (its seam is built in P11).
