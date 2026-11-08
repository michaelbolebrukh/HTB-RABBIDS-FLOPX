"""
AI service HTTP surface — minimal on purpose (Phase 2).

Two endpoints: create a conversation, and stream a run over SSE. The SSE
endpoint is the D29 proof — it is `async def`, streams our RunEvents as
server-sent events, and a client disconnect cancels the async generator, which
propagates into the model run and stops token spend.

Router stays thin (repo router rule): it builds RunDeps from the injected
session + authenticated user and delegates to the engine. The engine is the
business layer.
"""

from __future__ import annotations

import asyncio
import json
import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from infrastructure.database import SessionLocal
from infrastructure.dependencies import assert_session_live, db_dependency, get_user, user_dependency
from platform.services.auth_services.auth_service import is_session_live
from infrastructure.settings import settings
from platform.enums import TokenUse
from security import decode_token

from . import engine as _engine
from .billing import check_budget, record_spend, usage_summary
from .engine import AgentSpec, ApprovalDecision, RunDeps, UserInput, find_pending_approvals
from .engine.agent_registry import get_agent, register_agent
from .engine.types import (
    ApprovalResolutionError,
    BudgetCheckUnavailableError,
    BudgetExceededError,
    MediaModels,
)
from .extensions import add_user_extension, delete_user_extension, list_user_extensions
from .extensions.mcp_catalog import McpError
from .extensions.skill_catalog import SkillError
from .media import MediaError, MediaModelInvalidError, price_speech, synthesize_speech
from .exceptions import ConversationNotFoundError
from .models import AIConversation, AITranscriptItem
from .transcript import create_conversation, get_conversation
from .transcript.types import Usage
from .voice import VoiceConfig, VoiceError, build_voice_session


logger = logging.getLogger(__name__)


# How long a voice socket may go without re-proving its session is still live.
# A code default, not an env var (config-minimalism): nothing about it is
# per-instance. It is the ceiling on how long a revoked session can keep
# streaming, traded against one indexed lookup per interval - checking every
# audio frame would be a query per packet.
_WS_AUTH_RECHECK_SECONDS = 20.0


router = APIRouter(prefix="/ai", tags=["AI"])


# A platform default agent for QUANTSOC — one general assistant. Product agents
# come from the product boundary later; specs live as data (D19):
_DEFAULT_AGENT = AgentSpec(
    name="assistant",
    model="openai:gpt-5.2",
    instructions="You are a helpful assistant. Be concise and accurate.",
)

# Registering the default agent so the extension endpoints can resolve it (and
# its BYO toggles) by name (P9); product agents register the same way:
register_agent(_DEFAULT_AGENT)


# The ONE refusal a caller sees for a conversation they cannot reach. An id
# that does not exist and an id owned by somebody else return this identical
# 404, so the endpoint is never an existence oracle:
_CONVERSATION_NOT_FOUND = "We couldn't find that conversation."


def _require_owned_conversation(db, conversation_id: UUID, owner_id: str) -> None:
    """
    Resolving a conversation the caller owns, or refusing with the single
    shared 404. The store is owner-scoped by construction (`get_conversation`
    takes a required owner); this is the HTTP translation of that refusal, run
    BEFORE any stream opens so the client gets a clean status rather than a
    terminal event mid-SSE:
    """
    try:
        get_conversation(db, conversation_id, owner_id=owner_id)
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_CONVERSATION_NOT_FOUND) from exc


class CreateConversationRequest(BaseModel):
    title: str | None = None


class CreateConversationResponse(BaseModel):
    conversation_id: UUID


class RunRequest(BaseModel):
    """A single user turn plus an optional model override (mid-conversation switching)."""
    text: str
    model: str | None = None               # e.g. "anthropic:claude-sonnet-4-6" to switch this turn


@router.post("/conversations", response_model=CreateConversationResponse)
def create_conversation_endpoint(
    body: CreateConversationRequest,
    db: db_dependency,
    user: user_dependency,
):
    """Creating a conversation owned by the authenticated user (owner_id = str(user.id), D26):"""
    conversation = create_conversation(db, owner_id=str(user.id), title=body.title)
    return CreateConversationResponse(conversation_id=conversation.id)


