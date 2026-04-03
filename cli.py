"""CLI for interacting with the running agent — review proposals, approve tools, check status."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from temporalio.client import Client

TASK_QUEUE = "weekend-activity-agent"


async def get_client() -> Client:
    load_dotenv()
    address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "weekend-activity-agent")
    return await Client.connect(address, namespace=namespace)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


async def cmd_status(args):
    """Show the current status of the weekly research workflow."""
    client = await get_client()
    handle = client.get_workflow_handle("weekly-events-reynoldstown")
    status = await handle.query("get_status")
    print(f"Status:   {status['status']}")
    print(f"Findings: {status['findings_count']}")


async def cmd_proposals(args):
    """List all tool proposals by querying Temporal's visibility API."""
    client = await get_client()

    query = "WorkflowType='ToolProposalWorkflow'"
    if args.pending:
        query += " AND ExecutionStatus='Running'"

    proposals = []
    async for workflow in client.list_workflows(query=query):
        try:
            handle = client.get_workflow_handle(workflow.id)
            status = await handle.query("get_status")
            proposals.append(status)
        except Exception:
            proposals.append({
                "proposal": {"id": workflow.id.replace("tool-proposal-", ""), "name": "?"},
                "status": str(workflow.status),
            })

    if not proposals:
        print("No proposals found.")
        return

    header = "Pending proposals" if args.pending else "All proposals"
    print(f"\n{header}:")
    print("-" * 60)
    for p in proposals:
        proposal = p.get("proposal", {})
        status_icon = {
            "pending": "?",
            "approved": "+",
            "rejected": "x",
        }.get(p["status"], " ")
        print(f"  [{status_icon}] {proposal.get('id', '?')}  {proposal.get('name', '?')}")
        print(f"      {proposal.get('description', '')}")
        print(f"      Status: {p['status']}")
        if p.get("discussion_count"):
            print(f"      Discussion messages: {p['discussion_count']}")
        print()


async def cmd_review(args):
    """Show the full details and implementation code of a proposal."""
    client = await get_client()
    handle = client.get_workflow_handle(f"tool-proposal-{args.proposal_id}")

    try:
        proposal = await handle.query("get_proposal")
    except Exception:
        print(f"Proposal '{args.proposal_id}' not found.")
        sys.exit(1)

    print(f"\n{'=' * 60}")
    print(f"Proposal: {proposal['name']} ({proposal.get('id', args.proposal_id)})")
    print(f"Status:   {proposal['status']}")
    print(f"{'=' * 60}")
    print(f"\nDescription:\n  {proposal['description']}")
    print(f"\nCapability Key:\n  {proposal.get('capability_key', proposal['name'])}")
    print(f"\nRationale:\n  {proposal.get('rationale', 'N/A')}")

    if proposal.get("reference_urls"):
        print("\nReferences:")
        for url in proposal["reference_urls"]:
            print(f"  - {url}")

    if proposal.get("input_schema"):
        print(f"\nInput Schema:")
        print(json.dumps(proposal["input_schema"], indent=2))

    if proposal.get("suggested_implementation"):
        print(f"\nImplementation Code:")
        print("-" * 60)
        print(proposal["suggested_implementation"])
        print("-" * 60)

    if proposal.get("discussion"):
        print(f"\nDiscussion ({len(proposal['discussion'])} messages):")
        print("-" * 60)
        for entry in proposal["discussion"]:
            print(f"  User: {entry['user_message']}")
            print(f"  Agent: {entry['response'][:200]}...")
            print()

    if proposal["status"] == "pending":
        print(f"\nTo approve:  python cli.py approve {args.proposal_id}")
        print(f"To reject:   python cli.py reject {args.proposal_id}")


async def cmd_approve(args):
    """Approve a pending tool proposal."""
    client = await get_client()
    handle = client.get_workflow_handle(f"tool-proposal-{args.proposal_id}")

    try:
        status = await handle.query("get_status")
    except Exception:
        print(f"Proposal '{args.proposal_id}' not found.")
        sys.exit(1)

    if status["status"] != "pending":
        print(f"Proposal is '{status['status']}', not pending.")
        sys.exit(1)

    proposal = status["proposal"]
    print(f"\nApproving: {proposal['name']}")
    print(f"  {proposal['description']}")

    if not args.yes:
        confirm = input("\nProceed? [y/N] ").strip().lower()
        if confirm != "y":
            print("Cancelled.")
            return

    await handle.signal("approve", "cli-user")
    print(f"Tool '{proposal['name']}' approved.")


async def cmd_reject(args):
    """Reject a pending tool proposal."""
    client = await get_client()
    handle = client.get_workflow_handle(f"tool-proposal-{args.proposal_id}")
    await handle.signal("reject", "cli-user")
    print(f"Proposal '{args.proposal_id}' rejected.")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Weekend Activity Agent CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  python cli.py status                    Show agent status
  python cli.py proposals --pending       List pending tool proposals
  python cli.py review abc123             Review a proposal's code
  python cli.py approve abc123            Approve a proposal
  python cli.py reject abc123             Reject a proposal
""",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # status
    sub.add_parser("status", help="Show weekly research workflow status")

    # proposals
    p_prop = sub.add_parser("proposals", help="List tool proposals")
    p_prop.add_argument("--pending", action="store_true", help="Show only pending proposals")

    # review
    p_review = sub.add_parser("review", help="Review a proposal's implementation code")
    p_review.add_argument("proposal_id", help="Proposal ID")

    # approve
    p_approve = sub.add_parser("approve", help="Approve a tool proposal")
    p_approve.add_argument("proposal_id", help="Proposal ID")
    p_approve.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")

    # reject
    p_reject = sub.add_parser("reject", help="Reject a tool proposal")
    p_reject.add_argument("proposal_id", help="Proposal ID")

    args = parser.parse_args()

    cmd_map = {
        "status": cmd_status,
        "proposals": cmd_proposals,
        "review": cmd_review,
        "approve": cmd_approve,
        "reject": cmd_reject,
    }
    asyncio.run(cmd_map[args.command](args))


if __name__ == "__main__":
    main()
