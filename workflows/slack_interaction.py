"""
Workflow for handling Slack interactions that need LLM calls.

Triggered by the Socket Mode listener when a user clicks "Tell me more"
on a recommendation. Running this through Temporal gives us:
- Automatic retry if the LLM call fails
- Visibility in the Temporal UI
- Consistent activity-level heartbeating
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from activities import call_llm_simple, send_slack_message

RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
)


@workflow.defn
class SlackInteractionWorkflow:
    """Short-lived workflow: handles a single Slack interaction."""

    @workflow.run
    async def run(self, interaction: dict):
        match interaction["type"]:
            case "more_info":
                await self._handle_more_info(interaction)

    async def _handle_more_info(self, interaction: dict):
        slug = interaction["slug"]
        context = interaction["context"]
        channel = interaction["channel"]
        thinking_ts = interaction["thinking_ts"]
        thread_ts = interaction["thread_ts"]

        # LLM call as an activity — retried automatically on failure
        response_text = await workflow.execute_activity(
            call_llm_simple,
            args=[
                f"""A user wants more details about this weekend activity recommendation:

{context}

Give a helpful, detailed follow-up in Slack mrkdwn format. Include:
- Practical tips (parking, best times to go, what to bring)
- Any insider knowledge about the venue/location
- Nearby food/drink options
- Links if you have them

Keep it conversational. 2-3 short paragraphs max.""",
            ],
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RETRY,
        )

        # Update the "thinking" message with the real response
        await workflow.execute_activity(
            send_slack_message,
            args=[channel, response_text, None, thinking_ts],
            start_to_close_timeout=timedelta(minutes=1),
            retry_policy=RETRY,
        )
