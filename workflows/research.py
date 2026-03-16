"""
Child workflow: one day's agentic research session.

The LLM loop lives here. Each LLM call and each tool execution is a separate
Temporal activity, giving us fine-grained durability:

  If a tool call fails at step 17, Temporal replays steps 1-16 from history
  (no re-execution, no re-calling the LLM) and retries only step 17.

This workflow also handles the propose_new_tool flow by signaling the
ToolRegistryWorkflow — without blocking its own execution.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from activities import call_llm, execute_tool, recover_approved_tools
    from config import Location, Preferences, build_system_prompt
    from tools import TOOL_DEFINITIONS

RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
)

MAX_ITERATIONS = 25  # guardrail against runaway loops


@workflow.defn
class AgenticResearchWorkflow:
    """Runs a single research session with an agentic LLM loop."""

    def __init__(self):
        self.iteration: int = 0
        self.findings: list[dict] = []

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

        # Fetch any dynamically approved tools from the registry
        dynamic_tools = await self._get_dynamic_tools()
        all_tools = TOOL_DEFINITIONS + dynamic_tools

        messages = [{
            "role": "user",
            "content": (
                f"Today is {day}. Your research focus for today:\n\n{focus}\n\n"
                f"Location: {location.area_description}\n"
                f"This is one day in a Mon-Thu research cycle. Focus on today's "
                f"theme but note anything interesting you find along the way."
            ),
        }]

        self.findings = []
        self.iteration = 0

        while self.iteration < MAX_ITERATIONS:
            self.iteration += 1

            # --- LLM reasoning step (persisted as activity result) ---
            llm_response = await workflow.execute_activity(
                call_llm,
                args=[system_prompt, messages, all_tools],
                start_to_close_timeout=timedelta(minutes=3),
                retry_policy=RETRY,
            )

            # Agent decided it's done researching
            if llm_response["stop_reason"] == "end_turn":
                break

            # --- Execute each tool call as its own activity ---
            tool_results = []
            for tool_call in llm_response["tool_calls"]:
                # Handle special tools that interact with workflows
                if tool_call["name"] == "propose_new_tool":
                    await self._handle_tool_proposal(tool_call)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_call["id"],
                        "content": "Tool proposal submitted for review. Continuing research.",
                    })
                    continue

                # Regular tool execution
                result = await workflow.execute_activity(
                    execute_tool,
                    args=[tool_call["name"], tool_call["input"]],
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=RETRY,
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_call["id"],
                    "content": result,
                })

                # Capture recommendations
                if tool_call["name"] == "save_recommendation":
                    self.findings.append(tool_call["input"])

            # Build conversation for next loop iteration
            messages.append({"role": "assistant", "content": llm_response["raw_content"]})
            messages.append({"role": "user", "content": tool_results})

        workflow.logger.info(
            f"{day} research done: {len(self.findings)} findings "
            f"in {self.iteration} iterations"
        )
        return self.findings

    async def _handle_tool_proposal(self, tool_call: dict):
        """Signal the ToolRegistryWorkflow. The registry spawns a child workflow
        that handles Slack notification and discussion."""
        proposal = {
            "id": str(workflow.uuid4())[:8],
            "proposed_at": workflow.now().isoformat(),
            **tool_call["input"],
        }

        registry_handle = workflow.get_external_workflow_handle("tool-registry")
        await registry_handle.signal("propose_tool", proposal)

    async def _get_dynamic_tools(self) -> list[dict]:
        """Load approved dynamic tools from disk."""
        try:
            return await workflow.execute_activity(
                recover_approved_tools,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RETRY,
            )
        except Exception:
            workflow.logger.warn("Could not load dynamic tools")
            return []