@router.post("/conversations/{conversation_id}/runs")
async def stream_run_endpoint(
    conversation_id: UUID,
    body: RunRequest,
    db: db_dependency,
    user: user_dependency,
):
    """
    Streaming a run as server-sent events. Each SSE `data:` line is one of our
    RunEvents serialized to JSON; the stream ends after `run_completed` or
    `run_failed`. Disconnect cancels the run (D29):
    """
    # Ownership FIRST — before any spend, any budget read, any transcript write.
    # Run off the event loop (asyncio.to_thread): this is a blocking DB call in
    # an async handler, and the request's own coroutine is the only thing
    # touching `db` while it awaits, so there is no concurrent use of the
    # Session:
    await asyncio.to_thread(_require_owned_conversation, db, conversation_id, str(user.id))

    # Selecting the agent spec, applying a per-turn model override if given:
    spec = _DEFAULT_AGENT
    if body.model:
        spec = _DEFAULT_AGENT.model_copy(update={"model": body.model})

    # session_factory powers the D35 background embedding backfill with a
    # fresh session (the request's session dies with the response):
    deps = RunDeps(
        db=db, owner_id=str(user.id), conversation_id=conversation_id,
        session_factory=SessionLocal,
    )

    # Pre-flight budget check at the HTTP boundary (Phase 4) so an exhausted
    # budget gets a clean 429 BEFORE the SSE stream opens. The engine repeats
    # the check as defense (a race between the two just ends the stream early).
    # Fail-CLOSED: an unverifiable budget blocks rather than spends (PHASE4).
    # Off the event loop for the same reason as the ownership check above:
    if settings.AI_BUDGET_ENFORCEMENT:
        try:
            decision = await asyncio.to_thread(
                check_budget,
                db, owner_id=str(user.id), conversation_id=conversation_id, agent_name=spec.name,
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail="We couldn't verify your AI usage limit right now. Try again in a moment.") from exc
        if not decision.allowed:
            raise HTTPException(
                status_code=429,
                detail="You've reached your AI usage limit for now. Try again later.",
            )

    async def event_source():
        """Yielding engine RunEvents as Server-Sent Events for the streaming response."""
        # Yielding each RunEvent as an SSE message; sse-starlette detects client
        # disconnect and cancels this generator, unwinding the engine's run:
        async for event in _engine.stream_agent(spec, deps, UserInput(text=body.text)):
            yield {"event": event.type, "data": event.model_dump_json()}

    return EventSourceResponse(event_source())


class ApprovalDecisionRequest(BaseModel):
    """One resolution in an approvals payload (P8): approve/deny one paused call."""
    tool_call_id: str
    approved: bool
    reason: str | None = None
    override_args: dict | None = None


class ResumeRunRequest(BaseModel):
    """Resolving every tool call a run paused on (P8) and resuming the run."""
    decisions: list[ApprovalDecisionRequest]
    model: str | None = None               # Same per-turn model override as a fresh run


@router.get("/conversations/{conversation_id}/pending-approvals")
def pending_approvals_endpoint(
    conversation_id: UUID,
    db: db_dependency,
    user: user_dependency,
):
    """
    Listing the tool calls awaiting the caller's approval on this conversation
    (P8): what a paused run is blocked on, so a UI can render the decision
    prompt. 409 when nothing is pending; 404 when the conversation is unknown
    OR not theirs — the same refusal for both, so this is not an existence
    oracle:
    """
    _require_owned_conversation(db, conversation_id, str(user.id))
    deps = RunDeps(db=db, owner_id=str(user.id), conversation_id=conversation_id)
    try:
        pending = find_pending_approvals(deps)
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_CONVERSATION_NOT_FOUND) from exc
    except ApprovalResolutionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"pending": [p.model_dump() for p in pending]}


