"""
Child workflow: manages the lifecycle of a single tool proposal.

Each proposal is its own long-running workflow that:
- Posts the proposal to Slack
- Handles threaded discussion via signals
- Processes approve/reject decisions
- Collects required secrets via Slack modal on approval
- Auto-expires after 15 days with a notification
"""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from activities import (
        discuss_tool_proposal,
        notify_tool_proposal,
        save_tool_secrets,
        send_slack_message,
        write_dynamic_tool,
    )

RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
)

PROPOSAL_TTL = timedelta(days=15)


@workflow.defn
class ToolProposalWorkflow:
    """Manages a single tool proposal's lifecycle: discussion, approval, expiry."""

    def __init__(self):
        self.proposal: dict = {}
        self.status: str = "pending"
        self.discussion: list[dict] = []
        self.thread_ts: str = ""
        self.channel: str = ""
        self._pending_messages: list[dict] = []
        self._pending_secrets: dict[str, str] | None = None
        self._resolved: bool = False
        self._secrets_received: bool = False

    @workflow.signal
    async def discuss(self, message: dict):
        self._pending_messages.append(message)

    @workflow.signal
    async def approve(self, user: str):
        # If the tool needs secrets, defer full approval until they're provided
        required = self.proposal.get("required_secrets", [])
        if required and not self._secrets_received:
            self.status = "awaiting_secrets"
        else:
            self.status = "approved"
            self._resolved = True

    @workflow.signal
    async def provide_secrets(self, secrets: dict):
        self._pending_secrets = secrets
        self._secrets_received = True
        # If we were waiting for secrets, now we can fully approve
        if self.status == "awaiting_secrets":
            self.status = "approved"
            self._resolved = True

    @workflow.signal
    async def reject(self, user: str):
        self.status = "rejected"
        self._resolved = True

    @workflow.query
    def get_status(self) -> dict:
        return {
            "status": self.status,
            "proposal": self.proposal,
            "discussion_count": len(self.discussion),
        }

    @workflow.query
    def get_proposal(self) -> dict:
        return {
            **self.proposal,
            "status": self.status,
            "discussion": self.discussion,
        }

    @workflow.run
    async def run(self, proposal: dict) -> dict:
        self.proposal = proposal
        self.status = "pending"
        start_time = workflow.now()

        # Post the proposal notification to Slack
        slack_result = await workflow.execute_activity(
            notify_tool_proposal,
            args=[proposal],
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RETRY,
        )
        self.thread_ts = slack_result["ts"]
        self.channel = slack_result["channel"]

        # Wait for signals with TTL countdown
        while not self._resolved:
            elapsed = workflow.now() - start_time
            remaining = PROPOSAL_TTL - elapsed
            if remaining.total_seconds() <= 0:
                break

            try:
                await workflow.wait_condition(
                    lambda: self._resolved or bool(self._pending_messages),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                break

            # Process any pending discussion messages
            while self._pending_messages:
                msg = self._pending_messages.pop(0)
                response = await workflow.execute_activity(
                    discuss_tool_proposal,
                    args=[
                        self.proposal,
                        self.discussion,
                        msg["text"],
                        self.channel,
                        self.thread_ts,
                    ],
                    start_to_close_timeout=timedelta(minutes=3),
                    retry_policy=RETRY,
                )
                self.discussion.append({
                    "user": msg.get("user", "unknown"),
                    "user_message": msg["text"],
                    "response": response,
                })

        # Handle the resolution
        if self.status == "approved":
            # Save any secrets that were provided
            if self._pending_secrets:
                await workflow.execute_activity(
                    save_tool_secrets,
                    args=[self._pending_secrets],
                    start_to_close_timeout=timedelta(minutes=1),
                    retry_policy=RETRY,
                )

            await workflow.execute_activity(
                write_dynamic_tool,
                args=[self.proposal],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RETRY,
            )

            tool_def = {
                "name": self.proposal["name"],
                "description": self.proposal["description"],
                "input_schema": self.proposal.get("input_schema", {
                    "type": "object",
                    "properties": {},
                }),
            }
            registry = workflow.get_external_workflow_handle("tool-registry")
            await registry.signal("tool_approved", tool_def)

            workflow.logger.info(f"Tool '{self.proposal['name']}' approved and written")

        elif not self._resolved:
            self.status = "expired"
            await workflow.execute_activity(
                send_slack_message,
                args=[
                    self.channel,
                    (
                        f":hourglass: Tool proposal `{self.proposal['name']}` expired "
                        f"after 15 days without a decision. The agent can propose it "
                        f"again if it's still needed."
                    ),
                    None,
                    None,
                    self.thread_ts,
                ],
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RETRY,
            )

        return {"status": self.status, "proposal_id": self.proposal["id"]}
