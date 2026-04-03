"""Tool definitions (Claude schema) and implementations."""

import json
import os
from datetime import datetime
from pathlib import Path

import httpx

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

# ---------------------------------------------------------------------------
# Tool definitions — these get passed to Claude's `tools` parameter
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "name": "search_events",
        "description": (
            "Search the web for local events, festivals, pop-ups, shows, and "
            "happenings in or near Atlanta. Returns relevant search results."
        ),
        "input_schema": {
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
        },
    },
    {
        "name": "search_outdoors",
        "description": (
            "Search for outdoor activities: hiking trails, bike routes, kayaking "
            "spots, scenic drives, etc. Can search for trail conditions, seasonal "
            "info (wildflowers, foliage), and nearby amenities."
        ),
        "input_schema": {
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
        },
    },
    {
        "name": "get_weather",
        "description": (
            "Get the weather forecast for Atlanta or a specific location. "
            "Returns a multi-day forecast with temperature, precipitation, and conditions."
        ),
        "input_schema": {
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
        },
    },
    {
        "name": "save_recommendation",
        "description": (
            "Save an activity recommendation for the weekly report. Call this for "
            "every activity worth recommending. Include enough detail for the report."
        ),
        "input_schema": {
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
        },
    },
    {
        "name": "read_page",
        "description": (
            "Fetch and read the full content of a web page. Use this to get "
            "details from URLs found in search results — event pages, trail "
            "descriptions, restaurant menus, calendar listings, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch and read.",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a file from the agent's project directory. Use this to inspect "
            "dynamic tool source code, config files, or other local files. "
            "Paths are relative to the project root."
        ),
        "input_schema": {
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
        },
    },
    {
        "name": "propose_new_tool",
        "description": (
            "Last resort: propose a new tool/capability only when the task is "
            "important, impossible with current tools, and plausibly implementable "
            "with a public webpage or documented API. You must include a stable "
            "capability_key and at least one public docs/reference URL. Never use "
            "this for browser automation, logins, CAPTCHAs, private APIs, or mobile "
            "apps. Do NOT wait for approval; continue with existing tools."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Tool name in snake_case",
                },
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
                                "description": "Environment variable name (e.g., EVENTBRITE_PRIVATE_TOKEN)",
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
        },
    },
]

MEMORY_TOOLS = [
    {
        "name": "save_memory",
        "description": (
            "Save something to persistent memory for future reference. Use this "
            "when the user asks you to remember something, gives you feedback or "
            "preferences, or when you learn something worth retaining. Memories "
            "persist across conversations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Short identifier for this memory (e.g., 'user_dislikes_crowds')",
                },
                "content": {
                    "type": "string",
                    "description": "The information to remember",
                },
            },
            "required": ["key", "content"],
        },
    },
    {
        "name": "recall_memories",
        "description": (
            "Search persistent memory for previously saved information. Use this "
            "to recall user preferences, past feedback, or any stored context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to search for (searches keys and content)",
                },
            },
            "required": ["query"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


async def search_tavily(query: str, search_depth: str = "basic") -> str:
    """General-purpose web search via Tavily."""
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": query,
                "search_depth": search_depth,
                "include_answer": True,
                "max_results": 8,
            },
        )
        response.raise_for_status()
        data = response.json()

    # Format results for the LLM
    parts = []
    if data.get("answer"):
        parts.append(f"Summary: {data['answer']}\n")

    for r in data.get("results", []):
        parts.append(f"- {r['title']}\n  {r['url']}\n  {r.get('content', '')[:300]}")

    return "\n\n".join(parts) if parts else "No results found."


async def extract_page(url: str) -> str:
    """Fetch clean content from a URL via Tavily's extract endpoint."""
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.tavily.com/extract",
            json={
                "api_key": TAVILY_API_KEY,
                "urls": [url],
            },
        )
        response.raise_for_status()
        data = response.json()

    results = data.get("results", [])
    if not results:
        # Fallback: try a direct fetch and return raw text (truncated)
        return await _direct_fetch(url)

    content = results[0].get("raw_content", "") or results[0].get("content", "")
    # Truncate to avoid blowing up the LLM context
    if len(content) > 8000:
        content = content[:8000] + "\n\n[... truncated — page content exceeded 8000 chars]"
    return content


async def _direct_fetch(url: str) -> str:
    """Fallback: fetch page directly and return text content."""
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        response = await client.get(url, headers={"User-Agent": "weekend-activity-agent/0.1"})
        response.raise_for_status()
    text = response.text
    if len(text) > 8000:
        text = text[:8000] + "\n\n[... truncated]"
    return text


async def get_weather_forecast(lat: float = 33.749, lon: float = -84.358) -> str:
    """Fetch forecast from weather.gov (free, no key, US only)."""
    headers = {"User-Agent": "weekend-activity-agent/0.1 (contact@example.com)"}

    async with httpx.AsyncClient(timeout=15, headers=headers) as client:
        # Step 1: resolve lat/lon to a forecast grid
        points_resp = await client.get(f"https://api.weather.gov/points/{lat},{lon}")
        points_resp.raise_for_status()
        forecast_url = points_resp.json()["properties"]["forecast"]

        # Step 2: get the actual forecast
        forecast_resp = await client.get(forecast_url)
        forecast_resp.raise_for_status()
        periods = forecast_resp.json()["properties"]["periods"]

    # Format for the LLM — next 7 periods (~ 3.5 days)
    lines = []
    for p in periods[:10]:
        lines.append(
            f"{p['name']}: {p['temperature']}\u00b0{p['temperatureUnit']}, "
            f"{p['shortForecast']}. Wind: {p['windSpeed']} {p['windDirection']}. "
            f"{p['detailedForecast']}"
        )
    return "\n\n".join(lines)


