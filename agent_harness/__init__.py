"""Agent loop and tool dispatch harness.

Public surface:
- ToolDef: declarative description of a tool the agent can call
- AgentContext: caller-provided hooks (Slack indicators, custom state)
- TurnResult: outcome of one agent turn
- run_agent_turn: the loop itself (call LLM, dispatch tools, repeat)
- execute_tool_dynamic: the single dynamic activity that runs every tool

The loop module is workflow-safe (stdlib + temporalio.workflow only).
The dynamic_executor module runs in the activity worker process.
"""

from agent_harness.loop import (
    AgentContext,
    DEFAULT_LLM_RETRY,
    DEFAULT_TOOL_RETRY,
    TurnResult,
    run_agent_turn,
)
from agent_harness.tooldef import ToolDef, dynamic_tool_to_def

__all__ = [
    "AgentContext",
    "DEFAULT_LLM_RETRY",
    "DEFAULT_TOOL_RETRY",
    "ToolDef",
    "TurnResult",
    "dynamic_tool_to_def",
    "run_agent_turn",
]
