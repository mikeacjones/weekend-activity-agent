"""Static tool registry — workflow-safe metadata only.

Each entry is a `ToolDef` from `agent_harness.tooldef`. Implementations live
in `tool_impls.py` and are dispatched by the dynamic-activity executor in
`agent_harness.dynamic_executor`.

Two exceptions: `propose_new_tool` is a `kind="interaction"` tool whose body
runs inline in workflow context (it signals the long-running tool-registry
workflow rather than running an activity).

This module imports stdlib + temporalio.workflow only — workflows can
import it without `imports_passed_through`.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow

from agent_harness.tooldef import ToolDef
from proposal_utils import normalize_proposal, validate_proposal


# ---------------------------------------------------------------------------
# Tool schemas (Anthropic input_schema dicts)
# ---------------------------------------------------------------------------

_SEARCH_EVENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Search query. Be specific — include neighborhood names, "
                "dates, and event types. Example: 'Reynoldstown pop-up market "
                "March 2026' or 'Atlanta drag brunch this weekend'"
            ),
        },
    },
    "required": ["query"],
}

_SEARCH_OUTDOORS_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Search query. Example: 'best wildflower hikes north georgia "
                "march' or 'kayaking Chattahoochee spring conditions'"
            ),
        },
    },
    "required": ["query"],
}

_GET_WEATHER_SCHEMA = {
    "type": "object",
    "properties": {
        "lat": {
            "type": "number",
            "description": "Latitude. Defaults to Atlanta (33.749) if omitted.",
        },
        "lon": {
            "type": "number",
            "description": "Longitude. Defaults to Atlanta (-84.358) if omitted.",
        },
    },
    "required": [],
}

_SAVE_RECOMMENDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Activity name"},
        "category": {
            "type": "string",
            "enum": [
                "local_event",
                "food_and_drink",
                "outdoor_adventure",
                "arts_and_culture",
                "seasonal_unique",
                "weekday_alert",
            ],
        },
        "date": {"type": "string", "description": "When it's happening"},
        "location": {"type": "string", "description": "Where — venue, address, or area"},
        "drive_time": {
            "type": "string",
            "description": "Approximate drive time from Reynoldstown, or 'walkable'",
        },
        "why_recommended": {
            "type": "string",
            "description": (
                "Why this is worth doing. Mention weather suitability, "
                "uniqueness, seasonal relevance, etc."
            ),
        },
        "combo_suggestion": {
            "type": "string",
            "description": (
                "Optional: nearby activities to pair with this "
                "(e.g., 'hit the farmers market in Dahlonega after')"
            ),
        },
        "url": {"type": "string", "description": "Link for more info"},
    },
    "required": ["title", "category", "date", "location", "why_recommended"],
}

_READ_PAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "The URL to fetch and read."},
    },
    "required": ["url"],
}

_READ_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": (
                "Relative path within the project. Example: "
                "'dynamic_tools/search_eventbrite.py' or 'config.py'"
            ),
        },
    },
    "required": ["path"],
}

_PROPOSE_NEW_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Tool name in snake_case"},
        "capability_key": {
            "type": "string",
            "description": (
                "Stable snake_case identifier for the underlying capability. "
                "Use the service plus action, e.g. 'alltrails_trail_conditions' "
                "or 'beltline_events_calendar'."
            ),
        },
        "description": {
            "type": "string",
            "description": "What the tool does and why it would be useful",
        },
        "rationale": {
            "type": "string",
            "description": "Why existing tools aren't sufficient for this",
        },
        "suggested_implementation": {
            "type": "string",
            "description": (
                "Implementation sketch in Python. It must include a single "
                "async function `run(tool_input: dict)` that returns a string. "
                "Use only feasible patterns: public HTTP requests via httpx, "
                "light HTML parsing, and documented APIs. No browser automation, "
                "no login/session scraping, and no private endpoints. For API "
                "keys, use: from secrets_store import get_secret; "
                "token = get_secret('SECRET_NAME')"
            ),
        },
        "reference_urls": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "One or more public docs/reference URLs proving the capability "
                "exists and is feasible to implement."
            ),
        },
        "dependencies": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Python packages required beyond what's already installed "
                "(httpx, anthropic, slack-sdk are available). "
                "Example: ['beautifulsoup4', 'lxml']. Omit if none needed."
            ),
        },
        "required_secrets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "Environment variable name "
                            "(e.g., EVENTBRITE_PRIVATE_TOKEN)"
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "What this key is for and where to get it",
                    },
                },
                "required": ["name", "description"],
            },
            "description": (
                "API keys or tokens the tool needs. Each entry has a name "
                "(env var style) and description of where to obtain it. "
                "These will be collected securely from the user on approval."
            ),
        },
        "input_schema": {
            "type": "object",
            "description": "JSON Schema for the tool's input parameters",
        },
    },
    "required": [
        "name",
        "capability_key",
        "description",
        "rationale",
        "suggested_implementation",
        "reference_urls",
        "input_schema",
    ],
}

_SAVE_MEMORY_SCHEMA = {
    "type": "object",
    "properties": {
        "key": {
            "type": "string",
            "description": (
                "Short identifier for this memory (e.g., 'user_dislikes_crowds')"
            ),
        },
        "content": {"type": "string", "description": "The information to remember"},
    },
    "required": ["key", "content"],
}

_RECALL_MEMORIES_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "What to search for (searches keys and content)",
        },
    },
    "required": ["query"],
}


# ---------------------------------------------------------------------------
# propose_new_tool: workflow-side interaction
# ---------------------------------------------------------------------------


async def _propose_new_tool_interaction(tool_input: dict, ctx) -> str:
    """Validate the proposal, signal the registry workflow, return a tool_result.

    Reads/writes caller state via `ctx.state`:
    - `all_tools`: list of currently-registered tool dicts/ToolDefs (read)
    - `rejected_tools`: list of rejection records (read)
    - `proposed_tool_names`: set updated as we accept proposals (read+write)
    - `proposed_capability_keys`: set updated as we accept proposals (read+write)
    """
    state = ctx.state

    proposal = normalize_proposal({
        "id": str(workflow.uuid4())[:8],
        "proposed_at": workflow.now().isoformat(),
        **tool_input,
    })

    errors = _validate(proposal, state)
    if errors:
        return (
            "Tool proposal rejected automatically: "
            + "; ".join(errors[:4])
            + " Continue with existing tools."
        )

    state.setdefault("proposed_tool_names", set()).add(proposal["name"])
    state.setdefault("proposed_capability_keys", set()).add(proposal["capability_key"])

    registry = workflow.get_external_workflow_handle("tool-registry")
    await registry.signal("propose_tool", proposal)

    return (
        f"Tool proposal `{proposal['name']}` "
        f"({proposal['capability_key']}) submitted for review. "
        "Continuing with existing tools."
    )


def _validate(proposal: dict, state: dict) -> list[str]:
    """Run validate_proposal against the caller's state. Pure helper."""
    from proposal_utils import normalize_identifier

    all_tools = state.get("all_tools", [])
    rejected = state.get("rejected_tools", [])

    def _name_of(t):
        if isinstance(t, ToolDef):
            return t.name
        return t.get("name", "") if isinstance(t, dict) else ""

    def _capkey_of(t):
        if isinstance(t, ToolDef):
            return t.name
        if isinstance(t, dict):
            return t.get("capability_key", t.get("name", ""))
        return ""

    approved_tool_names = {
        normalize_identifier(str(_name_of(t))) for t in all_tools if _name_of(t)
    }
    approved_capability_keys = {
        normalize_identifier(str(_capkey_of(t)))
        for t in all_tools
        if _capkey_of(t)
    }
    rejected_tool_names = {
        normalize_identifier(str(t.get("name", ""))) for t in rejected if t.get("name")
    }
    rejected_capability_keys = {
        normalize_identifier(str(t.get("capability_key", "")))
        for t in rejected
        if t.get("capability_key")
    }

    return validate_proposal(
        proposal,
        approved_tool_names=approved_tool_names,
        approved_capability_keys=approved_capability_keys,
        rejected_tool_names=rejected_tool_names,
        rejected_capability_keys=rejected_capability_keys,
        pending_tool_names=set(state.get("proposed_tool_names", set())),
        pending_capability_keys=set(state.get("proposed_capability_keys", set())),
    )


