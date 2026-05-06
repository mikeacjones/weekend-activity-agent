"""Workflow-safe agent loop.

`run_agent_turn` calls the LLM, dispatches tool calls, and loops until the
model emits `end_turn` or a `terminates_loop` tool succeeds. Activity-kind
tools are dispatched via `workflow.execute_activity(td.activity_name, ...)`
so each tool shows under its own name in the Temporal UI (the activity is
resolved by the single dynamic executor in the worker process).

This module imports only stdlib + temporalio.workflow — workflows can use
it directly without `imports_passed_through`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Awaitable, Callable, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

from agent_harness.tooldef import ToolDef


DEFAULT_TOOL_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
)

# LLM calls get a longer window — 529 overload errors can last minutes
# and we'd rather wait than lose an entire turn.
DEFAULT_LLM_RETRY = RetryPolicy(
    maximum_attempts=10,
    initial_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
)


async def _noop_text(_text: str) -> None:
    return None


async def _noop_starting(_calls: list[dict]) -> None:
    return None


async def _noop_failed(_name: str, _err: BaseException) -> None:
    return None


@dataclass
class AgentContext:
    """Caller-provided hooks and free-form state for the agent loop.

    All hooks are optional async callables defaulting to no-ops, so callers
    that don't need them (e.g. the research workflow) can ignore them.

    `state` is a free-form dict that interaction-kind tools can read and
    mutate — for example, the `propose_new_tool` interaction uses it to
    track which proposals have been submitted in this turn.
    """

    on_text: Callable[[str], Awaitable[None]] = _noop_text
    on_tool_calls_starting: Callable[[list[dict]], Awaitable[None]] = _noop_starting
    on_tool_failed: Callable[[str, BaseException], Awaitable[None]] = _noop_failed
    llm_retry: RetryPolicy = field(default_factory=lambda: DEFAULT_LLM_RETRY)
    tool_retry: RetryPolicy = field(default_factory=lambda: DEFAULT_TOOL_RETRY)
    llm_timeout: timedelta = field(default_factory=lambda: timedelta(minutes=3))
    state: dict = field(default_factory=dict)


@dataclass
class TurnResult:
    """Outcome of a single `run_agent_turn` invocation.

    `messages` is the full updated conversation history (caller's input
    list mutated in place + appended assistant/tool turns). `tool_calls_made`
    captures every tool call the LLM emitted, in order — research.py reads
    this to extract `save_recommendation` inputs into its findings list.
    """

    messages: list[dict]
    iterations: int
    stop_reason: str
    tool_calls_made: list[dict]


async def dispatch_tool(
    td: ToolDef,
    tool_input: dict,
    ctx: AgentContext,
) -> str:
    """Run a single tool. Activity for `kind=activity`, inline coroutine for
    `kind=interaction`.

    The activity-type string is `td.activity_name`; in the worker, the single
    `@activity.defn(dynamic=True)` executor catches it and routes by name.
    """
    if td.kind == "interaction":
        # Interaction tools run inline in workflow context so they can use
        # workflow primitives (signals, child workflows, uuid4, now).
        assert td.interaction is not None
        return await td.interaction(tool_input, ctx)

    return await workflow.execute_activity(
        td.activity_name,
        args=[tool_input],
        start_to_close_timeout=td.timeout,
        retry_policy=ctx.tool_retry,
    )


async def run_agent_turn(
    *,
    system_prompt: str,
    messages: list[dict],
    tools: list[ToolDef],
    ctx: AgentContext,
    max_iterations: int,
    parallel_tools: bool = False,
    call_llm_activity: str = "call_llm",
) -> TurnResult:
    """Run the agentic loop until end_turn, terminating tool, or iteration cap.

    Each iteration:
      1. Call the LLM with the current message history + tool schemas.
      2. Emit any text via `ctx.on_text`.
      3. If stop_reason is `end_turn`, finish.
      4. Otherwise dispatch every tool_use block (interactions sequentially
         first, then activity tools — sequential by default, parallel when
         `parallel_tools=True`).
      5. Append the assistant turn and tool_result turn to `messages`.

    The function appends to the caller's `messages` list in place AND returns
    it on the TurnResult so callers can chain or copy.
    """
    by_name: dict[str, ToolDef] = {td.name: td for td in tools}
    tool_schemas = [td.to_anthropic_schema() for td in tools]
    tool_calls_made: list[dict] = []
    iterations = 0
    stop_reason = "max_iterations"

    while iterations < max_iterations:
        iterations += 1

        try:
            llm_response = await workflow.execute_activity(
                call_llm_activity,
                args=[system_prompt, messages, tool_schemas],
                start_to_close_timeout=ctx.llm_timeout,
                retry_policy=ctx.llm_retry,
            )
        except Exception as e:
            workflow.logger.error(
                f"LLM call failed after all retries on iteration {iterations}: {e}"
            )
            stop_reason = "llm_error"
            break

        # Surface any text the LLM produced (even alongside tool calls).
        text_parts = [
            block["text"]
            for block in llm_response["raw_content"]
            if block.get("type") == "text" and block.get("text", "").strip()
        ]
        if text_parts:
            await ctx.on_text("\n".join(text_parts))

        if llm_response["stop_reason"] == "end_turn":
            messages.append(
                {"role": "assistant", "content": llm_response["raw_content"]}
            )
            stop_reason = "end_turn"
            break

        tool_calls = llm_response["tool_calls"]
        tool_calls_made.extend(tool_calls)
        await ctx.on_tool_calls_starting(tool_calls)

        # Slot results in the same order as tool_calls.
        tool_results: list[Optional[dict]] = [None] * len(tool_calls)
        parallel_jobs: list[tuple[int, ToolDef, dict]] = []
        terminator_hit = False

        # First pass: run interaction-kind tools sequentially. They tend to
        # mutate workflow state and we want their effects ordered.
        for idx, call in enumerate(tool_calls):
            td = by_name.get(call["name"])
            if td is None:
                tool_results[idx] = {
                    "type": "tool_result",
                    "tool_use_id": call["id"],
                    "content": (
                        f"Tool {call['name']!r} is not registered. "
                        "Continue with other tools."
                    ),
                    "is_error": True,
                }
                continue

            if td.kind == "interaction":
                content, is_error = await _run_one(td, call, ctx)
                tool_results[idx] = {
                    "type": "tool_result",
                    "tool_use_id": call["id"],
                    "content": content,
                    **({"is_error": True} if is_error else {}),
                }
                if td.terminates_loop and not is_error:
                    terminator_hit = True
            else:
                parallel_jobs.append((idx, td, call))

        # Second pass: activity-kind tools.
        if parallel_jobs:
            if parallel_tools:
                coros = [_run_one(td, call, ctx) for _, td, call in parallel_jobs]
                outcomes = await asyncio.gather(*coros)
            else:
                outcomes = []
                for _, td, call in parallel_jobs:
                    outcomes.append(await _run_one(td, call, ctx))

            for (idx, td, call), (content, is_error) in zip(parallel_jobs, outcomes):
                tool_results[idx] = {
                    "type": "tool_result",
                    "tool_use_id": call["id"],
                    "content": content,
                    **({"is_error": True} if is_error else {}),
                }
                if td.terminates_loop and not is_error:
                    terminator_hit = True

        messages.append({"role": "assistant", "content": llm_response["raw_content"]})
        messages.append({"role": "user", "content": tool_results})

        if terminator_hit:
            stop_reason = "tool_terminated"
            break

    return TurnResult(
        messages=messages,
        iterations=iterations,
        stop_reason=stop_reason,
        tool_calls_made=tool_calls_made,
    )


async def _run_one(
    td: ToolDef,
    call: dict,
    ctx: AgentContext,
) -> tuple[str, bool]:
    """Dispatch a single tool, returning (content, is_error)."""
    try:
        content = await dispatch_tool(td, call["input"], ctx)
        return content, False
    except Exception as e:
        await ctx.on_tool_failed(call["name"], e)
        return (
            f"Tool call failed with error: {e}\n"
            f"Continue with other tools or let the user know.",
            True,
        )
