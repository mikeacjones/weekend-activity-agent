"""
Long-running workflow: manages dynamic tool proposals and approvals.

Lifecycle of a tool proposal:
  1. Research agent proposes a tool → signal arrives here
  2. Proposal stored as "pending" with a timestamp
  3. User gets notified on Slack
  4. User reviews code via CLI and approves/rejects
  5. On approval: workflow executes write_dynamic_tool activity (retryable)
  6. On next research run: research workflow queries approved tools
  7. If no response in 15 days: auto-expired

Non-blocking by design — the research workflow signals and moves on.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from activities import write_dynamic_tool

PROPOSAL_TTL = timedelta(days=15)

RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
)


@workflow.defn
class ToolRegistryWorkflow:
    """Runs indefinitely. Manages the lifecycle of dynamic tool proposals."""

    def __init__(self):
        self.proposals: dict[str, dict] = {}
        self._has_updates: bool = False
        self._pending_writes: list[str] = []

    # --- Signals (state mutations only) ---

    @workflow.signal
    async def propose_tool(self, proposal: dict):
        proposal["status"] = "pending"
        self.proposals[proposal["id"]] = proposal
        self._has_updates = True
        workflow.logger.info(f"New tool proposal: {proposal['name']} ({proposal['id']})")

    @workflow.signal
    async def approve_tool(self, proposal_id: str):
        if proposal_id not in self.proposals:
            workflow.logger.warn(f"Unknown proposal: {proposal_id}")
            return

        proposal = self.proposals[proposal_id]
        if proposal["status"] != "pending":
            workflow.logger.warn(f"Proposal {proposal_id} is {proposal['status']}, cannot approve")
            return

        proposal["status"] = "approved"
        self._pending_writes.append(proposal_id)
        self._has_updates = True
        workflow.logger.info(f"Tool approved: {proposal['name']} ({proposal_id})")

    @workflow.signal
    async def reject_tool(self, proposal_id: str):
        if proposal_id in self.proposals:
            self.proposals[proposal_id]["status"] = "rejected"
            self._has_updates = True
            workflow.logger.info(f"Tool rejected: {proposal_id}")

    # --- Queries (reads, no side effects) ---

    @workflow.query
    def get_approved_tools(self) -> list[dict]:
        return [p for p in self.proposals.values() if p["status"] == "approved"]

    @workflow.query
    def get_pending_proposals(self) -> list[dict]:
        return [p for p in self.proposals.values() if p["status"] == "pending"]

    @workflow.query
    def get_all_proposals(self) -> list[dict]:
        return list(self.proposals.values())

    @workflow.query
    def get_proposal(self, proposal_id: str) -> dict | None:
        return self.proposals.get(proposal_id)

    # --- Main loop ---

    @workflow.run
    async def run(self, carry_over: dict[str, dict] | None = None):
        if carry_over:
            self.proposals = carry_over

        cycles = 0
        max_cycles_before_reset = 365

        while cycles < max_cycles_before_reset:
            cycles += 1
            self._has_updates = False

            await workflow.wait_condition(
                lambda: self._has_updates,
                timeout=timedelta(days=1),
            )

            # Process any approved tools that need writing
            while self._pending_writes:
                proposal_id = self._pending_writes.pop(0)
                proposal = self.proposals.get(proposal_id)
                if proposal and proposal["status"] == "approved":
                    await workflow.execute_activity(
                        write_dynamic_tool,
                        args=[proposal],
                        start_to_close_timeout=timedelta(minutes=5),
                        retry_policy=RETRY,
                    )

            self._expire_old_proposals()

        active = {
            pid: p for pid, p in self.proposals.items()
            if p["status"] in ("pending", "approved")
        }
        workflow.continue_as_new(args=[active])

    def _expire_old_proposals(self):
        from datetime import datetime

        now = workflow.now()
        for pid, proposal in self.proposals.items():
            if proposal["status"] != "pending":
                continue
            proposed_at = datetime.fromisoformat(proposal["proposed_at"])
            if now - proposed_at > PROPOSAL_TTL:
                proposal["status"] = "expired"
                workflow.logger.info(f"Proposal expired (15d TTL): {proposal['name']} ({pid})")
