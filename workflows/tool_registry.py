"""
Long-running workflow: orchestrates tool proposals and tracks approved tools.

Each proposal is managed by its own ToolProposalWorkflow child. This registry
spawns those children and maintains the authoritative list of approved tools
that the research workflow queries.
"""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from activities import recover_approved_tools, recover_rejected_tools

from .tool_proposal import ToolProposalWorkflow

RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
)


@workflow.defn
class ToolRegistryWorkflow:
    """Runs indefinitely. Spawns child workflows for proposals, tracks approvals."""

    def __init__(self):
        self.approved_tools: list[dict] = []
        self.rejected_tool_names: list[str] = []
        self._pending_proposals: list[dict] = []
        self._has_updates: bool = False

    # --- Signals ---

    @workflow.signal
    async def propose_tool(self, proposal: dict):
        self._pending_proposals.append(proposal)
        self._has_updates = True
        workflow.logger.info(f"New tool proposal: {proposal['name']} ({proposal['id']})")

    @workflow.signal
    async def tool_approved(self, tool: dict):
        self.approved_tools.append(tool)
        self._has_updates = True
        workflow.logger.info(f"Tool registered: {tool['name']}")

    @workflow.signal
    async def tool_rejected(self, tool_name: str):
        if tool_name not in self.rejected_tool_names:
            self.rejected_tool_names.append(tool_name)
        workflow.logger.info(f"Tool rejected: {tool_name}")

    # --- Queries ---

    @workflow.query
    def get_approved_tools(self) -> list[dict]:
        return self.approved_tools

    @workflow.query
    def get_rejected_tools(self) -> list[str]:
        return self.rejected_tool_names

    # --- Main loop ---

    @workflow.run
    async def run(
        self,
        carry_over: list[dict] | None = None,
        rejected_carry_over: list[str] | None = None,
    ):
        if carry_over:
            self.approved_tools = carry_over
        else:
            recovered = await workflow.execute_activity(
                recover_approved_tools,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RETRY,
            )
            if recovered:
                self.approved_tools = recovered
                workflow.logger.info(f"Recovered {len(recovered)} tools from disk")

        if rejected_carry_over:
            self.rejected_tool_names = rejected_carry_over
        else:
            rejected = await workflow.execute_activity(
                recover_rejected_tools,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RETRY,
            )
            if rejected:
                self.rejected_tool_names = rejected
                workflow.logger.info(f"Recovered {len(rejected)} rejected tools from disk")

        cycles = 0
        while cycles < 365:
            cycles += 1
            self._has_updates = False

            try:
                await workflow.wait_condition(
                    lambda: self._has_updates,
                    timeout=timedelta(days=1),
                )
            except asyncio.TimeoutError:
                continue

            # Spawn a child workflow for each new proposal
            while self._pending_proposals:
                proposal = self._pending_proposals.pop(0)
                await workflow.start_child_workflow(
                    ToolProposalWorkflow.run,
                    args=[proposal],
                    id=f"tool-proposal-{proposal['id']}",
                    parent_close_policy=ParentClosePolicy.ABANDON,
                )

        workflow.continue_as_new(args=[self.approved_tools, self.rejected_tool_names])
