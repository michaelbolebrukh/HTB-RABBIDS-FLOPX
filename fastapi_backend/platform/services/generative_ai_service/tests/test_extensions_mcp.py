"""
MCP extensibility tests (P9 Slice 1, D50/D52). An in-process `mcp` server is the
offline seam: dev-registered servers are bridged into the tool table, the model
calls them like any tool, results flow through the transcript as untrusted data,
least-privilege subsets are honored, and a broken/unknown server fails loudly.
"""

import pytest
from mcp.server.fastmcp import FastMCP
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from platform.services.generative_ai_service.engine import engine as engine_mod
from platform.services.generative_ai_service.engine import AgentFeatures, AgentSpec, RunDeps, UserInput
from platform.services.generative_ai_service.engine.providers import BuiltModel
from platform.services.generative_ai_service.extensions import mcp_catalog
from platform.services.generative_ai_service.extensions.mcp_catalog import McpError, resolve_mcp_configs
from platform.services.generative_ai_service.transcript import create_conversation, full_history
from platform.services.generative_ai_service.transcript.types import ItemKind


@pytest.fixture()
def clean_catalog():
    """Snapshotting the dev MCP catalog so per-test registrations don't leak:"""
    snapshot = dict(mcp_catalog._MCP_CATALOG)
    yield
    mcp_catalog._MCP_CATALOG.clear()
    mcp_catalog._MCP_CATALOG.update(snapshot)


def _server(*tools) -> FastMCP:
    """Building an in-process MCP server exposing the given (name, fn) tools:"""
    srv = FastMCP("svc")
    for name, fn in tools:
        srv.tool(name=name)(fn)
    return srv


def _use(model, monkeypatch):
    monkeypatch.setattr(
        engine_mod, "build_model",
        lambda model_id, fallbacks=None: BuiltModel(model=model, requested_id=model_id, served_by="test"),
    )


def _no_native_tools(**kw):
    kw.setdefault("features", AgentFeatures(rag_search=False, summarization=False))
    return AgentSpec(name="a", model="test:test", **kw)


def _calls_then_answers(tool_name: str, json_args: str, answer: str) -> FunctionModel:
    """A model that calls `tool_name` once, then answers from its result:"""
    async def stream_function(messages, info):
        for message in messages:
            for part in getattr(message, "parts", []):
                if getattr(part, "tool_name", None) == tool_name and part.__class__.__name__ == "ToolReturnPart":
                    yield answer
                    return
        yield {1: DeltaToolCall(name=tool_name, json_args=json_args, tool_call_id="mc1")}
    return FunctionModel(stream_function=stream_function)


async def _drain(stream):
    events = [e async for e in stream]
    return events, {e.type: e for e in events}


# ============================================================================
# Bridging + calling
# ============================================================================

@pytest.mark.asyncio
async def test_dev_mcp_tool_is_bridged_and_callable(db, monkeypatch, clean_catalog):
    def weather(city: str) -> str:
        """Get the weather for a city."""
        return f"sunny in {city}"

    mcp_catalog.register_mcp_server("svc", server=_server(("weather", weather))._mcp_server)
    _use(_calls_then_answers("mcp__svc__weather", '{"city": "Paris"}', "It is sunny in Paris."), monkeypatch)

    conv = create_conversation(db, owner_id="u1")
    spec = _no_native_tools(mcp_servers=["svc"])
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    events, by_type = await _drain(engine_mod.stream_agent(spec, deps, UserInput(text="weather in Paris?")))

    assert events[-1].type == "run_completed"
    assert "sunny in Paris" in str(by_type["run_completed"].output)
    # The tool lifecycle surfaced under the namespaced name:
    assert by_type["tool_call_started"].tool_name == "mcp__svc__weather"

    # The call/result landed in the transcript as data (untrusted model input, D49):
    hist = full_history(db, conv.id, owner_id="u1")
    call = next(i for i in hist if i.kind == ItemKind.tool_call)
    result = next(i for i in hist if i.kind == ItemKind.tool_result)
    assert call.tool_name == "mcp__svc__weather"
    assert "sunny in Paris" in result.extract_text()


@pytest.mark.asyncio
async def test_allowed_tools_limits_the_exposed_subset(db, monkeypatch, clean_catalog):
    def safe(x: str) -> str:
        """A safe tool."""
        return f"ok {x}"

    def danger(x: str) -> str:
        """A tool we do not expose."""
        return "should never run"

    srv = _server(("safe", safe), ("danger", danger))
    mcp_catalog.register_mcp_server("svc", server=srv._mcp_server, allowed_tools=["safe"])

    configs = resolve_mcp_configs(["svc"])
    from platform.services.generative_ai_service.extensions import build_mcp_tools
    tools = await build_mcp_tools(configs)
    names = {t.name for t in tools}
    assert names == {"mcp__svc__safe"}, f"least-privilege subset not honored: {names}"


@pytest.mark.asyncio
async def test_unknown_mcp_id_fails_loudly(db, monkeypatch, clean_catalog):
    _use(_calls_then_answers("x", "{}", "y"), monkeypatch)
    conv = create_conversation(db, owner_id="u1")
    spec = _no_native_tools(mcp_servers=["not_registered"])
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    events, _ = await _drain(engine_mod.stream_agent(spec, deps, UserInput(text="hi")))
    assert events[-1].type == "run_failed"
    assert "not_registered" in events[-1].error and "McpError" in events[-1].error


def test_url_reference_is_backcompat_anonymous_server(clean_catalog):
    # A raw URL (no catalog entry) resolves to an anonymous no-auth remote config:
    (config,) = resolve_mcp_configs(["https://mcp.example.com/sse"])
    assert config.url == "https://mcp.example.com/sse"
    assert config.headers is None and config.server is None


@pytest.mark.asyncio
async def test_mcp_tool_error_becomes_a_model_retry_not_a_dead_run(db, monkeypatch, clean_catalog):
    def boom(x: str) -> str:
        """A tool that fails."""
        raise ValueError("upstream exploded")

    mcp_catalog.register_mcp_server("svc", server=_server(("boom", boom))._mcp_server)

    async def stream_function(messages, info):
        """Call the failing tool; once its (error) result appears, adapt and finish."""
        for message in messages:
            for part in getattr(message, "parts", []):
                if getattr(part, "tool_name", None) == "mcp__svc__boom" and part.__class__.__name__ in (
                    "ToolReturnPart", "RetryPromptPart",
                ):
                    yield "The tool failed; I'll stop."
                    return
        yield {1: DeltaToolCall(name="mcp__svc__boom", json_args='{"x": "1"}', tool_call_id="mc1")}

    _use(FunctionModel(stream_function=stream_function), monkeypatch)
    conv = create_conversation(db, owner_id="u1")
    spec = _no_native_tools(mcp_servers=["svc"])
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conv.id)

    events, by_type = await _drain(engine_mod.stream_agent(spec, deps, UserInput(text="go")))
    # The run recovered (the model saw the error as a tool result) rather than dying:
    assert events[-1].type == "run_completed"
    assert "stop" in str(by_type["run_completed"].output).lower()
