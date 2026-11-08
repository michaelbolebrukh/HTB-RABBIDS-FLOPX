# Phase 9 — Extensibility: MCP + Skills: Full Development Spec

Status: BUILT (July 2026). Companion to `README.md` (D1–D54) and `ROADMAP.md`.
Owner decisions folded in during design: the enable flag is **per-agent** (a
spec field), two independent toggles (`allow_user_mcp` / `allow_user_skills`);
**no allowlist and no tenant/product config** — the developer of the user-facing
product flips a toggle and accepts the risk-class, and the AI service scopes by
`owner_id` only (D54); user MCP is **remote-only, token-paste** (no OAuth
client — the product owns any OAuth and hands us a token); **skills are
containerized**; injection defense stays the D49 research track.

The goal: a developer attaches MCP servers (external tools/data) and Skills
(procedural-knowledge bundles) to their agents, and — when they enable it —
their end-users bring their own, gated only by the per-agent toggle.

## Decisions (D50–D54, see README for the canonical rows)

- **D50** — Extension catalog: dev-owned, code-registered by id; specs reference
  the id, never inline config or secrets (mirrors the tool table, D33).
- **D51** — Per-agent BYO toggles; user-added extensions stored per `owner_id`,
  no allowlist, loaded only when the toggle is on.
- **D52** — MCP via the official `mcp` SDK bridged into the tool table (NOT
  pydantic-ai's MCP capability — see the dependency note); remote-only +
  token-paste for user-added; results are untrusted data.
- **D53** — Skills are containerized: bundle in the sandbox, L1 metadata in
  context, L2 body via `open_skill`, L3 scripts via `code_execution`;
  network-off in v1 (inherits the P11 workspace egress policy later).
- **D54** — The AI service is the product, not a tenancy platform: no
  tenant/product config; scope by `owner_id`; cross-cutting concerns belong to
  the host platform.

## Dependency note (why the `mcp` SDK, not pydantic-ai's MCP capability)

pydantic-ai 2.5's MCP capability requires `fastmcp-slim`, which requires
`starlette >= 1.0.1` — a hard conflict with FastAPI 0.115 (`starlette < 0.46`;
starlette 1.x removed the `on_startup` Router kwarg FastAPI 0.115 uses, so the
app won't even import). The lower-level official `mcp` SDK needs only
`starlette >= 0.27`, coexists with our web stack, and bridging its tools into
our own tool table is the cleaner fit anyway — MCP tools become ordinary fills
and inherit P8 approval, billing, and offline-testability. `extensions/` is the
sole `mcp`-importing seam (analogous to the voice/media adapters; the facade
rule D3 constrains `pydantic_ai`, and this is the same isolated-seam pattern).

## Architecture (rides the existing spine)

```
extensions/
  mcp_client.py     # the mcp-SDK seam: in-process (dev/test) + remote Streamable HTTP
  mcp_catalog.py    # dev registry + resolve + BRIDGE (build_mcp_tools -> RegisteredTool)
  skill_catalog.py  # dev registry + frontmatter + L1 preamble + open_skill tool
  store.py          # user-added CRUD (owner-scoped), validation, resolution
engine/agent_registry.py   # specs resolvable by name (for the endpoints)
```

- **MCP** (Slice 1): `resolve_mcp_configs(spec.mcp_servers)` (catalog id or raw
  URL back-compat) → `build_mcp_tools` lists each server's tools and wraps each
  as a per-run `RegisteredTool` whose schema is the MCP JSON schema and whose
  body calls the server through the seam. The adapter binds these via
  `Tool.from_schema` (they have a JSON schema, not a Python signature). Connect
  per call keeps it stateless. Namespaced `mcp__<server>__<tool>`.
- **Skills** (Slice 2): progressive disclosure — L1 metadata is an always-on
  instructions preamble; `open_skill(skill_id)` discloses the SKILL.md body +
  file list on demand; `ToolContext.skills` carries the bundles, which
  `code_execution` mounts at `/skills/<id>/` so scripts run there.
- **User-added** (Slice 3): `AIUserExtension` rows keyed by `(owner_id,
  agent_name)`; the engine merges the owner's rows into the MCP/skill sets only
  when the agent's toggle is on. `GET/POST/DELETE /ai/agents/{agent}/extensions`
  (403 when a toggle is off, 422 on invalid config, owner-scoped delete).

## Security posture

- User MCP is **remote-only** (no code on our infra) and passes the same **SSRF
  guard** `web_fetch` uses (no loopback/private/metadata targets) at add time.
- User skills run in the **sandbox** (the isolation boundary), network-off in v1.
- User-added tool/skill descriptions are **provenance-labeled** (`origin="user"`)
  so their text is never mistaken for a platform tool.
- MCP tool results (dev or user) are stored as **data items, never instructions**
  (D49 unchanged); whole-tool P8 approval on MCP tools is best-effort (the SDK's
  `from_schema` has no approval param — set post-hoc when supported).
- Injection defense remains the deferred **D49** research track.

## Tests (offline, in-process `mcp` server as the seam)

- `test_extensions_mcp.py` (5): bridge + call, `allowed_tools` least-privilege,
  unknown-id loud fail, URL back-compat, tool-error → model-retry.
- `test_extensions_skills.py` (7): frontmatter parse, L1 preamble, L1-in-
  instructions (body stays out), `open_skill` body+files, unattached-skill
  error, bundle mounts into `code_execution`, unknown-skill loud fail.
- `test_extensions_user.py` (7): remote-only + SSRF validation, skill
  validation, per-owner isolation, toggle-gated load (on/off), cross-owner
  non-leak, user MCP merge + provenance label.

Full service suite green: 231 passed / 1 skipped / 2 xfailed.

## Out of scope (deferred, per the plan)

OAuth 2.1 client · any allowlist · tenant/product config · user-added
stdio/local MCP · network-enabled skills (waits for the P11 workspace egress
policy) · binary skill bundles · per-tool P8 approval guaranteed on MCP tools.
