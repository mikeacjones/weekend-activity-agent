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
from proposal_utils import normalize_proposal, normalize_tool_summary

anthropic_client = Anthropic()
slack_client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL_ID", "")
LLM_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")


def _load_rejected_tool_entries_from_disk() -> list[dict]:
    """Read rejected tool entries from disk and normalize them."""
    rejection_file = Path(__file__).parent / "dynamic_tools" / "rejected_tools.json"
    if not rejection_file.exists():
        return []
    try:
        entries = json.loads(rejection_file.read_text())
        if not isinstance(entries, list):
            return []
        recovered: list[dict] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            normalized = normalize_tool_summary(entry)
            if normalized["name"] or normalized["capability_key"]:
                recovered.append({
                    **normalized,
                    "rejected_at": str(entry.get("rejected_at", "")),
                })
        return recovered
    except json.JSONDecodeError:
        return []


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
def call_llm_for_report(findings: list[dict], weather_summary: str) -> str:
    """Call the LLM to generate the weekly Slack report. Returns raw response text."""
    activity.heartbeat("Calling LLM for report")
    response = anthropic_client.messages.create(
        model=LLM_MODEL,
        max_tokens=16000,
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

    if response.stop_reason == "max_tokens":
        raise ValueError(
            f"LLM response was truncated (max_tokens reached). "
            f"Response was {len(response.content[0].text)} chars. "
            "Increase max_tokens or reduce findings input."
        )

    return response.content[0].text


@activity.defn
def parse_report(raw: str) -> dict:
    """Parse and validate the LLM-generated Slack Block Kit JSON."""
    activity.heartbeat("Parsing report")
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    report = json.loads(text)

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
    reference_urls = proposal.get("reference_urls", [])
    reference_text = (
        "\n".join(f"- <{url}|Reference {idx + 1}>"
                  for idx, url in enumerate(reference_urls[:3]))
        if reference_urls
        else "_No references provided_"
    )
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
                    f"*Capability key:* `{proposal.get('capability_key', proposal['name'])}`\n"
                    f"*Why:* {proposal['rationale']}"
                ),
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*References:*\n"
                    f"{reference_text}"
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
                        f"Auto-expires in 15 days"
                        + (
                            " | :key: Requires API keys: "
                            + ", ".join(f"`{s['name']}`" for s in proposal.get("required_secrets", []))
                            if proposal.get("required_secrets")
                            else ""
                        )
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
    normalized = normalize_proposal(proposal)

    # Write metadata for registry recovery on restart
    meta = {
        "name": normalized["name"],
        "capability_key": normalized["capability_key"],
        "description": normalized["description"],
        "reference_urls": normalized.get("reference_urls", []),
        "input_schema": normalized.get("input_schema", {
            "type": "object",
            "properties": {},
        }),
    }
    meta_file = tool_dir / f"{normalized['name']}.json"
    meta_file.write_text(json.dumps(meta, indent=2))

    code = normalized.get("suggested_implementation", "")
    if code:
        header = f'"""{normalized["description"]}"""\n\n'
        if "async def run(" not in code:
            code = f"{header}{code}"
        tool_file = tool_dir / f"{normalized['name']}.py"
        tool_file.write_text(code)

    deps = normalized.get("dependencies", [])
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
def recover_approved_tools() -> list[dict]:
    """Scan dynamic_tools/ for metadata files to rebuild the approved tools list."""
    activity.heartbeat("Recovering tools from disk")
    tool_dir = Path(__file__).parent / "dynamic_tools"
    if not tool_dir.exists():
        return []

    tools = []
    for meta_file in sorted(tool_dir.glob("*.json")):
        if meta_file.name == "rejected_tools.json":
            continue
        try:
            meta = json.loads(meta_file.read_text())
            if isinstance(meta, dict) and meta.get("name"):
                tools.append(normalize_tool_summary(meta))
        except (json.JSONDecodeError, KeyError):
            continue

    # Also pick up .py files that don't have a metadata sidecar
    for py_file in sorted(tool_dir.glob("*.py")):
        name = py_file.stem
        if name.startswith("_") or any(t["name"] == name for t in tools):
            continue
        # Extract description from module docstring
        content = py_file.read_text()
        desc = ""
        if content.startswith('"""'):
            end = content.find('"""', 3)
            if end > 0:
                desc = content[3:end].strip()
        tools.append({
            "name": name,
            "capability_key": name,
            "description": desc or f"Dynamic tool: {name}",
            "input_schema": {"type": "object", "properties": {}},
        })

    return tools


