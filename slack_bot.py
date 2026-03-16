"""
Slack Socket Mode listener — translates Slack interactions into Temporal signals/activities.

Not a standalone process. Started by the worker alongside the Temporal polling loop.
The listener's only job is to receive Slack events and route them into the Temporal system:

- Tool approval/rejection → signals to ToolRegistryWorkflow
- "More info" requests → starts a HandleSlackInteraction workflow
- Simple reactions (interested/dismiss) → fires a one-shot activity

This keeps all execution durable and visible in the Temporal UI.
"""

import os
import re
import threading

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from temporalio.client import Client

from pathlib import Path


def create_slack_app(temporal_client: Client) -> App:
    """Create and configure the Slack app with all action handlers."""
    app = App(token=os.environ["SLACK_BOT_TOKEN"])

    # -----------------------------------------------------------------
    # Tool proposal actions → Temporal signals
    # -----------------------------------------------------------------

    @app.action(re.compile(r"^approve_tool:"))
    def handle_approve_tool(ack, body, client, action):
        ack()
        proposal_id = action["value"]
        user = body["user"]["username"]

        import asyncio
        async def _approve():
            handle = temporal_client.get_workflow_handle("tool-registry")
            proposal = await handle.query("get_proposal", proposal_id)

            if not proposal:
                client.chat_postMessage(
                    channel=body["channel"]["id"],
                    thread_ts=body["message"]["ts"],
                    text=f"Proposal `{proposal_id}` not found.",
                )
                return

            if proposal["status"] != "pending":
                client.chat_postMessage(
                    channel=body["channel"]["id"],
                    thread_ts=body["message"]["ts"],
                    text=f"Proposal is already `{proposal['status']}`.",
                )
                return

            _write_dynamic_tool(proposal)
            await handle.signal("approve_tool", proposal_id)

            client.chat_postMessage(
                channel=body["channel"]["id"],
                thread_ts=body["message"]["ts"],
                text=(
                    f":white_check_mark: *{user}* approved `{proposal['name']}`.\n"
                    f"It will be available on the next research run."
                ),
            )

        asyncio.run(_approve())

    @app.action(re.compile(r"^reject_tool:"))
    def handle_reject_tool(ack, body, client, action):
        ack()
        proposal_id = action["value"]
        user = body["user"]["username"]

        import asyncio
        async def _reject():
            handle = temporal_client.get_workflow_handle("tool-registry")
            await handle.signal("reject_tool", proposal_id)

        asyncio.run(_reject())

        client.chat_postMessage(
            channel=body["channel"]["id"],
            thread_ts=body["message"]["ts"],
            text=f":x: *{user}* rejected proposal `{proposal_id}`.",
        )

    @app.action(re.compile(r"^review_tool:"))
    def handle_review_tool(ack, body, client, action):
        ack()
        proposal_id = action["value"]

        import asyncio
        async def _review():
            handle = temporal_client.get_workflow_handle("tool-registry")
            return await handle.query("get_proposal", proposal_id)

        proposal = asyncio.run(_review())

        if not proposal:
            text = f"Proposal `{proposal_id}` not found."
        else:
            code = proposal.get("suggested_implementation", "_No implementation provided._")
            text = (
                f"*`{proposal['name']}`* — proposed implementation:\n\n"
                f"```{code}```"
            )

        client.chat_postMessage(
            channel=body["channel"]["id"],
            thread_ts=body["message"]["ts"],
            text=text,
        )

    # -----------------------------------------------------------------
    # Report interactions → Temporal activities/workflows
    # -----------------------------------------------------------------

    @app.action(re.compile(r"^interested:"))
    def handle_interested(ack, body, client, action):
        ack()
        slug = action["value"]
        user = body["user"]["username"]

        client.reactions_add(
            channel=body["channel"]["id"],
            timestamp=body["message"]["ts"],
            name="heart",
        )
        client.chat_postMessage(
            channel=body["channel"]["id"],
            thread_ts=body["message"]["ts"],
            text=f":heart: *{user}* is interested in *{_slug_to_title(slug)}*!",
        )

    @app.action(re.compile(r"^dismiss:"))
    def handle_dismiss(ack, body, client, action):
        ack()
        slug = action["value"]

        client.chat_postMessage(
            channel=body["channel"]["id"],
            thread_ts=body["message"]["ts"],
            text=f"Got it — skipping _{_slug_to_title(slug)}_ this time.",
        )

    @app.action(re.compile(r"^more_info:"))
    def handle_more_info(ack, body, client, action):
        """Route 'more info' through Temporal as a workflow for durability."""
        ack()
        slug = action["value"]
        channel = body["channel"]["id"]
        thread_ts = body["message"]["ts"]

        # Post a "thinking" message immediately via Slack
        thinking = client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f":mag: Looking up more details on _{_slug_to_title(slug)}_...",
        )

        # Extract context from the original message blocks
        original_blocks = body.get("message", {}).get("blocks", [])
        context = _extract_recommendation_context(slug, original_blocks)

        # Fire a Temporal workflow for the LLM call — gets retry, visibility, history
        import asyncio
        from workflows.slack_interaction import SlackInteractionWorkflow

        async def _start():
            await temporal_client.start_workflow(
                SlackInteractionWorkflow.run,
                args=[{
                    "type": "more_info",
                    "slug": slug,
                    "context": context,
                    "channel": channel,
                    "thinking_ts": thinking["ts"],
                    "thread_ts": thread_ts,
                }],
                id=f"slack-interaction-{slug}-{thinking['ts']}",
                task_queue="weekend-activity-agent",
            )

        asyncio.run(_start())

    # -----------------------------------------------------------------
    # Catch-all
    # -----------------------------------------------------------------

    @app.action(re.compile(r".*"))
    def handle_unknown_action(ack, body, client, action):
        ack()
        action_id = action.get("action_id", "unknown")
        client.chat_postMessage(
            channel=body["channel"]["id"],
            thread_ts=body.get("message", {}).get("ts"),
            text=f":wave: Got your click on `{action_id}`. (No specific handler registered yet.)",
        )

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slug_to_title(slug: str) -> str:
    return slug.replace("-", " ").title()


