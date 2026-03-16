"""Temporal activities — thin wrappers around LLM calls, tool execution, and notifications."""

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

from anthropic import Anthropic
from slack_sdk import WebClient
from temporalio import activity

from config import Location, Preferences, build_system_prompt
from tools import TOOL_DEFINITIONS, dispatch_tool

anthropic_client = Anthropic()
slack_client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL_ID", "")
LLM_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def serialize_response(response) -> dict:
    """Convert Anthropic response to a JSON-serializable dict for Temporal."""
    tool_calls = []
    raw_content = []

    for block in response.content:
        if block.type == "tool_use":
            tool_calls.append({
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })
            raw_content.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })
        elif block.type == "text":
            raw_content.append({
                "type": "text",
                "text": block.text,
            })

    return {
        "stop_reason": response.stop_reason,
        "tool_calls": tool_calls,
        "raw_content": raw_content,
    }


# ---------------------------------------------------------------------------
# Sync activities — Temporal runs these in a thread pool automatically
# ---------------------------------------------------------------------------


@activity.defn
def call_llm(
    system_prompt: str,
    messages: list[dict],
    tool_definitions: list[dict],
) -> dict:
    """Single LLM inference step. Result is persisted in Temporal history."""
    activity.heartbeat("Calling LLM")
    response = anthropic_client.messages.create(
        model=LLM_MODEL,
        max_tokens=4096,
        system=system_prompt,
        tools=tool_definitions,
        messages=messages,
    )
    return serialize_response(response)


@activity.defn
def call_llm_simple(prompt: str) -> str:
    """Single LLM call without tools. For follow-up responses, summaries, etc."""
    activity.heartbeat("Calling LLM")
    response = anthropic_client.messages.create(
        model=LLM_MODEL,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


REPORT_SYSTEM_PROMPT = """\
You produce Slack messages using Block Kit for a weekend activity recommendation report.

You MUST return valid JSON with two keys:
- "blocks": array of Slack Block Kit blocks
- "text": plain text fallback (for notifications / non-Block Kit clients)

AVAILABLE BLOCK KIT ELEMENTS:
- header: {"type": "header", "text": {"type": "plain_text", "text": "..."}}
- section with markdown: {"type": "section", "text": {"type": "mrkdwn", "text": "..."}}
- section with accessory image: {"type": "section", "text": {"type": "mrkdwn", "text": "..."}, \
"accessory": {"type": "image", "image_url": "...", "alt_text": "..."}}
- divider: {"type": "divider"}
- context (small metadata): {"type": "context", "elements": [{"type": "mrkdwn", "text": "..."}]}
- actions (button row): {"type": "actions", "elements": [<buttons>]}
- button: {"type": "button", "text": {"type": "plain_text", "text": "..."}, \
"action_id": "<action>:<slug>", "value": "<slug>", "style": "primary"|"danger"}

INTERACTIVE BUTTON PATTERNS (use these action_id prefixes):
- "interested:<slug>" — "I'm in!" / "Save this" (style: "primary")
- "more_info:<slug>" — "Tell me more" (no style, default)
- "dismiss:<slug>" — "Pass" / "Not this time" (no style)

Where <slug> is a short, lowercase, hyphenated identifier derived from the activity title \
(e.g., "mothman-market", "blood-mountain-hike").

FORMATTING GUIDELINES:
- Use Slack mrkdwn: *bold*, _italic_, <url|link text>, :emoji_name:
- Weather emoji: :sunny: :partly_sunny: :cloud: :rain_cloud: :thunder_cloud_and_rain: :snowflake:
- Lead with a weather summary header
- Each recommendation gets its own section + action buttons
- Include a context block under each recommendation with: date, location, drive time
- Keep it conversational and fun — this is weekend planning, not a briefing
- For day trips, call out the combo suggestion prominently
- If weather is bad for outdoor plans, acknowledge it and pivot to indoor options
- End with a brief "also on the radar" context block for weekday alerts if any
- Max 50 blocks (Slack limit), aim for under 30"""


@activity.defn
def compile_report(
    findings: list[dict],
    weather_summary: str,
) -> dict:
    """Synthesize the week's research into a Slack Block Kit report.

    Returns {"blocks": [...], "text": "..."}.
    """
    activity.heartbeat("Compiling report")
    response = anthropic_client.messages.create(
        model=LLM_MODEL,
        max_tokens=8192,
        system=REPORT_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"""Here are this week's research findings and weekend weather forecast.

WEATHER FORECAST:
{weather_summary}

FINDINGS:
{json.dumps(findings, indent=2)}

Produce the Slack Block Kit message. Return ONLY valid JSON, no markdown fences.""",
        }],
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    report = json.loads(raw)

    if "blocks" not in report or not isinstance(report["blocks"], list):
        raise ValueError("LLM response missing 'blocks' array")
    if "text" not in report:
        report["text"] = "Your weekend recommendations are here!"

    return report