@router.post("/conversations/{conversation_id}/runs/resume")
async def resume_run_endpoint(
    conversation_id: UUID,
    body: ResumeRunRequest,
    db: db_dependency,
    user: user_dependency,
):
    """
    Resuming a paused run with the caller's approve/deny decisions (P8, D47/D48),
    streaming the continuation as SSE exactly like a fresh run. Pre-validated
    synchronously so a bad decision set is a clean 409 before the stream opens;
    approved calls execute (with edited args), denials become the tool result
    the model reads:
    """
    # Ownership FIRST, exactly as on a fresh run. Off the event loop, same
    # reasoning as stream_run_endpoint above:
    await asyncio.to_thread(_require_owned_conversation, db, conversation_id, str(user.id))

    spec = _DEFAULT_AGENT
    if body.model:
        spec = _DEFAULT_AGENT.model_copy(update={"model": body.model})
    deps = RunDeps(
        db=db, owner_id=str(user.id), conversation_id=conversation_id,
        session_factory=SessionLocal,
    )

    # Pre-check the pending set (clean 409) BEFORE opening the stream, mirroring
    # the run endpoint's budget pre-flight. Off the event loop for the same
    # reason:
    try:
        await asyncio.to_thread(find_pending_approvals, deps)
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_CONVERSATION_NOT_FOUND) from exc
    except ApprovalResolutionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    decisions = [
        ApprovalDecision(
            tool_call_id=d.tool_call_id, approved=d.approved,
            reason=d.reason, override_args=d.override_args,
        )
        for d in body.decisions
    ]

    async def event_source():
        """Yielding the resumed run's RunEvents as Server-Sent Events."""
        async for event in _engine.resume_agent(spec, deps, decisions):
            yield {"event": event.type, "data": event.model_dump_json()}

    return EventSourceResponse(event_source())


# WebSocket close codes for voice sessions (4000-range = application-defined):
_WS_UNAUTHORIZED = 4401
_WS_NOT_FOUND = 4404
_WS_BUDGET_EXHAUSTED = 4429
_WS_UNAVAILABLE = 4503


