"""
Engine — the facade over Pydantic AI (Phase 2 public surface).

Callers use `stream_agent` / `run_agent` and the facade types; `pydantic_ai`
lives only in adapter.py and providers.py (D3).
"""

from .engine import find_pending_approvals, resume_agent, resume_agent_sync, run_agent, stream_agent
from .model_info import ModelInfo, get_model_info
from .tool_table import (
    RegisteredTool,
    ToolContext,
    register_tool,
    registered_tool_names,
    resolve_tools,
)
from .types import (
    AgentFeatures,
    AgentLimits,
    AgentSpec,
    ApprovalDecision,
    ApprovalResolutionError,
    CapabilityUnavailableError,
    ContextOverflowError,
    EngineError,
    MediaGenerated,
    MediaModels,
    ModelSwitched,
    PendingApproval,
    RunCompleted,
    RunDeps,
    RunEvent,
    RunFailed,
    RunPaused,
    RunResult,
    RunStarted,
    TextDelta,
    ThinkingDelta,
    ToolAlreadyRegisteredError,
    ToolCallFinished,
    ToolCallStarted,
    ToolExecutionError,
    ToolNotRegisteredError,
    UserInput,
)


__all__ = [
    "run_agent", "stream_agent", "resume_agent", "resume_agent_sync", "find_pending_approvals",
    "ModelInfo", "get_model_info",
    "AgentSpec", "AgentFeatures", "AgentLimits", "MediaModels", "RunDeps", "UserInput",
    "RunEvent", "RunStarted", "TextDelta", "ThinkingDelta", "ToolCallStarted",
    "ToolCallFinished", "MediaGenerated", "ModelSwitched", "RunCompleted", "RunPaused",
    "RunFailed", "RunResult",
    # HITL approval (P8, D46-D48):
    "ApprovalDecision", "PendingApproval", "ApprovalResolutionError",
    "EngineError", "CapabilityUnavailableError", "ContextOverflowError",
    # Tool table (P5, D33):
    "ToolContext", "RegisteredTool", "register_tool", "resolve_tools",
    "registered_tool_names", "ToolNotRegisteredError", "ToolAlreadyRegisteredError",
    "ToolExecutionError",
]