@activity.defn
def send_slack_message(
    channel: str,
    text: str,
    blocks: list[dict] | None = None,
    update_ts: str | None = None,
    thread_ts: str | None = None,
) -> dict:
    """Send, update, or reply in a thread."""
    activity.heartbeat("Sending to Slack")

    if update_ts:
        result = slack_client.chat_update(
            channel=channel or SLACK_CHANNEL,
            ts=update_ts,
            text=text,
            blocks=blocks or [],
        )
    else:
        kwargs = {
            "channel": channel or SLACK_CHANNEL,
            "text": text,
        }
        if blocks:
            kwargs["blocks"] = blocks
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        result = slack_client.chat_postMessage(**kwargs)

    return {
        "ok": result["ok"],
        "ts": result["ts"],
        "channel": result["channel"],
    }


@activity.defn
def notify_tool_proposal(proposal: dict):
    """Send a tool proposal notification to Slack with interactive buttons."""
    activity.heartbeat("Notifying about tool proposal")

    proposal_id = proposal["id"]
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": ":hammer_and_wrench: New Tool Proposal"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*`{proposal['name']}`*\n\n"
                    f"{proposal['description']}\n\n"
                    f"*Why:* {proposal['rationale']}"
                ),
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"Proposal ID: `{proposal_id}` | "
                        f"Review code: `python cli.py review {proposal_id}` | "
                        f"Auto-expires in 15 days"
                    ),
                },
            ],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":mag: Review Code"},
                    "action_id": f"review_tool:{proposal_id}",
                    "value": proposal_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":white_check_mark: Approve"},
                    "action_id": f"approve_tool:{proposal_id}",
                    "value": proposal_id,
                    "style": "primary",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":x: Reject"},
                    "action_id": f"reject_tool:{proposal_id}",
                    "value": proposal_id,
                    "style": "danger",
                },
            ],
        },
    ]

    result = slack_client.chat_postMessage(
        channel=SLACK_CHANNEL,
        text=f"New tool proposal: {proposal['name']}",
        blocks=blocks,
    )
    return {"ts": result["ts"], "channel": result["channel"]}


@activity.defn
def write_dynamic_tool(proposal: dict):
    """Write an approved tool's code to dynamic_tools/ and install its dependencies."""
    activity.heartbeat(f"Writing tool: {proposal['name']}")

    tool_dir = Path(__file__).parent / "dynamic_tools"
    tool_dir.mkdir(exist_ok=True)

    code = proposal.get("suggested_implementation", "")
    if code:
        header = f'"""{proposal["description"]}"""\n\n'
        if "async def run(" not in code:
            code = f"{header}{code}"
        tool_file = tool_dir / f"{proposal['name']}.py"
        tool_file.write_text(code)

    deps = proposal.get("dependencies", [])
    if deps:
        activity.heartbeat(f"Installing deps: {deps}")
        subprocess.run(["uv", "pip", "install", *deps], check=True)
        req_file = tool_dir / "requirements.txt"
        existing = set()
        if req_file.exists():
            existing = {line.strip() for line in req_file.read_text().splitlines() if line.strip()}
        existing.update(deps)
        req_file.write_text("\n".join(sorted(existing)) + "\n")


@activity.defn
def discuss_tool_proposal(
    proposal: dict,
    discussion_history: list[dict],
    user_message: str,
    channel: str,
    thread_ts: str,
) -> str:
    """Discuss a tool proposal with the user in a Slack thread."""
    activity.heartbeat("Discussing tool proposal")

    code = proposal.get("suggested_implementation", "No code provided")
    deps = proposal.get("dependencies", [])

    system = (
        "You are reviewing a tool proposal for an automated weekend activity research agent. "
        "Help the user understand the tool, answer questions about the implementation, "
        "and flag any concerns about security, reliability, or dependencies.\n\n"
        f"TOOL NAME: {proposal['name']}\n"
        f"DESCRIPTION: {proposal['description']}\n"
        f"RATIONALE: {proposal.get('rationale', 'N/A')}\n"
        f"DEPENDENCIES: {', '.join(deps) if deps else 'None'}\n"
        f"IMPLEMENTATION:\n```python\n{code}\n```"
    )

    messages = []
    for entry in discussion_history:
        messages.append({"role": "user", "content": entry["user_message"]})
        messages.append({"role": "assistant", "content": entry["response"]})
    messages.append({"role": "user", "content": user_message})

    response = anthropic_client.messages.create(
        model=LLM_MODEL,
        max_tokens=2048,
        system=system,
        messages=messages,
    )
    reply_text = response.content[0].text

    slack_client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=reply_text,
    )

    return reply_text


# ---------------------------------------------------------------------------
# Async activities — these use async I/O (httpx) and belong on the event loop
# ---------------------------------------------------------------------------


@activity.defn
async def execute_tool(name: str, tool_input: dict) -> str:
    """Execute a single tool call. Independently retryable."""
    activity.heartbeat(f"Executing: {name}")
    return await dispatch_tool(name, tool_input)


@activity.defn
async def get_current_weather_summary() -> str:
    """Fetch the current weather forecast and return a summary string."""
    activity.heartbeat("Fetching weather")
    from tools import get_weather_forecast
    return await get_weather_forecast()