@router.websocket("/conversations/{conversation_id}/voice")
async def voice_ws_endpoint(
    websocket: WebSocket,
    conversation_id: UUID,
    token: str | None = None,
    model: str | None = None,
):
    """
    THE voice surface (Phase 6, D37): one authenticated WebSocket per session.
    Client frames up: {"type":"audio","audio_b64":...} | {"type":"end_turn"} |
    {"type":"close"}. Our VoiceEvents stream down as JSON frames until a
    terminal voice_session_ended / voice_failed. The registry decides whether
    a native realtime adapter or the pipeline serves — the client cannot tell.

    Auth: the access token rides the `token` query param (browsers cannot set
    WS headers) or an Authorization header; rejected BEFORE accept:
    """
    # A WebSocket outlives any request scope, so it owns its session directly
    # (closed in the finally, mirroring get_db's lifecycle):
    db = SessionLocal()
    session = None
    downlink_task: asyncio.Task | None = None
    watchdog_task: asyncio.Task | None = None
    try:
        raw_token = token or (websocket.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        try:
            payload = decode_token(raw_token, expected_use=TokenUse.ACCESS)
            user_id = int(payload.get("sub"))
            # This is the one auth path that does not resolve through
            # get_current_user, so the session check has to be made explicitly:
            # a signed signature is not enough, the session behind it must still
            # be live. Without this a token lifted from a signed-out tab would
            # open a long-lived, budget-spending socket that then outlives any
            # revocation entirely, because nothing re-checks after accept.
            # Both are blocking DB calls; off the event loop (asyncio.to_thread).
            # Run sequentially, before anything else touches `db`, so there is
            # no concurrent use of the Session:
            await asyncio.to_thread(assert_session_live, db, payload.get("sid"), user_id)
            user = await asyncio.to_thread(get_user, db, user_id)
        except (HTTPException, TypeError, ValueError):
            # Every auth refusal is the same 4401: a bad signature, a malformed
            # claim, a dead session and an unknown user must not be
            # distinguishable. (Our auth exceptions are all HTTPException
            # subclasses; the two builtins are a non-integer `sub`.)
            await websocket.close(code=_WS_UNAUTHORIZED)
            return
        except Exception:  # noqa: BLE001 — same 4401, but this one is a real fault:
            # A database outage reaches here too, and closing the socket without
            # a word would make it indistinguishable from a bad token - no log,
            # no error event, nothing to page on (see the observability rule).
            logger.exception("voice websocket auth failed unexpectedly")
            await websocket.close(code=_WS_UNAUTHORIZED)
            return

        # Ownership BEFORE accept, and before any session or spend: an unknown
        # conversation and another owner's conversation close identically, so
        # the socket is not an existence oracle either:
        try:
            # Off the event loop, same reasoning as the auth calls above — still
            # sequential on the main coroutine, still the only use of `db` so far:
            await asyncio.to_thread(get_conversation, db, conversation_id, owner_id=str(user.id))
        except ConversationNotFoundError:
            await websocket.close(code=_WS_NOT_FOUND)
            return

        await websocket.accept()

        # The same default agent as the SSE surface, with the same per-session
        # model override (mid-conversation switching applies to voice too):
        spec = _DEFAULT_AGENT if not model else _DEFAULT_AGENT.model_copy(update={"model": model})
        deps = RunDeps(
            db=db, owner_id=str(user.id), conversation_id=conversation_id,
            session_factory=SessionLocal,
        )

        try:
            session = await build_voice_session(VoiceConfig(spec=spec), deps)
            await session.start()
        except BudgetExceededError:
            await websocket.close(code=_WS_BUDGET_EXHAUSTED, reason="AI budget exhausted")
            return
        except (BudgetCheckUnavailableError, VoiceError) as exc:
            await websocket.close(code=_WS_UNAVAILABLE, reason=str(exc)[:120])
            return

        async def downlink():
            """Relaying the session's VoiceEvents down until the terminal event:"""
            try:
                async for event in session.events():
                    await websocket.send_text(event.model_dump_json())
            except Exception:  # noqa: BLE001 — a dead client socket just ends the relay:
                pass

        downlink_task = asyncio.create_task(downlink())

        session_id = int(payload["sid"])

        def _watchdog_check_session_live() -> bool:
            """
            Opening, querying, and closing a session ENTIRELY inside this
            function — which asyncio.to_thread runs start-to-finish on ONE
            worker thread. Two things that matter follow from that:

            1. No connection is held between polls. An earlier version opened
               one SessionLocal() for the whole socket lifetime; on top of the
               handler's own `db`, that doubled per-socket pooled-connection
               consumption for the entire call (this service sets no
               pool_size/max_overflow, so the default 5+10 per worker is all
               there is) — with enough concurrent voice calls, unrelated
               requests start queuing on pool checkout. Opening fresh per poll
               means the connection is held for milliseconds every
               _WS_AUTH_RECHECK_SECONDS instead of for the whole call.
            2. No cross-thread close. asyncio.to_thread does not pin a task to
               the same worker thread across separate calls, so a session
               created on one to_thread call and closed via another risked the
               DBAPI connection being created on one thread and torn down on
               another. Creating, using, and closing inside the SAME to_thread
               callable keeps the whole lifecycle on one thread.
            """
            watchdog_db = SessionLocal()
            try:
                return is_session_live(watchdog_db, session_id, user_id)
            finally:
                watchdog_db.close()

        async def auth_watchdog():
            """Re-checking the session on a clock, independently of the client.

            A socket must not outrun the sign-out that should end it, and this
            is the half of that guarantee the caller cannot influence. Anything
            placed in the uplink loop only runs when a frame arrives, which
            makes it a check on the CLIENT's schedule: send one burst, go quiet,
            and it never runs again - while the provider keeps generating, the
            downlink keeps relaying, and a per-minute-billed native session
            keeps spending, all from their own tasks. Closing the socket here
            unblocks the uplink loop's receive too.

            This is the highest-value async-safety fix in this file: with N
            sockets open, this loop makes N blocking DB round-trips every
            _WS_AUTH_RECHECK_SECONDS, for the lifetime of every voice call.
            Moved off the event loop via asyncio.to_thread, through
            `_watchdog_check_session_live` above rather than reusing the
            handler's `db` directly: a Session is not safe for concurrent use,
            and this task runs concurrently — on the wall clock, in a real OS
            thread once it's in `to_thread` — with the uplink loop's own use
            of `db` (e.g. its own `is_session_live` call on `end_turn`, and
            whatever `session.*` does with `deps.db`). A session that never
            leaves this task's control removes the sharing entirely rather
            than trying to prove those overlaps can never happen.
            """
            while True:
                await asyncio.sleep(_WS_AUTH_RECHECK_SECONDS)
                if not await asyncio.to_thread(_watchdog_check_session_live):
                    logger.info("voice websocket: session no longer live; closing mid-call")
                    await websocket.close(code=_WS_UNAUTHORIZED, reason="Session ended")
                    return

        watchdog_task = asyncio.create_task(auth_watchdog())

        # The uplink loop — the client drives; disconnect ends the session (and
        # with it token spend), exactly like SSE cancellation does for runs:
        while True:
            message = await websocket.receive_json()
            message_type = message.get("type")

            if message_type == "audio":
                await session.send_audio(message.get("audio_b64", ""))
            elif message_type == "end_turn":
                # Also on the turn boundary, because that is where the spend
                # lands - a turn should not start on a session that has just
                # died, rather than waiting out the watchdog interval. Off the
                # event loop; this coroutine is the only thing using `db` at
                # this point (the watchdog now has its own session), so no
                # concurrent Session use:
                if not await asyncio.to_thread(is_session_live, db, session_id, user_id):
                    await websocket.close(code=_WS_UNAUTHORIZED, reason="Session ended")
                    break
                await session.end_turn()
            elif message_type == "close":
                break

    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 — a voice session dying is logged, never a 500 page:
        logger.exception("voice websocket failed")
    finally:
        if watchdog_task is not None:
            watchdog_task.cancel()
            try:
                await watchdog_task
            except asyncio.CancelledError:
                pass
        if session is not None:
            await session.close()          # Emits the terminal event (idempotent)
        if downlink_task is not None:
            await downlink_task            # Drains it to the client if still connected
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001 — already closed is fine:
            pass
        db.close()


# Ceiling on CALLER-SUPPLIED read-aloud text. TTS is a paid, per-character
# lane, so unbounded `text` is a billing-amplification vector on an
# authenticated endpoint (one request can buy an arbitrary spend). Well above
# any real "read this message aloud" body and above every provider's own
# per-request limit. Text resolved from an `item_id` is our own transcript
# content and is not capped here:
_MAX_SPEECH_CHARS = 10_000


class SpeechRequest(BaseModel):
    """
    Read-aloud input (P7): either raw `text` or an `item_id` pointing at a
    transcript message the caller owns. Model/voice default to the platform
    TTS choices (D42) unless overridden per request:
    """
    text: str | None = None
    item_id: UUID | None = None
    tts_model: str | None = None
    voice: str | None = None


@router.post("/speech")
async def speech_endpoint(body: SpeechRequest, db: db_dependency, user: user_dependency):
    """
    THE read-aloud surface (P7, D41): synthesizing speech for a message (the
    textual UI's "read out loud" button) or arbitrary caller text, through the
    pickable TTS lane. Returns raw audio; the spend lands as `kind="voice"`:
    """
    from fastapi import Response

    # Resolving the text — an item id must belong to a conversation the
    # caller owns (never speak someone else's messages):
    text = body.text
    if body.item_id is None and text is not None and len(text) > _MAX_SPEECH_CHARS:
        raise HTTPException(
            status_code=422,
            detail=f"That text is too long to read aloud. Keep it under {_MAX_SPEECH_CHARS} characters.",
        )
    if body.item_id is not None:
        def _load_item_text() -> str | None:
            row = (
                db.query(AITranscriptItem)
                .join(AIConversation, AITranscriptItem.conversation_id == AIConversation.id)
                .filter(AITranscriptItem.id == body.item_id, AIConversation.owner_id == str(user.id))
                .first()
            )
            return row.text_content if row else None
        text = await asyncio.to_thread(_load_item_text)
        if text is None:
            raise HTTPException(status_code=404, detail="We couldn't find that message.")
    if not text or not text.strip():
        raise HTTPException(status_code=422, detail="There's nothing to read aloud. Add some text or pick a message first.")

    models = MediaModels(tts_model=body.tts_model, tts_voice=body.voice or "default")
    try:
        result = await synthesize_speech(text, models)
    except MediaModelInvalidError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MediaError as exc:
        raise HTTPException(status_code=502, detail="We couldn't generate the audio. Try again in a moment.") from exc

    # Billing the synthesis (D44) — a ledger failure never fails the audio:
    try:
        price = price_speech(result)
        if price.cost_microusd is not None:
            await asyncio.to_thread(
                record_spend, db,
                owner_id=str(user.id), model_id=result.model_id,
                usage=result.usage or Usage(), price=price, kind="voice",
            )
    except Exception:  # noqa: BLE001 — deliberate: delivered audio is not failed over ledger writes:
        logger.exception("read-aloud spend recording failed")

    return Response(content=result.audio, media_type=result.mime_type)


class AddExtensionRequest(BaseModel):
    """
    A user attaching their OWN MCP server or Skill to an agent (P9, D51). MCP is
    remote-only (url + optional headers/token — token-paste, no OAuth client);
    a skill is its SKILL.md text plus optional text files:
    """
    kind: str                              # "mcp" | "skill"
    url: str | None = None
    headers: dict[str, str] | None = None
    skill_md: str | None = None
    files: dict[str, str] | None = None


@router.get("/agents/{agent_name}/extensions")
def list_extensions_endpoint(agent_name: str, db: db_dependency, user: user_dependency):
    """
    Listing the caller's own extensions for an agent, plus whether this agent
    lets users add their own (the developer-controlled toggles, P9):
    """
    spec = get_agent(agent_name)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"We couldn't find an agent named '{agent_name}'.")
    rows = list_user_extensions(db, str(user.id), agent_name)
    return {
        "allow_user_mcp": spec.allow_user_mcp,
        "allow_user_skills": spec.allow_user_skills,
        "extensions": [
            {"id": str(r.id), "kind": r.kind, "created_at": r.created_at.isoformat()} for r in rows
        ],
    }


