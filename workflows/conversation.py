"""
Child workflow: interactive Slack conversation triggered by @mentioning the bot.

Each conversation is its own workflow that:
- Runs an agentic loop (LLM + tools) for each user message
- Waits for follow-up messages via signals
- Closes after 2 days of inactivity with a notification
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from activities import call_llm, execute_tool, notify_tool_proposal, send_slack_message
    from config import Location, Preferences, build_conversation_prompt
    from tools import TOOL_DEFINITIONS, MEMORY_TOOLS

RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
)

MAX_ITERATIONS = 15  # per user message
INACTIVITY_TTL = timedelta(days=2)


@workflow.defn
class ConversationWorkflow:
    """Interactive Slack conversation with full tool access."""

    def __init__(self):
        self.channel: str = ""
        self.thread_ts: str = ""
        self._pending_messages: list[dict] = []
        self._has_message: bool = False
        self._closed: bool = False

    @workflow.signal
    async def message(self, msg: dict):
        self._pending_messages.append(msg)
        self._has_message = True

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

        # Fetch approved dynamic tools
        dynamic_tools = await self._get_dynamic_tools()
        all_tools = TOOL_DEFINITIONS + MEMORY_TOOLS + dynamic_tools

        # Conversation history persists across all messages in this thread
        messages = []

        # Process the initial message
        await self._handle_user_message(
            initial_message, messages, system_prompt, all_tools,
        )

        # Wait for follow-up messages with inactivity TTL
        while not self._closed:
            self._has_message = False

            timed_out = not await workflow.wait_condition(
                lambda: self._has_message,
                timeout=INACTIVITY_TTL,
            )

            if timed_out:
                # No activity for 2 days — close the thread
                await self._post_to_thread(
                    ":wave: Closing this thread — no activity for 2 days. "
                    "Tag me again anytime to start a new conversation!"
                )
                self._closed = True
                break

            # Process all pending messages
            while self._pending_messages:
                msg = self._pending_messages.pop(0)
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

            # Extract text response for Slack
            if llm_response["stop_reason"] == "end_turn":
                text_parts = [
                    b["text"] for b in llm_response["raw_content"]
                    if b["type"] == "text"
                ]
                if text_parts:
                    messages.append({
                        "role": "assistant",
                        "content": llm_response["raw_content"],
                    })
                    await self._post_to_thread("\n".join(text_parts))
                break

            # Process tool calls
            tool_results = []
            for tool_call in llm_response["tool_calls"]:
                if tool_call["name"] == "propose_new_tool":
                    await self._handle_tool_proposal(tool_call)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_call["id"],
                        "content": "Tool proposal submitted for review.",
                    })
                    continue

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

    async def _handle_tool_proposal(self, tool_call: dict):
        """Signal the registry to create a new tool proposal."""
        proposal = {
            "id": str(workflow.uuid4())[:8],
            "proposed_at": workflow.now().isoformat(),
            **tool_call["input"],
        }
        registry = workflow.get_external_workflow_handle("tool-registry")
        await registry.signal("propose_tool", proposal)

    async def _get_dynamic_tools(self) -> list[dict]:
        """Query the registry for approved dynamic tools."""
        try:
            registry = workflow.get_external_workflow_handle("tool-registry")
            approved = await registry.query("get_approved_tools")
            return [
                {
                    "name": t["name"],
                    "description": t["description"],
                    "input_schema": t.get("input_schema", {
                        "type": "object",
                        "properties": {},
                    }),
                }
                for t in approved
            ]
        except Exception:
            workflow.logger.warn("Could not query tool registry for conversation")
            return []