# ---------------------------------------------------------------------------
# The static tool registry
# ---------------------------------------------------------------------------

STATIC_TOOLS: list[ToolDef] = [
    ToolDef(
        name="search_events",
        description=(
            "Search the web for local events, festivals, pop-ups, shows, and "
            "happenings in or near Atlanta. Returns relevant search results."
        ),
        input_schema=_SEARCH_EVENTS_SCHEMA,
    ),
    ToolDef(
        name="search_outdoors",
        description=(
            "Search for outdoor activities: hiking trails, bike routes, kayaking "
            "spots, scenic drives, etc. Can search for trail conditions, seasonal "
            "info (wildflowers, foliage), and nearby amenities."
        ),
        input_schema=_SEARCH_OUTDOORS_SCHEMA,
    ),
    ToolDef(
        name="get_weather",
        description=(
            "Get the weather forecast for Atlanta or a specific location. "
            "Returns a multi-day forecast with temperature, precipitation, and conditions."
        ),
        input_schema=_GET_WEATHER_SCHEMA,
    ),
    ToolDef(
        name="save_recommendation",
        description=(
            "Save an activity recommendation for the weekly report. Call this for "
            "every activity worth recommending. Include enough detail for the report."
        ),
        input_schema=_SAVE_RECOMMENDATION_SCHEMA,
    ),
    ToolDef(
        name="read_page",
        description=(
            "Fetch and read the full content of a web page. Use this to get "
            "details from URLs found in search results — event pages, trail "
            "descriptions, restaurant menus, calendar listings, etc."
        ),
        input_schema=_READ_PAGE_SCHEMA,
    ),
    ToolDef(
        name="read_file",
        description=(
            "Read a file from the agent's project directory. Use this to inspect "
            "dynamic tool source code, config files, or other local files. "
            "Paths are relative to the project root."
        ),
        input_schema=_READ_FILE_SCHEMA,
    ),
    ToolDef(
        name="propose_new_tool",
        description=(
            "Last resort: propose a new tool/capability only when the task is "
            "important, impossible with current tools, and plausibly implementable "
            "with a public webpage or documented API. You must include a stable "
            "capability_key and at least one public docs/reference URL. Never use "
            "this for browser automation, logins, CAPTCHAs, private APIs, or mobile "
            "apps. Do NOT wait for approval; continue with existing tools."
        ),
        input_schema=_PROPOSE_NEW_TOOL_SCHEMA,
        kind="interaction",
        interaction=_propose_new_tool_interaction,
        timeout=timedelta(minutes=1),
    ),
]


MEMORY_TOOLS: list[ToolDef] = [
    ToolDef(
        name="save_memory",
        description=(
            "Save something to persistent memory for future reference. Use this "
            "when the user asks you to remember something, gives you feedback or "
            "preferences, or when you learn something worth retaining. Memories "
            "persist across conversations."
        ),
        input_schema=_SAVE_MEMORY_SCHEMA,
    ),
    ToolDef(
        name="recall_memories",
        description=(
            "Search persistent memory for previously saved information. Use this "
            "to recall user preferences, past feedback, or any stored context."
        ),
        input_schema=_RECALL_MEMORIES_SCHEMA,
    ),
]