@router.post("/agents/{agent_name}/extensions")
def add_extension_endpoint(
    agent_name: str, body: AddExtensionRequest, db: db_dependency, user: user_dependency,
):
    """
    Adding one of the caller's OWN extensions to an agent — only when that
    agent's developer enabled the matching toggle (else 403); the config is
    validated (remote-only + SSRF for MCP) before it is stored (else 422):
    """
    spec = get_agent(agent_name)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"We couldn't find an agent named '{agent_name}'.")
    if body.kind == "mcp" and not spec.allow_user_mcp:
        raise HTTPException(status_code=403, detail="This agent does not allow user-added MCP servers")
    if body.kind == "skill" and not spec.allow_user_skills:
        raise HTTPException(status_code=403, detail="This agent does not allow user-added skills")

    config = (
        {"url": body.url, "headers": body.headers} if body.kind == "mcp"
        else {"skill_md": body.skill_md, "files": body.files}
    )
    try:
        row = add_user_extension(db, str(user.id), agent_name, body.kind, config)
    except (McpError, SkillError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"id": str(row.id), "kind": row.kind}


@router.delete("/agents/{agent_name}/extensions/{ext_id}")
def delete_extension_endpoint(
    agent_name: str, ext_id: UUID, db: db_dependency, user: user_dependency,
):
    """Removing one of the caller's own extensions (never another user's, D54):"""
    if not delete_user_extension(db, str(user.id), ext_id):
        raise HTTPException(status_code=404, detail="We couldn't find that extension.")
    return {"deleted": True}


class UsageResponse(BaseModel):
    """Owner-scoped spend summary (USD floats at the API boundary only)."""
    today_usd: float
    month_usd: float
    total_usd: float
    month_by_model: dict[str, float]


@router.get("/usage", response_model=UsageResponse)
def usage_endpoint(db: db_dependency, user: user_dependency):
    """Returning the authenticated owner's AI spend summary from the ledger (Phase 4):"""
    return UsageResponse(**usage_summary(db, owner_id=str(user.id)))