@activity.defn
def record_tool_rejection(proposal: dict):
    """Persist a rejected tool name to disk so the agent won't re-propose it."""
    activity.heartbeat(f"Recording rejection: {proposal['name']}")
    tool_dir = Path(__file__).parent / "dynamic_tools"
    tool_dir.mkdir(exist_ok=True)
    rejection_file = tool_dir / "rejected_tools.json"
    normalized = normalize_proposal(proposal)

    existing: list[dict] = []
    if rejection_file.exists():
        try:
            existing = json.loads(rejection_file.read_text())
        except json.JSONDecodeError:
            pass

    existing_keys = {
        normalize_tool_summary(r).get("capability_key", "")
        for r in existing
        if isinstance(r, dict)
    }
    if normalized["capability_key"] not in existing_keys:
        existing.append({
            "name": normalized["name"],
            "capability_key": normalized["capability_key"],
            "description": normalized.get("description", ""),
            "rejected_at": datetime.utcnow().isoformat(),
        })
        rejection_file.write_text(json.dumps(existing, indent=2))


@activity.defn
def recover_rejected_tool_entries() -> list[dict]:
    """Load persisted rejected tools with names and capability keys."""
    activity.heartbeat("Recovering rejected tool entries")
    return _load_rejected_tool_entries_from_disk()


@activity.defn
def recover_rejected_tools() -> list[str]:
    """Load persisted rejected tool names from disk."""
    activity.heartbeat("Recovering rejected tool names")
    return [
        entry["name"]
        for entry in _load_rejected_tool_entries_from_disk()
        if entry.get("name")
    ]


@activity.defn
def save_tool_secrets(secrets: dict[str, str]):
    """Encrypt and save secrets provided via Slack modal."""
    activity.heartbeat("Saving secrets")
    from secrets_store import save_secret
    for name, value in secrets.items():
        save_secret(name, value)


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
        "IMPORTANT: You are responding in Slack. Use Slack mrkdwn formatting:\n"
        "- Bold: *bold* (single asterisks, NOT **double**)\n"
        "- Italic: _italic_ (NOT *single*)\n"
        "- Links: <https://example.com|link text> (NOT [text](url))\n"
        "- Code: `inline` or ```block```\n"
        "- Never use **double asterisks** or [markdown links](url)\n\n"
        f"TOOL NAME: {proposal['name']}\n"
        f"DESCRIPTION: {proposal['description']}\n"
        f"RATIONALE: {proposal.get('rationale', 'N/A')}\n"
        f"DEPENDENCIES: {', '.join(deps) if deps else 'None'}\n"
        f"IMPLEMENTATION:\n```\n{code}\n```"
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
    """Legacy two-arg dispatcher. Kept registered for one deploy cycle so
    workflows mid-loop during deploy don't break on replay; new workflows
    use the dynamic activity in `agent_harness.dynamic_executor` instead.
    Remove once all in-flight workflows have drained.
    """
    activity.heartbeat(f"Executing (legacy path): {name}")
    import tool_impls
    impl = tool_impls.STATIC_IMPLS.get(name)
    if impl is not None:
        return await impl(tool_input)
    return await tool_impls.dispatch_dynamic_tool(name, tool_input)


@activity.defn
async def get_current_weather_summary() -> str:
    """Fetch the current weather forecast and return a summary string."""
    activity.heartbeat("Fetching weather")
    import tool_impls
    return await tool_impls.get_weather_forecast()
