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
    from activities import recover_approved_tools, recover_rejected_tool_entries
    from proposal_utils import normalize_identifier, normalize_proposal, normalize_tool_summary

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
        self.approved_capability_keys: list[str] = []
        self.rejected_tools: list[dict] = []
        self.rejected_tool_names: list[str] = []
        self.rejected_capability_keys: list[str] = []
        self._pending_proposals: list[dict] = []
        self._has_updates: bool = False

    # --- Signals ---

    @workflow.signal
    async def propose_tool(self, proposal: dict):
        normalized = normalize_proposal(proposal)
        pending_names = {p["name"] for p in self._pending_proposals}
        pending_capability_keys = {p["capability_key"] for p in self._pending_proposals}
        approved_names = {tool["name"] for tool in self.approved_tools}
        rejected_names = {tool["name"] for tool in self.rejected_tools}
        if (
            normalized["name"] in approved_names
            or normalized["capability_key"] in set(self.approved_capability_keys)
            or normalized["name"] in rejected_names
            or normalized["capability_key"] in set(self.rejected_capability_keys)
            or normalized["name"] in pending_names
            or normalized["capability_key"] in pending_capability_keys
        ):
            workflow.logger.info(
                "Ignoring duplicate or rejected tool proposal: "
                f"{normalized['capability_key']}"
            )
            return
        self._pending_proposals.append(normalized)
        self._has_updates = True
        workflow.logger.info(
            f"New tool proposal: {normalized['name']} ({normalized['id']})"
        )

    @workflow.signal
    async def tool_approved(self, tool: dict):
        normalized = normalize_tool_summary(tool)
        if normalized["name"] and all(
            existing["name"] != normalized["name"] for existing in self.approved_tools
        ):
            self.approved_tools.append(normalized)
        if (
            normalized["capability_key"]
            and normalized["capability_key"] not in self.approved_capability_keys
        ):
            self.approved_capability_keys.append(normalized["capability_key"])
        self._has_updates = True
        workflow.logger.info(f"Tool registered: {normalized['name']}")

    @workflow.signal
    async def tool_rejected(self, tool: str | dict):
        if isinstance(tool, dict):
            normalized = normalize_tool_summary(tool)
        else:
            name = normalize_identifier(str(tool))
            normalized = {
                "name": name,
                "capability_key": name,
                "description": "",
                "input_schema": {"type": "object", "properties": {}},
            }
        if normalized["name"] and normalized["name"] not in self.rejected_tool_names:
            self.rejected_tool_names.append(normalized["name"])
        if (
            normalized["capability_key"]
            and normalized["capability_key"] not in self.rejected_capability_keys
        ):
            self.rejected_capability_keys.append(normalized["capability_key"])
        if normalized["capability_key"] and all(
            existing["capability_key"] != normalized["capability_key"]
            for existing in self.rejected_tools
        ):
            self.rejected_tools.append(normalized)
        workflow.logger.info(f"Tool rejected: {normalized['capability_key']}")

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
        rejected_carry_over: list[dict] | list[str] | None = None,
    ):
        if carry_over:
            self.approved_tools = [normalize_tool_summary(tool) for tool in carry_over]
            self.approved_capability_keys = [
                tool["capability_key"]
                for tool in self.approved_tools
                if tool["capability_key"]
            ]
        else:
            recovered = await workflow.execute_activity(
                recover_approved_tools,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RETRY,
            )
            if recovered:
                self.approved_tools = [normalize_tool_summary(tool) for tool in recovered]
                self.approved_capability_keys = [
                    tool["capability_key"]
                    for tool in self.approved_tools
                    if tool["capability_key"]
                ]
                workflow.logger.info(f"Recovered {len(recovered)} tools from disk")

        if rejected_carry_over:
            if all(isinstance(entry, dict) for entry in rejected_carry_over):
                self.rejected_tools = [
                    normalize_tool_summary(entry) for entry in rejected_carry_over
                ]
            else:
                self.rejected_tools = [
                    {
                        "name": normalize_identifier(str(name)),
                        "capability_key": normalize_identifier(str(name)),
                        "description": "",
                        "input_schema": {"type": "object", "properties": {}},
                    }
                    for name in rejected_carry_over
                ]
            self.rejected_tool_names = [
                entry["name"] for entry in self.rejected_tools if entry["name"]
            ]
            self.rejected_capability_keys = [
                entry["capability_key"]
                for entry in self.rejected_tools
                if entry["capability_key"]
            ]
        else:
            rejected = await workflow.execute_activity(
                recover_rejected_tool_entries,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RETRY,
            )
            if rejected:
                self.rejected_tools = [normalize_tool_summary(entry) for entry in rejected]
                self.rejected_tool_names = [
                    entry["name"] for entry in self.rejected_tools if entry["name"]
                ]
                self.rejected_capability_keys = [
                    entry["capability_key"]
                    for entry in self.rejected_tools
                    if entry["capability_key"]
                ]
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

        workflow.continue_as_new(args=[self.approved_tools, self.rejected_tools])
