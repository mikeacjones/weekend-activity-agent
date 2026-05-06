"""Declarative tool definition shared by workflow and activity sides.

Stdlib only — workflows can import this without `imports_passed_through`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Awaitable, Callable, Literal, Optional


ToolKind = Literal["activity", "interaction"]


@dataclass(frozen=True)
class ToolDef:
    """Single tool exposed to the agent.

    Two flavors:
    - kind="activity": invoked via `workflow.execute_activity(impl_name, ...)`,
      routed by the dynamic executor to the correct implementation. The
      Temporal UI shows each call under its `impl_name`.
    - kind="interaction": invoked inline as a workflow-safe coroutine. Used
      for tools that need workflow primitives (signals, child workflows)
      rather than activity I/O — e.g. `propose_new_tool`.
    """

    name: str
    description: str
    input_schema: dict
    kind: ToolKind = "activity"
    impl_name: Optional[str] = None
    interaction: Optional[Callable[[dict, "AgentContext"], Awaitable[str]]] = None  # noqa: F821
    timeout: timedelta = field(default_factory=lambda: timedelta(minutes=5))
    terminates_loop: bool = False

    def __post_init__(self):
        if self.kind == "activity" and self.interaction is not None:
            raise ValueError(
                f"ToolDef {self.name!r}: activity-kind tools must not set "
                "an interaction coroutine."
            )
        if self.kind == "interaction" and self.interaction is None:
            raise ValueError(
                f"ToolDef {self.name!r}: interaction-kind tools must provide "
                "an interaction coroutine."
            )

    @property
    def activity_name(self) -> str:
        """Activity-type string used by `workflow.execute_activity`."""
        return self.impl_name or self.name

    def to_anthropic_schema(self) -> dict:
        """Produce the dict shape Anthropic's `tools` parameter expects."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


def dynamic_tool_to_def(meta: dict) -> ToolDef:
    """Convert a `recover_approved_tools` metadata dict into a ToolDef.

    The dynamic executor handles the actual import from `dynamic_tools/{name}.py`
    so all we need here is the schema and the activity name.
    """
    name = meta["name"]
    return ToolDef(
        name=name,
        description=meta.get("description", f"Dynamic tool: {name}"),
        input_schema=meta.get("input_schema") or {"type": "object", "properties": {}},
        kind="activity",
        impl_name=name,
    )
