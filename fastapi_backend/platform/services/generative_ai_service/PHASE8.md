# Phase 8 — HITL Tool Approval (pause/resume tool loop): Full Development Spec

Status: BUILT (July 2026). Companion to `README.md` (D1–D49). Owner decisions
folded in during design: approval gates **only our client-executed tools** —
native tools are never forced into the fill lane (D46); durability stays
**turn-granular** (at most the in-flight tool call is at risk — accepted);
prompt-injection defense is a **separate research track (D49)**, explicitly not
built here; moderation is not a phase.

The driving observation: an agent that only produces text is safe to run
unattended, but the moment it can take a **consequential action** — delete a
file, send an email, spend money, open a PR — some calls must be gated on a
human (or policy) decision. The whole feature reduces to one mechanism:
**pause-able tool execution**. Because our tools are plain callables behind one
table and every tool call already carries a `tool_call_id` in the transcript,
"pause" is a transcript checkpoint and "resume" is an ordinary engine run over
that checkpoint — no new state store, no blocked worker, no durable-execution
engine.

## Goals / non-goals

**Goals**:
- **Optional, scoped approval**: dormant by default; scoped per-agent
  (`AgentSpec.requires_approval`), per-tool (names in that list), and
  per-invocation (a code-registered `approval_predicate` over the model-facing
  args — "only transfers above the threshold").
- **Only our tools are gateable (D46)**: a provider-native tool runs inside the
  provider and can never be intercepted, so it is structurally unreachable by
  the gate; a native name in the scope is inert (logged), never a forced lane.
- **Pause is a checkpoint, not a wait (D47)**: a gated call ends the run with
  `RunPaused` after persisting the pending `tool_call` item (no result), and
  the connection is released. No async task hangs.
- **Resume is an ordinary run (D47)**: approve / approve-with-edited-args / deny
  over the checkpointed transcript; all P2–P5 machinery (budget, branching,
  model-switch, fallback) applies, and a resumed run may complete, fail, or
  pause again.
- **Denial is data, not an error (D48)**: a denial becomes the tool result the
  model reads, so the agent adapts.

**Non-goals** (owner-decided):
- **Prompt-injection defense** (D49) — its own research track; the checkpoint
  here is a hook it will build on, but no trust marker / confinement / moderation
  ships in P8.
- **Intra-run exactly-once durability** — turn-granular resume only; a crash
  mid-tool-loop replays at most the in-flight call (accepted).
- **Deferred/external-tool result submission** — the SDK seam is the same, but a
  dedicated external-result tool flag is left for later.

## Architecture (rides existing invariants)

- **Tool table** (`engine/tool_table.py`) is the single interception point and
  is exactly the set D46 covers. `RegisteredTool` gains an optional
  `approval_predicate`; `AgentSpec` gains `requires_approval: list[str]`.
- **Adapter** (`engine/adapter.py`, the pydantic_ai facade) owns the SDK seam:
  - `bind_tools(..., requires_approval)` marks whole-tool-gated tools with the
    SDK's `Tool(requires_approval=True)` (the SDK defers them before executing);
    a per-invocation predicate uses the SDK `RunContext` to raise
    `ApprovalRequired` conditionally and to detect a resumed, already-approved
    call (`tool_call_approved`) so it doesn't re-pause.
  - `build_agent(allow_pause=True)` admits `DeferredToolRequests` into the output
    union so a gated call ends the run paused instead of erroring; agents with no
    approval surface keep the exact pre-P8 output contract.
  - `to_message_history(resume=...)` pairs a `pending`/`abandoned` tool_call with
    a synthetic "not executed" return on ordinary renders (never a bare call to a
    provider), and sends it bare on a resume render (the deferred results supply
    the return).
  - `make_deferred_results(decisions)` converts our decisions to the SDK payload
    (`ToolApproved(override_args=...)` / `ToolDenied(reason)`); the paused run's
    `DeferredToolRequests` is converted to our `PendingApproval` shapes so nothing
    above the adapter sees an SDK type (D3).
- **Engine** (`engine/engine.py`): `stream_agent` (fresh turn) and `resume_agent`
  (resume) are thin wrappers over one `_execute` core. The core stamps each
  newly-persisted gated call `meta.approval="pending"` and, on resume, flips the
  OLD pending markers to `"resolved"` in the SAME `append_items` commit as their
  arriving results — so a crash can never leave a duplicate result or a bare
  call. A fresh turn started instead of a decision flips stale pending calls to
  `"abandoned"`. `find_pending_approvals` is the synchronous pre-check the router
  runs for a clean 409.
- **Router** (`routers.py`): `GET /ai/conversations/{id}/pending-approvals`
  (what a paused run is blocked on) and `POST /ai/conversations/{id}/runs/resume`
  (stream the continuation as SSE, pre-validated to a 409).
- **Voice** (`voice/adapters/pipeline.py`): a live turn cannot wait for a UI, so
  a gated tool is auto-denied with a spoken-friendly reason and the run resumes
  until it settles (bounded guard against a deny/re-call loop).

## The pending lifecycle

```
tool_call.meta.approval:  (unset) ──gated call──▶ "pending"
                                                    │
                          new turn instead of a     │   resume decision
                          decision → "abandoned"    │   arrives
                                                    ▼
                                                "resolved"  (+ real tool_result)
```

`"pending"`/`"abandoned"` render with a synthetic return (provider never sees a
bare call); `"resolved"` renders as a normal completed pair. An abandoned call
can never be resumed — `find_pending_approvals` only sees calls still `"pending"`
on the current leaf.

## Persistence & durability

Persistence stays turn-granular and atomic (`append_items`, one commit): a
resolved run's pairs are complete, the pending call is committed WITHOUT a result
but explicitly flagged. On resume, the same commit that appends the results flips
the old markers to `"resolved"` — the one place both a duplicate (marker stays
pending beside a real result) and a bare call (marker resolved with no result)
are ruled out atomically. Beyond that, a crash loses at most the in-flight turn
(owner-accepted). No Temporal/DBOS; the checkpoint IS the turn-granular
durable-resume story.

## Tests (`tests/test_approvals.py`, 15 adversarial)

Pause-without-executing (side-effect flag proves the body never ran) · approve
executes + continues + flips to resolved · approve-with-edited-args runs the
edited call · deny becomes the tool result and the model adapts · D46 native
tools structurally ungateable (a native name in the scope marks zero tools) ·
inert native scope never pauses · per-invocation predicate gates only the
consequential call · a new turn abandons a pending call (and it can't be
resumed) · resume rejects a mismatched decision set · nothing-pending and
foreign-owner both raise the typed error · a paused run still bills its tokens ·
resume can pause again · two gated calls in one turn need both decisions
(partial rejected; approve-one/deny-one runs exactly the approved one) · a
pending call renders with a synthetic return (bare only on resume) · voice
pipeline auto-denies and still finishes the turn.

Full service suite green: 212 passed / 1 skipped / 2 xfailed.
