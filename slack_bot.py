"""
Slack Socket Mode listener — translates Slack interactions into Temporal signals/activities.

Not a standalone process. Started by the worker alongside the Temporal polling loop.
The listener's only job is to receive Slack events and route them into the Temporal system:

- @mention → starts a ConversationWorkflow
- Thread replies → signals to ConversationWorkflow or ToolProposalWorkflow
- Tool approval/rejection → signals to ToolProposalWorkflow
- "More info" requests → starts a SlackInteractionWorkflow
- Simple reactions (interested/dismiss) → fires a one-shot activity

This keeps all execution durable and visible in the Temporal UI.
"""

import os
import re
import threading

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from temporalio.client import Client


def create_slack_app(temporal_client: Client) -> App:
    """Create and configure the Slack app with all action handlers."""
    app = App(token=os.environ["SLACK_BOT_TOKEN"])

    # Get the bot's own user ID to filter self-mentions in threads
    auth = app.client.auth_test()
    bot_user_id = auth["user_id"]

    # -----------------------------------------------------------------
    # Tool proposal actions → Temporal signals to child workflows
    # -----------------------------------------------------------------

    @app.action(re.compile(r"^approve_tool:"))
    def handle_approve_tool(ack, body, client, action):
        ack()
        proposal_id = action["value"]
        user = body["user"]["username"]

        import asyncio
        async def _approve():
            handle = temporal_client.get_workflow_handle(f"tool-proposal-{proposal_id}")
            status = await handle.query("get_status")

            if status["status"] != "pending":
                client.chat_postMessage(
                    channel=body["channel"]["id"],
                    thread_ts=body["message"]["ts"],
                    text=f"Proposal is already `{status['status']}`.",
                )
                return

            await handle.signal("approve", user)

            client.chat_postMessage(
                channel=body["channel"]["id"],
                thread_ts=body["message"]["ts"],
                text=(
                    f":white_check_mark: *{user}* approved `{status['proposal']['name']}`.\n"
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
            handle = temporal_client.get_workflow_handle(f"tool-proposal-{proposal_id}")
            await handle.signal("reject", user)

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
            handle = temporal_client.get_workflow_handle(f"tool-proposal-{proposal_id}")
            return await handle.query("get_proposal")

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
    # @mention → start a conversation workflow
    # -----------------------------------------------------------------

    @app.event("app_mention")
    def handle_app_mention(event, client):
        channel = event["channel"]
        thread_ts = event.get("thread_ts")
        event_ts = event["ts"]
        user = event.get("user", "unknown")

        # Strip the bot mention from the message text
        text = re.sub(rf"<@{bot_user_id}>\s*", "", event.get("text", "")).strip()
        if not text:
            text = "Hey! What can you help me with?"

        # If mentioned in an existing thread, route to that conversation
        if thread_ts:
            import asyncio
            async def _signal():
                handle = temporal_client.get_workflow_handle(f"conversation-{thread_ts}")
                await handle.signal("message", {"user": user, "text": text})
            try:
                asyncio.run(_signal())
            except Exception:
                pass
            return

        # Start a new conversation workflow — the mention message is the thread anchor
        import asyncio
        from workflows.conversation import ConversationWorkflow

        async def _start():
            await temporal_client.start_workflow(
                ConversationWorkflow.run,
                args=[channel, event_ts, text],
                id=f"conversation-{event_ts}",
                task_queue="weekend-activity-agent",
            )

        asyncio.run(_start())

    # -----------------------------------------------------------------
    # Thread replies → route to conversation or proposal workflows
    # -----------------------------------------------------------------

    @app.event("message")
    def handle_thread_reply(event, client):
        # Only process threaded replies
        thread_ts = event.get("thread_ts")
        if not thread_ts:
            return

        # Skip bot messages to avoid loops
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return

        channel = event["channel"]
        user = event.get("user", "unknown")
        text = event.get("text", "")

        # Fetch the parent message to determine thread type
        parent = client.conversations_history(
            channel=channel,
            latest=thread_ts,
            inclusive=True,
            limit=1,
        )
        if not parent["messages"]:
            return

        parent_msg = parent["messages"][0]
        parent_text = parent_msg.get("text", "")

        import asyncio

        # Tool proposal thread
        if "New tool proposal:" in parent_text:
            proposal_id = None
            for block in parent_msg.get("blocks", []):
                if block.get("type") == "context":
                    for el in block.get("elements", []):
                        match = re.search(r"Proposal ID: `([^`]+)`", el.get("text", ""))
                        if match:
                            proposal_id = match.group(1)
                            break
                if proposal_id:
                    break

            if proposal_id:
                async def _discuss_proposal():
                    handle = temporal_client.get_workflow_handle(f"tool-proposal-{proposal_id}")
                    await handle.signal("discuss", {"user": user, "text": text})
                try:
                    asyncio.run(_discuss_proposal())
                except Exception:
                    pass
            return

        # Conversation thread (bot was mentioned in the parent)
        if f"<@{bot_user_id}>" in parent_text:
            async def _discuss_conversation():
                handle = temporal_client.get_workflow_handle(f"conversation-{thread_ts}")
                await handle.signal("message", {"user": user, "text": text})
            try:
                asyncio.run(_discuss_conversation())
            except Exception:
                pass

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
