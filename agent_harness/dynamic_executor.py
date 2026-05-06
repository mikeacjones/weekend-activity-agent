"""Single dynamic activity that executes every tool.

Workflows call `workflow.execute_activity("search_events", args=[input], ...)`.
The Temporal SDK looks for an activity registered under that name; finding none,
it routes to this dynamic handler. We read `activity.info().activity_type` to
recover the tool name and dispatch.

Result: the Temporal UI shows each call under the tool's actual name (the
activity_type), even though only one activity is registered. Static and
dynamic-from-disk tools take exactly the same code path.

This module imports `tool_impls` (which has httpx, importlib, env reads), so
it runs only in the activity worker process — workflows must NOT import it.
"""

from collections.abc import Sequence

from temporalio import activity
from temporalio.common import RawValue
from temporalio.exceptions import ApplicationError

import tool_impls


@activity.defn(dynamic=True)
async def execute_tool_dynamic(args: Sequence[RawValue]) -> str:
    """Catch-all activity for every tool. Routes by activity_type."""
    name = activity.info().activity_type
    activity.heartbeat(f"Executing: {name}")

    # Decode the single dict argument. Workflows pass `args=[tool_input]`.
    tool_input: dict
    if not args:
        tool_input = {}
    else:
        tool_input = activity.payload_converter().from_payload(
            args[0].payload, dict
        )

    impl = tool_impls.STATIC_IMPLS.get(name)
    if impl is not None:
        return await impl(tool_input)

    # Fall through to a dynamic tool from disk (dynamic_tools/{name}.py).
    try:
        return await tool_impls.dispatch_dynamic_tool(name, tool_input)
    except FileNotFoundError as e:
        raise ApplicationError(
            f"Unknown tool: {name}",
            type="UnknownTool",
            non_retryable=True,
        ) from e
