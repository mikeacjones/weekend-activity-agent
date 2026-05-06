"""
Child workflow: interactive Slack conversation triggered by @mentioning the bot.

Each conversation is its own workflow that:
- Runs an agentic loop (LLM + tools) for each user message
- Posts LLM text and tool call indicators to the Slack thread in real time
- Waits for follow-up messages via signals
- Closes after 2 days of inactivity with a notification

The agent loop lives in `agent_harness.loop.run_agent_turn`. We hand it
`AgentContext` hooks so the loop can fire our Slack-posting side effects
without knowing anything about Slack.
"""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

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
        send_slack_message,
    )
    from config import Location, Preferences, build_conversation_prompt
    from proposal_utils import format_rejected_capability_note
    from tools import MEMORY_TOOLS, STATIC_TOOLS

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
        messages: list[dict] = []
        self._proposed_tool_names = set()
        self._proposed_capability_keys = set()

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
        all_tools: list,
    ):
        """Run the agentic loop for a single user message, post results to Slack."""
        messages.append({"role": "user", "content": user_text})

        async def _on_tool_calls_starting(calls: list[dict]):
            indicators = [
                TOOL_LABELS.get(call["name"], f":gear: Using {call['name']}")
                for call in calls
            ]
            await self._post_to_thread("_" + " · ".join(indicators) + "..._")

        async def _on_tool_failed(name: str, err: BaseException):
            label = TOOL_LABELS.get(name, name)
            await self._post_to_thread(f":warning: {label} failed: {err}")

        ctx = AgentContext(
            on_text=self._post_to_thread,
            on_tool_calls_starting=_on_tool_calls_starting,
            on_tool_failed=_on_tool_failed,
            llm_retry=DEFAULT_LLM_RETRY,
            tool_retry=DEFAULT_TOOL_RETRY,
            llm_timeout=timedelta(minutes=3),
            state={
                "all_tools": all_tools,
                "rejected_tools": self._rejected_tools,
                "proposed_tool_names": self._proposed_tool_names,
                "proposed_capability_keys": self._proposed_capability_keys,
            },
        )

        await run_agent_turn(
            system_prompt=system_prompt,
            messages=messages,
            tools=all_tools,
            ctx=ctx,
            max_iterations=MAX_ITERATIONS,
        )

    async def _post_to_thread(self, text: str):
        """Post a message to the conversation's Slack thread."""
        await workflow.execute_activity(
            send_slack_message,
            args=[self.channel, text, None, None, self.thread_ts],
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RETRY,
        )

    async def _build_tool_list(self) -> list:
        """Build the full tool list, refreshing dynamic tools each time."""
        dynamic_tools = await self._get_dynamic_tools()
        return (
            STATIC_TOOLS
            + MEMORY_TOOLS
            + [dynamic_tool_to_def(m) for m in dynamic_tools]
        )

    async def _get_dynamic_tools(self) -> list[dict]:
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
        try:
            return await workflow.execute_activity(
                recover_rejected_tool_entries,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RETRY,
            )
        except Exception:
            workflow.logger.warn("Could not load rejected tools")
            return []
