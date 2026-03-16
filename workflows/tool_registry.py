"""
Long-running workflow: orchestrates tool proposals and tracks approved tools.

Each proposal is managed by its own ToolProposalWorkflow child. This registry
spawns those children and maintains the authoritative list of approved tools
that the research workflow queries.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.workflow import ParentClosePolicy

from .tool_proposal import ToolProposalWorkflow


@workflow.defn
class ToolRegistryWorkflow:
    """Runs indefinitely. Spawns child workflows for proposals, tracks approvals."""

    def __init__(self):
        self.approved_tools: list[dict] = []
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

    # --- Queries ---

    @workflow.query
    def get_approved_tools(self) -> list[dict]:
        return self.approved_tools

    # --- Main loop ---

    @workflow.run
    async def run(self, carry_over: list[dict] | None = None):
        if carry_over:
            self.approved_tools = carry_over

        cycles = 0
        while cycles < 365:
            cycles += 1
            self._has_updates = False

            await workflow.wait_condition(
                lambda: self._has_updates,
                timeout=timedelta(days=1),
            )

            # Spawn a child workflow for each new proposal
            while self._pending_proposals:
                proposal = self._pending_proposals.pop(0)
                await workflow.start_child_workflow(
                    ToolProposalWorkflow.run,
                    args=[proposal],
                    id=f"tool-proposal-{proposal['id']}",
                    parent_close_policy=ParentClosePolicy.ABANDON,
                )

        workflow.continue_as_new(args=[self.approved_tools])
