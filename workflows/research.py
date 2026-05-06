"""
Child workflow: one day's agentic research session.

The loop now lives in `agent_harness.loop.run_agent_turn`. Each LLM call and
each tool call is still a separate Temporal activity, giving us fine-grained
durability — but every tool now shows up in the Temporal UI under its own
name (search_events, get_weather, ...) thanks to the dynamic-activity
executor in `agent_harness.dynamic_executor`.

The propose_new_tool flow lives in `tools._propose_new_tool_interaction`,
which signals the long-running tool-registry workflow.
"""

from datetime import timedelta

from temporalio import workflow

from agent_harness import (
    AgentContext,
    DEFAULT_LLM_RETRY,
    DEFAULT_TOOL_RETRY,
    dynamic_tool_to_def,
    run_agent_turn,
)

with workflow.unsafe.imports_passed_through():
    from activities import (
        recover_approved_tools,
        recover_rejected_tool_entries,
    )
    from config import Location, Preferences, build_system_prompt
    from proposal_utils import format_rejected_capability_note
    from tools import STATIC_TOOLS


MAX_ITERATIONS = 25  # guardrail against runaway loops


@workflow.defn
class AgenticResearchWorkflow:
    """Runs a single research session with an agentic LLM loop."""

    def __init__(self):
        self.iteration: int = 0
        self.findings: list[dict] = []
        self.rejected_tools: list[dict] = []

    @workflow.query
    def get_progress(self) -> dict:
        return {
            "iteration": self.iteration,
            "findings_count": len(self.findings),
        }

    @workflow.run
    async def run(self, location_area: str, day: str, focus: str) -> list[dict]:
        location = Location()
        prefs = Preferences()
        system_prompt = build_system_prompt(location, prefs)

        dynamic_tools = await self._get_dynamic_tools()
        all_tools = STATIC_TOOLS + [dynamic_tool_to_def(m) for m in dynamic_tools]

        self.rejected_tools = await self._get_rejected_tool_entries()
        rejected_note_text = format_rejected_capability_note(self.rejected_tools)
        if rejected_note_text:
            system_prompt += (
                "\n\nPREVIOUSLY REJECTED CAPABILITIES "
                "(do NOT propose these again, even under a new tool name): "
                + rejected_note_text
            )

        messages = [{
            "role": "user",
            "content": (
                f"Today is {day}. Your research focus for today:\n\n{focus}\n\n"
                f"Location: {location.area_description}\n"
                f"This is one day in a Mon-Thu research cycle. Focus on today's "
                f"theme but note anything interesting you find along the way."
            ),
        }]

        ctx = AgentContext(
            llm_retry=DEFAULT_LLM_RETRY,
            tool_retry=DEFAULT_TOOL_RETRY,
            llm_timeout=timedelta(minutes=3),
            state={
                "all_tools": all_tools,
                "rejected_tools": self.rejected_tools,
                "proposed_tool_names": set(),
                "proposed_capability_keys": set(),
            },
        )

        result = await run_agent_turn(
            system_prompt=system_prompt,
            messages=messages,
            tools=all_tools,
            ctx=ctx,
            max_iterations=MAX_ITERATIONS,
        )
        self.iteration = result.iterations

        # Pull save_recommendation inputs out of the tool calls — those are
        # the agent's findings for today.
        for call in result.tool_calls_made:
            if call["name"] == "save_recommendation":
                self.findings.append(call["input"])

        workflow.logger.info(
            f"{day} research done: {len(self.findings)} findings "
            f"in {result.iterations} iterations (stop: {result.stop_reason})"
        )
        return self.findings

    async def _get_dynamic_tools(self) -> list[dict]:
        try:
            return await workflow.execute_activity(
                recover_approved_tools,
                start_to_close_timeout=timedelta(minutes=1),
            )
        except Exception:
            workflow.logger.warn("Could not load dynamic tools")
            return []

    async def _get_rejected_tool_entries(self) -> list[dict]:
        try:
            return await workflow.execute_activity(
                recover_rejected_tool_entries,
                start_to_close_timeout=timedelta(minutes=1),
            )
        except Exception:
            workflow.logger.warn("Could not load rejected tools")
            return []