MEMORIES_DIR = Path(__file__).parent / "memories"


async def save_memory(key: str, content: str) -> str:
    """Save a memory entry to disk."""
    MEMORIES_DIR.mkdir(exist_ok=True)
    memory = {
        "key": key,
        "content": content,
        "saved_at": datetime.now().isoformat(),
    }
    filepath = MEMORIES_DIR / f"{key}.json"
    filepath.write_text(json.dumps(memory, indent=2))
    return f"Memory saved: {key}"


async def recall_memories(query: str) -> str:
    """Search saved memories by key and content."""
    if not MEMORIES_DIR.exists():
        return "No memories saved yet."

    query_lower = query.lower()
    matches = []
    for filepath in MEMORIES_DIR.glob("*.json"):
        try:
            memory = json.loads(filepath.read_text())
            if (query_lower in memory.get("key", "").lower() or
                    query_lower in memory.get("content", "").lower()):
                matches.append(f"[{memory['key']}] {memory['content']}")
        except (json.JSONDecodeError, KeyError):
            continue

    if not matches:
        # Return all memories if no specific match
        all_memories = []
        for filepath in sorted(MEMORIES_DIR.glob("*.json")):
            try:
                memory = json.loads(filepath.read_text())
                all_memories.append(f"[{memory['key']}] {memory['content']}")
            except (json.JSONDecodeError, KeyError):
                continue
        if all_memories:
            return "No exact matches. All saved memories:\n" + "\n".join(all_memories)
        return "No memories saved yet."

    return "\n".join(matches)


PROJECT_ROOT = Path(__file__).parent


def _read_project_file(path: str) -> str:
    """Read a file scoped to the project directory."""
    resolved = (PROJECT_ROOT / path).resolve()
    if not str(resolved).startswith(str(PROJECT_ROOT.resolve())):
        return "Error: path traversal outside project directory is not allowed."
    if not resolved.exists():
        return f"File not found: {path}"
    if not resolved.is_file():
        return f"Not a file: {path}"
    content = resolved.read_text()
    if len(content) > 8000:
        content = content[:8000] + "\n\n[... truncated — file exceeded 8000 chars]"
    return content


async def dispatch_tool(name: str, tool_input: dict) -> str:
    """Dispatch a tool call. Async — runs inside Temporal's event loop."""
    match name:
        case "search_events":
            query = f"Atlanta {tool_input['query']} events {_this_weekend()}"
            return await search_tavily(query, search_depth="advanced")

        case "search_outdoors":
            query = f"{tool_input['query']} near Atlanta Georgia"
            return await search_tavily(query, search_depth="advanced")

        case "read_page":
            return await extract_page(tool_input["url"])

        case "get_weather":
            lat = tool_input.get("lat", 33.749)
            lon = tool_input.get("lon", -84.358)
            return await get_weather_forecast(lat, lon)

        case "read_file":
            return _read_project_file(tool_input["path"])

        case "save_recommendation":
            return "Recommendation saved."

        case "propose_new_tool":
            capability_key = tool_input.get("capability_key", tool_input.get("name", "unknown"))
            return (
                f"Tool proposal `{tool_input.get('name', 'unknown')}` "
                f"({capability_key}) submitted for review. Continuing with existing tools."
            )

        case "save_memory":
            return await save_memory(tool_input["key"], tool_input["content"])

        case "recall_memories":
            return await recall_memories(tool_input["query"])

        case _:
            return await _dispatch_dynamic_tool(name, tool_input)


async def _dispatch_dynamic_tool(name: str, tool_input: dict) -> str:
    """Load and execute a dynamically approved tool from dynamic_tools/."""
    import importlib.util
    from pathlib import Path

    tool_path = Path(__file__).parent / "dynamic_tools" / f"{name}.py"
    if not tool_path.exists():
        raise ValueError(f"Unknown tool: {name}")

    spec = importlib.util.spec_from_file_location(name, tool_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # Look for run(), or fall back to the first async callable matching the tool name
    import inspect
    fn = getattr(module, "run", None)
    if fn is None:
        fn = getattr(module, name, None)
    if fn is None:
        # Find any async function in the module
        for attr_name, attr in inspect.getmembers(module, inspect.iscoroutinefunction):
            if not attr_name.startswith("_"):
                fn = attr
                break
    if fn is None:
        raise ValueError(f"Dynamic tool {name} has no callable function")

    return await fn(tool_input)


def _this_weekend() -> str:
    """Returns a string like 'March 21-22 2026' for the coming weekend."""
    today = datetime.now()
    days_until_saturday = (5 - today.weekday()) % 7
    if days_until_saturday == 0 and today.weekday() != 5:
        days_until_saturday = 7
    from datetime import timedelta
    saturday = today + timedelta(days=days_until_saturday)
    sunday = saturday + timedelta(days=1)
    return f"{saturday.strftime('%B %d')}-{sunday.strftime('%d')} {saturday.year}"