def _extract_recommendation_context(slug: str, blocks: list[dict]) -> str:
    parts = []
    capture = False

    for block in blocks:
        text = ""
        if block.get("type") == "section":
            text = block.get("text", {}).get("text", "")
        elif block.get("type") == "context":
            text = " ".join(e.get("text", "") for e in block.get("elements", []))

        if slug in text.lower().replace(" ", "-") or slug.replace("-", " ") in text.lower():
            capture = True

        if capture:
            if text:
                parts.append(text)
            if block.get("type") == "divider" and parts:
                break

    return "\n".join(parts) if parts else f"Activity: {_slug_to_title(slug)}"


def _write_dynamic_tool(proposal: dict):
    tool_dir = Path(__file__).parent / "dynamic_tools"
    tool_dir.mkdir(exist_ok=True)
    tool_file = tool_dir / f"{proposal['name']}.py"

    code = proposal.get("suggested_implementation", "")
    if not code:
        return

    header = f'"""{proposal["description"]}"""\n\n'
    if "async def run(" not in code:
        code = f"{header}{code}"

    tool_file.write_text(code)

    # Install any new dependencies
    deps = proposal.get("dependencies", [])
    if deps:
        _install_dynamic_deps(deps)


def _install_dynamic_deps(deps: list[str]):
    """Install dependencies and persist them for container restarts."""
    import subprocess

    # Install into the current environment
    subprocess.run(["uv", "pip", "install", *deps], check=True)

    # Persist to requirements file so they survive container restarts
    req_file = Path(__file__).parent / "dynamic_tools" / "requirements.txt"
    existing = set()
    if req_file.exists():
        existing = {line.strip() for line in req_file.read_text().splitlines() if line.strip()}
    existing.update(deps)
    req_file.write_text("\n".join(sorted(existing)) + "\n")


def start_socket_mode(temporal_client: Client):
    """Start the Socket Mode listener in a background thread.

    Called by the worker on startup. Runs in a daemon thread so it
    shuts down automatically when the worker exits.
    """
    slack_app = create_slack_app(temporal_client)
    handler = SocketModeHandler(slack_app, os.environ["SLACK_APP_TOKEN"])

    thread = threading.Thread(target=handler.start, daemon=True)
    thread.start()
    print("Slack Socket Mode listener started (background thread)")
    return thread
