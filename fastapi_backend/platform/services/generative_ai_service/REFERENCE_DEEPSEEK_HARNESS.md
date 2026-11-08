# Reference — DeepSeek Harness (surveyed 2026-08-18)

Not a dependency, not a plan. A reference note for whoever opens Phase 3
("own runtime on the in-platform pydantic-ai base", planning/orchestrator-blueprint.md)
so the survey is not re-run from scratch. Re-verify before relying on it — the
project was five days old and pre-1.0 when this was written.

## What it is

`dsh` — https://github.com/deepseek-ai/deepseek-harness — MIT, TypeScript,
released 2026-08-13, `v0.1.0-rc.7` at survey time, ~150k stars. An agent harness
built on the Cordis plugin kernel (https://github.com/cordiverse/cordis) where
the model adapter, tool registry, session log, and the agent loop itself are all
plugins, composed at boot from ordered profile → bundle → patch layers. Read
`docs/architecture.md` first, then the ~35 files under `docs/subsystems/`.

## Why it is worth reading here

It arrives at our P1–P10 spine independently: an append-only session log from
which message history is *derived* (our D4/D24), compaction as a seam with a
summarizing provider and a model-free tool-result pruner (D25/D60), fail-closed
approval outcomes (D47/D48), credentials as references resolved at the operation
boundary (D50), MCP and skills as provider-neutral catalogs (D52/D53). Treat the
convergence as corroboration of those decisions, not as a reason to change them.

## The three patterns to study when Phase 3 opens

1. **Tool-output spill** (`packages/spill/`, `docs/subsystems/spill.md`).
   Oversized tool output is persisted and the inline result replaced by a bounded
   preview plus a retrieval locator. We truncate instead (`engine/renderer.py`,
   the `…[truncated]` path), which destroys the bytes irrecoverably. This is the
   highest-value, lowest-cost thing on the list.
2. **Loop-hygiene guards** (`packages/guard/`). Advisory repeat-tool-call
   reminders injected as additional context on the post-execute decision, and
   per-call tool deadlines armed as deployment policy rather than in tool code.
   The engine has no equivalent today.
3. **Background-job seam** (`packages/jobs/`, `docs/subsystems/jobs.md`). One
   owner-fenced protocol — observe, cancel, wait, completion notice — for every
   long-running tool, exposed to the model as tools. We have detached background
   work (D35) but no model-facing job contract.

Secondary: session projections (`docs/subsystems/session-projection.md`) as the
general form of our summary cache; replay-aware token metering
(`docs/subsystems/token-meter.md`) against D7's retry-token requirement; and the
sandbox's habit of *reporting* enforcement as `full` or `partial` with the
partial cases named (`docs/subsystems/sandbox.md`) — the runtime version of
test-integrity.mdc.

## Where it is NOT useful

Not adoptable: TypeScript, a plugin kernel we have no reason to take on, and the
facade rule (D3) exists precisely so our runtime does not inherit someone else's
breaking-change window. Its billing/budget story is thinner than ours (no hard
pre-flight enforcement of the D7 kind). It models no owner/tenant scoping (D54).
Its eval story is thin — `BENCHMARK.md` is a pointer to the Python SDK guide, not
a benchmark suite, so there is nothing here to inform our evals.

## For the agent-ops layer, separately

`packages/subagent/` ships working out-of-process children over ACP
(https://agentclientprotocol.com) plus real Codex app-server and Claude Code
(official Agent SDK) child drivers — prior art for the orchestrator blueprint's
session-driver protocol and its Phase-2 cross-provider routing, which the
blueprint flags as a load-bearing feasibility check. `.agents/notes/` is a
gate-enforced decision-record tree (lifecycle × class in the path, mandatory per
non-trivial PR, frozen archive with a hash manifest) worth reading before the
next revision of our planning/rules layer.
