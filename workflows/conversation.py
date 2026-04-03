"""
Child workflow: interactive Slack conversation triggered by @mentioning the bot.

Each conversation is its own workflow that:
- Runs an agentic loop (LLM + tools) for each user message
- Posts LLM text and tool call indicators to the Slack thread in real time
- Waits for follow-up messages via signals
- Closes after 2 days of inactivity with a notification
"""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from activities import (
        call_llm,
        execute_tool,
        recover_approved_tools,
        recover_rejected_tool_entries,
        send_slack_message,
    )
    from config import Location, Preferences, build_conversation_prompt
    from proposal_utils import (
        format_rejected_capability_note,
        normalize_identifier,
        normalize_proposal,
        validate_proposal,
    )
    from tools import TOOL_DEFINITIONS, MEMORY_TOOLS

RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
)

MAX_ITERATIONS = 15  # per user message
INACTIVITY_TTL = timedelta(days=2)

TOOL_LABELS = {
    "search_events": ":calendar: Searching for events",
    "search_outdoors": ":national_park: Searching outdoor activities",
    "get_weather": ":partly_sunny: Checking the weather",
    "read_page": ":globe_with_meridians: Reading page",
    "read_file": ":page_facing_up: Reading file",
    "save_recommendation": ":pushpin: Saving recommendation",
    "save_memory": ":brain: Saving to memory",
    "recall_memories": ":brain: Checking memory",
    "propose_new_tool": ":hammer_and_wrench: Submitting tool proposal",
}


@workflow.defn
class ConversationWorkflow:
    """Interactive Slack conversation with full tool access."""

    def __init__(self):
        self.channel: str = ""
        self.thread_ts: str = ""
        self._pending_messages: list[dict] = []
        self._closed: bool = False
        self._rejected_tools: list[dict] = []
        self._proposed_tool_names: set[str] = set()
        self._proposed_capability_keys: set[str] = set()

    @workflow.signal
    async def message(self, msg: dict):
        self._pending_messages.append(msg)

    @workflow.query
    def get_status(self) -> dict:
        return {
            "channel": self.channel,
            "thread_ts": self.thread_ts,
            "closed": self._closed,
        }

    @workflow.run
    async def run(self, channel: str, thread_ts: str, initial_message: str) -> dict:
        self.channel = channel
        self.thread_ts = thread_ts

        location = Location()
        prefs = Preferences()
        system_prompt = build_conversation_prompt(location, prefs)
        self._rejected_tools = await self._get_rejected_tool_entries()
        rejected_note_text = format_rejected_capability_note(self._rejected_tools)
        if rejected_note_text:
            system_prompt += (
                "\n\nPREVIOUSLY REJECTED CAPABILITIES "
                "(do NOT propose these again, even under a new tool name): "
                + rejected_note_text
            )

        # Conversation history persists across all messages in this thread
        messages = []
        self._proposed_tool_names = set()
        self._proposed_capability_keys = set()

        # Process the initial message
        all_tools = await self._build_tool_list()
        await self._handle_user_message(
            initial_message, messages, system_prompt, all_tools,
        )

        # Wait for follow-up messages with inactivity TTL
        while not self._closed:
            try:
                await workflow.wait_condition(
                    lambda: bool(self._pending_messages),
                    timeout=INACTIVITY_TTL,
                )
            except asyncio.TimeoutError:
                await self._post_to_thread(
                    ":wave: Closing this thread — no activity for 2 days. "
                    "Tag me again anytime to start a new conversation!"
                )
                self._closed = True
                break

            while self._pending_messages:
                msg = self._pending_messages.pop(0)
                all_tools = await self._build_tool_list()
                await self._handle_user_message(
                    msg["text"], messages, system_prompt, all_tools,
                )

        return {"closed_reason": "inactivity"}

    async def _handle_user_message(
        self,
        user_text: str,
        messages: list[dict],
        system_prompt: str,
        all_tools: list[dict],
    ):
        """Run the agentic loop for a single user message, post result to Slack."""
        messages.append({"role": "user", "content": user_text})

        iteration = 0
        while iteration < MAX_ITERATIONS:
            iteration += 1

            llm_response = await workflow.execute_activity(
                call_llm,
                args=[system_prompt, messages, all_tools],
                start_to_close_timeout=timedelta(minutes=3),
                retry_policy=RETRY,
            )

            # Post any text the LLM produced (even alongside tool calls)
            text_parts = [
                b["text"] for b in llm_response["raw_content"]
                if b["type"] == "text" and b["text"].strip()
            ]
            if text_parts:
                await self._post_to_thread("\n".join(text_parts))

            # Done — no tool calls
            if llm_response["stop_reason"] == "end_turn":
                messages.append({
                    "role": "assistant",
                    "content": llm_response["raw_content"],
                })
                break

            # Show tool call indicators and execute
            tool_results = []
            tool_names = [tc["name"] for tc in llm_response["tool_calls"]]
            indicators = [
                TOOL_LABELS.get(name, f":gear: Using {name}")
                for name in tool_names
            ]
            await self._post_to_thread("_" + " · ".join(indicators) + "..._")

            for tool_call in llm_response["tool_calls"]:
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
                            f"({proposal['capability_key']}) submitted for review."
                        ),
                    })
                    continue

                try:
                    result = await workflow.execute_activity(
                        execute_tool,
                        args=[tool_call["name"], tool_call["input"]],
                        start_to_close_timeout=timedelta(minutes=5),
                        retry_policy=RETRY,
                    )
                except Exception as e:
                    label = TOOL_LABELS.get(
                        tool_call["name"], tool_call["name"],
                    )
                    await self._post_to_thread(
                        f":warning: {label} failed: {e}"
                    )
                    result = (
                        f"Tool call failed with error: {e}\n"
                        f"Let the user know and suggest alternatives."
                    )

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_call["id"],
                    "content": result,
                })

            messages.append({"role": "assistant", "content": llm_response["raw_content"]})
            messages.append({"role": "user", "content": tool_results})

    async def _post_to_thread(self, text: str):
        """Post a message to the conversation's Slack thread."""
        await workflow.execute_activity(
            send_slack_message,
            args=[self.channel, text, None, None, self.thread_ts],
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RETRY,
        )

    async def _handle_tool_proposal(self, proposal: dict):
        """Signal the registry to create a new tool proposal."""
        registry = workflow.get_external_workflow_handle("tool-registry")
        await registry.signal("propose_tool", proposal)

    async def _build_tool_list(self) -> list[dict]:
        """Build the full tool list, refreshing dynamic tools each time."""
        dynamic_tools = await self._get_dynamic_tools()
        return TOOL_DEFINITIONS + MEMORY_TOOLS + dynamic_tools

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
            for tool in self._rejected_tools
            if tool.get("name")
        }
        rejected_capability_keys = {
            normalize_identifier(str(tool.get("capability_key", "")))
            for tool in self._rejected_tools
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
