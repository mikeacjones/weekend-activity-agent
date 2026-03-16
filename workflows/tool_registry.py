"""
Long-running workflow: manages dynamic tool proposals and approvals.

Lifecycle of a tool proposal:
  1. Research agent proposes a tool → signal arrives here
  2. Proposal stored as "pending" with a timestamp
  3. User gets notified on Slack
  4. User reviews code via CLI and approves/rejects
  5. On approval: tool code is written to dynamic_tools/<name>.py
  6. On next research run: research workflow queries approved tools
  7. If no response in 15 days: auto-expired

Non-blocking by design — the research workflow signals and moves on.
"""

from datetime import timedelta
from pathlib import Path

from temporalio import workflow
from temporalio.common import RetryPolicy

PROPOSAL_TTL = timedelta(days=15)


@workflow.defn
class ToolRegistryWorkflow:
    """Runs indefinitely. Manages the lifecycle of dynamic tool proposals."""

    def __init__(self):
        self.proposals: dict[str, dict] = {}
        self._has_updates: bool = False
        self._initial_proposals: dict[str, dict] = {}

    # --- Signals (mutations from external sources) ---

    @workflow.signal
    async def propose_tool(self, proposal: dict):
        """Called by research workflows when the agent proposes a new tool."""
        proposal["status"] = "pending"
        self.proposals[proposal["id"]] = proposal
        self._has_updates = True
        workflow.logger.info(f"New tool proposal: {proposal['name']} ({proposal['id']})")

    @workflow.signal
    async def approve_tool(self, proposal_id: str):
        """Called by CLI when the user approves a proposal."""
        if proposal_id not in self.proposals:
            workflow.logger.warn(f"Unknown proposal: {proposal_id}")
            return

        proposal = self.proposals[proposal_id]
        if proposal["status"] != "pending":
            workflow.logger.warn(
                f"Proposal {proposal_id} is {proposal['status']}, cannot approve"
            )
            return

        proposal["status"] = "approved"
        self._has_updates = True
        workflow.logger.info(f"Tool approved: {proposal['name']} ({proposal_id})")

        # Write the implementation to dynamic_tools/
        await self._write_tool_implementation(proposal)

    @workflow.signal
    async def reject_tool(self, proposal_id: str):
        """Called by CLI when the user rejects a proposal."""
        if proposal_id in self.proposals:
            self.proposals[proposal_id]["status"] = "rejected"
            self._has_updates = True
            workflow.logger.info(f"Tool rejected: {proposal_id}")

    # --- Queries (reads, no side effects) ---

    @workflow.query
    def get_approved_tools(self) -> list[dict]:
        """Returns all approved tools. Called by research workflows."""
        return [
            p for p in self.proposals.values()
            if p["status"] == "approved"
        ]

    @workflow.query
    def get_pending_proposals(self) -> list[dict]:
        """Returns all pending proposals. Used by the CLI."""
        return [
            p for p in self.proposals.values()
            if p["status"] == "pending"
        ]

    @workflow.query
    def get_all_proposals(self) -> list[dict]:
        """Returns all proposals regardless of status."""
        return list(self.proposals.values())

    @workflow.query
    def get_proposal(self, proposal_id: str) -> dict | None:
        """Get a single proposal by ID, including its implementation code."""
        return self.proposals.get(proposal_id)

    # --- Main loop ---

    @workflow.run
    async def run(self, carry_over: dict[str, dict] | None = None):
        """
        Runs forever. Periodically expires old proposals.

        Wakes on signals (new proposals, approvals) or daily to check TTLs.
        Uses continue_as_new periodically to keep history bounded, carrying
        over active proposals.
        """
        if carry_over:
            self.proposals = carry_over

        cycles = 0
        max_cycles_before_reset = 365  # continue_as_new after ~1 year

        while cycles < max_cycles_before_reset:
            cycles += 1
            self._has_updates = False

            await workflow.wait_condition(
                lambda: self._has_updates,
                timeout=timedelta(days=1),
            )

            self._expire_old_proposals()

        # Carry over active proposals into the new execution
        active = {
            pid: p for pid, p in self.proposals.items()
            if p["status"] in ("pending", "approved")
        }
        workflow.continue_as_new(args=[active])

    def _expire_old_proposals(self):
        """Mark pending proposals older than TTL as expired."""
        from datetime import datetime

        now = workflow.now()
        for pid, proposal in self.proposals.items():
            if proposal["status"] != "pending":
                continue
            proposed_at = datetime.fromisoformat(proposal["proposed_at"])
            if now - proposed_at > PROPOSAL_TTL:
                proposal["status"] = "expired"
                workflow.logger.info(
                    f"Proposal expired (15d TTL): {proposal['name']} ({pid})"
                )

