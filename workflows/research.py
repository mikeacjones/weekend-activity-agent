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
    from activities import (
        call_llm,
        execute_tool,
        recover_approved_tools,
        recover_rejected_tool_entries,
    )
    from config import Location, Preferences, build_system_prompt
    from proposal_utils import (
        format_rejected_capability_note,
        normalize_identifier,
        normalize_proposal,
        validate_proposal,
    )
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
        self.rejected_tools: list[dict] = []
        self._proposed_tool_names: set[str] = set()
        self._proposed_capability_keys: set[str] = set()

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

        # Load rejected capabilities so the LLM knows not to re-propose them.
        self.rejected_tools = await self._get_rejected_tool_entries()
        rejected_note_text = format_rejected_capability_note(self.rejected_tools)
        if rejected_note_text:
            rejected_note = (
                "\n\nPREVIOUSLY REJECTED CAPABILITIES "
                "(do NOT propose these again, even under a new tool name): "
                + rejected_note_text
            )
            system_prompt += rejected_note

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
        self._proposed_tool_names = set()
        self._proposed_capability_keys = set()

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
                    proposal = normalize_proposal({
                        "id": str(workflow.uuid4())[:8],
                        "proposed_at": workflow.now().isoformat(),
                        **tool_call["input"],
                    })
                    validation_errors = self._validate_tool_proposal(proposal, all_tools)
                    if validation_errors:
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_call["id"],
                            "content": (
                                "Tool proposal rejected automatically: "
                                + "; ".join(validation_errors[:4])
                                + " Continue research with existing tools."
                            ),
                        })
                        continue
                    self._proposed_tool_names.add(proposal["name"])
                    self._proposed_capability_keys.add(proposal["capability_key"])
                    await self._handle_tool_proposal(proposal)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_call["id"],
                        "content": (
                            f"Tool proposal `{proposal['name']}` "
                            f"({proposal['capability_key']}) submitted for review. "
                            "Continuing research."
                        ),
                    })
                    continue

                # Regular tool execution
                try:
                    result = await workflow.execute_activity(
                        execute_tool,
                        args=[tool_call["name"], tool_call["input"]],
                        start_to_close_timeout=timedelta(minutes=5),
                        retry_policy=RETRY,
                    )
                except Exception as e:
                    workflow.logger.warn(f"Tool {tool_call['name']} failed: {e}")
                    result = (
                        f"Tool call failed with error: {e}\n"
                        f"Continue research with other tools."
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

    async def _handle_tool_proposal(self, proposal: dict):
        """Signal the ToolRegistryWorkflow. The registry spawns a child workflow
        that handles Slack notification and discussion."""
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

    async def _get_rejected_tool_entries(self) -> list[dict]:
        """Load rejected capability entries from disk."""
        try:
            return await workflow.execute_activity(
                recover_rejected_tool_entries,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RETRY,
            )
        except Exception:
            workflow.logger.warn("Could not load rejected tools")
            return []

    def _validate_tool_proposal(self, proposal: dict, all_tools: list[dict]) -> list[str]:
        approved_tool_names = {
            normalize_identifier(str(tool.get("name", "")))
            for tool in all_tools
            if tool.get("name")
        }
        approved_capability_keys = {
            normalize_identifier(str(tool.get("capability_key", tool.get("name", ""))))
            for tool in all_tools
            if tool.get("name") or tool.get("capability_key")
        }
        rejected_tool_names = {
            normalize_identifier(str(tool.get("name", "")))
            for tool in self.rejected_tools
            if tool.get("name")
        }
        rejected_capability_keys = {
            normalize_identifier(str(tool.get("capability_key", "")))
            for tool in self.rejected_tools
            if tool.get("capability_key")
        }
        return validate_proposal(
            proposal,
            approved_tool_names=approved_tool_names,
            approved_capability_keys=approved_capability_keys,
            rejected_tool_names=rejected_tool_names,
            rejected_capability_keys=rejected_capability_keys,
            pending_tool_names=set(self._proposed_tool_names),
            pending_capability_keys=set(self._proposed_capability_keys),
        )
