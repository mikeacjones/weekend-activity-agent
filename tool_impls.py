"""Activity-side tool implementations.

Imports httpx, env vars, and uses importlib to load runtime-approved tools
from `dynamic_tools/`. Lives outside the workflow sandbox — workflows must
NOT import this module. The dynamic activity (`agent_harness.dynamic_executor`)
imports it and dispatches by name.

The module exposes `STATIC_IMPLS: dict[str, Callable[[dict], Awaitable[str]]]`
mapping each statically-known tool name to a coroutine that takes the tool's
input dict and returns a string. Anything not in `STATIC_IMPLS` is loaded
from `dynamic_tools/{name}.py` via `dispatch_dynamic_tool`.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable

import httpx


TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
PROJECT_ROOT = Path(__file__).parent
MEMORIES_DIR = PROJECT_ROOT / "memories"
DYNAMIC_TOOLS_DIR = PROJECT_ROOT / "dynamic_tools"


# ---------------------------------------------------------------------------
# Pure helpers
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
            json={"api_key": TAVILY_API_KEY, "urls": [url]},
        )
        response.raise_for_status()
        data = response.json()

    results = data.get("results", [])
    if not results:
        return await _direct_fetch(url)

    content = results[0].get("raw_content", "") or results[0].get("content", "")
    if len(content) > 8000:
        content = content[:8000] + "\n\n[... truncated — page content exceeded 8000 chars]"
    return content


async def _direct_fetch(url: str) -> str:
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        response = await client.get(
            url, headers={"User-Agent": "weekend-activity-agent/0.1"}
        )
        response.raise_for_status()
    text = response.text
    if len(text) > 8000:
        text = text[:8000] + "\n\n[... truncated]"
    return text


async def get_weather_forecast(lat: float = 33.749, lon: float = -84.358) -> str:
    """Fetch forecast from weather.gov (free, no key, US only)."""
    headers = {"User-Agent": "weekend-activity-agent/0.1 (contact@example.com)"}
    async with httpx.AsyncClient(timeout=15, headers=headers) as client:
        points_resp = await client.get(f"https://api.weather.gov/points/{lat},{lon}")
        points_resp.raise_for_status()
        forecast_url = points_resp.json()["properties"]["forecast"]

        forecast_resp = await client.get(forecast_url)
        forecast_resp.raise_for_status()
        periods = forecast_resp.json()["properties"]["periods"]

    lines = []
    for p in periods[:10]:
        lines.append(
            f"{p['name']}: {p['temperature']}°{p['temperatureUnit']}, "
            f"{p['shortForecast']}. Wind: {p['windSpeed']} {p['windDirection']}. "
            f"{p['detailedForecast']}"
        )
    return "\n\n".join(lines)


async def save_memory(key: str, content: str) -> str:
    MEMORIES_DIR.mkdir(exist_ok=True)
    memory = {
        "key": key,
        "content": content,
        "saved_at": datetime.now().isoformat(),
    }
    (MEMORIES_DIR / f"{key}.json").write_text(json.dumps(memory, indent=2))
    return f"Memory saved: {key}"


async def recall_memories(query: str) -> str:
    if not MEMORIES_DIR.exists():
        return "No memories saved yet."

    query_lower = query.lower()
    matches = []
    for filepath in MEMORIES_DIR.glob("*.json"):
        try:
            memory = json.loads(filepath.read_text())
            if (
                query_lower in memory.get("key", "").lower()
                or query_lower in memory.get("content", "").lower()
            ):
                matches.append(f"[{memory['key']}] {memory['content']}")
        except (json.JSONDecodeError, KeyError):
            continue

    if not matches:
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


def _this_weekend() -> str:
    """Returns a string like 'March 21-22 2026' for the coming weekend."""
    today = datetime.now()
    days_until_saturday = (5 - today.weekday()) % 7
    if days_until_saturday == 0 and today.weekday() != 5:
        days_until_saturday = 7
    saturday = today + timedelta(days=days_until_saturday)
    sunday = saturday + timedelta(days=1)
    return f"{saturday.strftime('%B %d')}-{sunday.strftime('%d')} {saturday.year}"


# ---------------------------------------------------------------------------
# Tool-name → coroutine wrappers (input dict in, string out)
# ---------------------------------------------------------------------------


async def _search_events(tool_input: dict) -> str:
    query = f"Atlanta {tool_input['query']} events {_this_weekend()}"
    return await search_tavily(query, search_depth="advanced")


async def _search_outdoors(tool_input: dict) -> str:
    query = f"{tool_input['query']} near Atlanta Georgia"
    return await search_tavily(query, search_depth="advanced")


async def _read_page(tool_input: dict) -> str:
    return await extract_page(tool_input["url"])


async def _get_weather(tool_input: dict) -> str:
    lat = tool_input.get("lat", 33.749)
    lon = tool_input.get("lon", -84.358)
    return await get_weather_forecast(lat, lon)


async def _read_file(tool_input: dict) -> str:
    return _read_project_file(tool_input["path"])


async def _save_recommendation(_tool_input: dict) -> str:
    # The workflow scrapes save_recommendation inputs from TurnResult; the
    # activity itself just acknowledges so the LLM keeps moving.
    return "Recommendation saved."


async def _save_memory(tool_input: dict) -> str:
    return await save_memory(tool_input["key"], tool_input["content"])


async def _recall_memories(tool_input: dict) -> str:
    return await recall_memories(tool_input["query"])


STATIC_IMPLS: dict[str, Callable[[dict], Awaitable[str]]] = {
    "search_events": _search_events,
    "search_outdoors": _search_outdoors,
    "read_page": _read_page,
    "get_weather": _get_weather,
    "read_file": _read_file,
    "save_recommendation": _save_recommendation,
    "save_memory": _save_memory,
    "recall_memories": _recall_memories,
}


# ---------------------------------------------------------------------------
# Dynamic tools loaded from dynamic_tools/{name}.py
# ---------------------------------------------------------------------------


async def dispatch_dynamic_tool(name: str, tool_input: dict) -> str:
    """Load and execute a runtime-approved tool from dynamic_tools/."""
    tool_path = DYNAMIC_TOOLS_DIR / f"{name}.py"
    if not tool_path.exists():
        raise FileNotFoundError(f"Unknown tool: {name}")

    spec = importlib.util.spec_from_file_location(name, tool_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    fn = getattr(module, "run", None)
    if fn is None:
        fn = getattr(module, name, None)
    if fn is None:
        for attr_name, attr in inspect.getmembers(module, inspect.iscoroutinefunction):
            if not attr_name.startswith("_"):
                fn = attr
                break
    if fn is None:
        raise ValueError(f"Dynamic tool {name} has no callable function")

    return await fn(tool_input)
