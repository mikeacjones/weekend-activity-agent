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
    """List all tool proposals."""
    client = await get_client()
    handle = client.get_workflow_handle("tool-registry")

    if args.pending:
        proposals = await handle.query("get_pending_proposals")
        header = "Pending proposals"
    else:
        proposals = await handle.query("get_all_proposals")
        header = "All proposals"

    if not proposals:
        print(f"{header}: none")
        return

    print(f"\n{header}:")
    print("-" * 60)
    for p in proposals:
        status_icon = {
            "pending": "?",
            "approved": "+",
            "rejected": "x",
            "expired": "~",
        }.get(p["status"], " ")
        print(f"  [{status_icon}] {p['id']}  {p['name']}")
        print(f"      {p['description']}")
        print(f"      Status: {p['status']} | Proposed: {p['proposed_at']}")
        print()


async def cmd_review(args):
    """Show the full details and implementation code of a proposal."""
    client = await get_client()
    handle = client.get_workflow_handle("tool-registry")
    proposal = await handle.query("get_proposal", args.proposal_id)

    if not proposal:
        print(f"Proposal '{args.proposal_id}' not found.")
        sys.exit(1)

    print(f"\n{'=' * 60}")
    print(f"Proposal: {proposal['name']} ({proposal['id']})")
    print(f"Status:   {proposal['status']}")
    print(f"Proposed: {proposal['proposed_at']}")
    print(f"{'=' * 60}")
    print(f"\nDescription:\n  {proposal['description']}")
    print(f"\nRationale:\n  {proposal['rationale']}")

    if proposal.get("input_schema"):
        print(f"\nInput Schema:")
        print(json.dumps(proposal["input_schema"], indent=2))

    if proposal.get("suggested_implementation"):
        print(f"\nImplementation Code:")
        print("-" * 60)
        print(proposal["suggested_implementation"])
        print("-" * 60)

    if proposal["status"] == "pending":
        print(f"\nTo approve:  python cli.py approve {proposal['id']}")
        print(f"To reject:   python cli.py reject {proposal['id']}")


async def cmd_approve(args):
    """Approve a pending tool proposal."""
    client = await get_client()
    handle = client.get_workflow_handle("tool-registry")

    # First, review what we're approving
    proposal = await handle.query("get_proposal", args.proposal_id)
    if not proposal:
        print(f"Proposal '{args.proposal_id}' not found.")
        sys.exit(1)

    if proposal["status"] != "pending":
        print(f"Proposal is '{proposal['status']}', not pending.")
        sys.exit(1)

    # Show a summary and confirm
    print(f"\nApproving: {proposal['name']}")
    print(f"  {proposal['description']}")

    if not args.yes:
        confirm = input("\nProceed? [y/N] ").strip().lower()
        if confirm != "y":
            print("Cancelled.")
            return

    # Signal the registry — it handles writing the tool and installing deps via activity
    await handle.signal("approve_tool", args.proposal_id)
    print(f"Tool '{proposal['name']}' approved. The registry workflow will write the tool and install dependencies.")


async def cmd_reject(args):
    """Reject a pending tool proposal."""
    client = await get_client()
    handle = client.get_workflow_handle("tool-registry")
    await handle.signal("reject_tool", args.proposal_id)
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
