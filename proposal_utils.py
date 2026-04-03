"""Helpers for normalizing and validating tool proposals."""

from __future__ import annotations

import re
from urllib.parse import urlparse

DEFAULT_INPUT_SCHEMA = {
    "type": "object",
    "properties": {},
}

_BANNED_TEXT_PATTERNS: list[tuple[str, str]] = [
    ("selenium", "Browser automation proposals are not allowed."),
    ("playwright", "Browser automation proposals are not allowed."),
    ("puppeteer", "Browser automation proposals are not allowed."),
    ("webdriver", "Browser automation proposals are not allowed."),
    ("headless browser", "Browser automation proposals are not allowed."),
    ("browser automation", "Browser automation proposals are not allowed."),
    ("captcha", "CAPTCHA-dependent integrations are not allowed."),
    ("sign in", "Login-gated integrations are not allowed."),
    ("log in", "Login-gated integrations are not allowed."),
    ("login", "Login-gated integrations are not allowed."),
    ("oauth", "OAuth or user-login flows are not allowed for dynamic tools."),
    ("private api", "Private or reverse-engineered APIs are not allowed."),
    ("mobile app", "Mobile-app-only integrations are not allowed."),
]

_BANNED_DEPENDENCIES = {
    "playwright",
    "selenium",
    "pyppeteer",
    "undetected-chromedriver",
}


def normalize_identifier(value: str, *, fallback: str = "") -> str:
    text = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower())
    text = re.sub(r"_+", "_", text).strip("_")
    return text or fallback


def normalize_reference_urls(values: list[str] | str | None) -> list[str]:
    if values is None:
        return []
    candidates = values if isinstance(values, list) else [values]

    urls: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        if not isinstance(raw, str):
            continue
        url = raw.strip()
        if not url:
            continue
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def normalize_string_list(values: list[str] | None) -> list[str]:
    if not isinstance(values, list):
        return []

    items: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        cleaned = value.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        items.append(cleaned)
    return items


def normalize_required_secrets(values: list[dict] | None) -> list[dict]:
    if not isinstance(values, list):
        return []

    secrets: list[dict] = []
    seen: set[str] = set()
    for secret in values:
        if not isinstance(secret, dict):
            continue
        name = secret.get("name", "")
        description = secret.get("description", "")
        normalized_name = normalize_identifier(str(name), fallback="").upper()
        if not normalized_name or normalized_name in seen:
            continue
        seen.add(normalized_name)
        secrets.append({
            "name": normalized_name,
            "description": str(description).strip(),
        })
    return secrets


def normalize_input_schema(schema: dict | None) -> dict:
    if isinstance(schema, dict):
        normalized = dict(schema)
        normalized.setdefault("type", "object")
        normalized.setdefault("properties", {})
        return normalized
    return dict(DEFAULT_INPUT_SCHEMA)


def normalize_tool_summary(tool: dict) -> dict:
    name = normalize_identifier(str(tool.get("name", "")))
    capability_key = normalize_identifier(
        str(tool.get("capability_key", "")),
        fallback=name,
    )
    return {
        "name": name,
        "capability_key": capability_key,
        "description": str(tool.get("description", "")).strip(),
        "input_schema": normalize_input_schema(tool.get("input_schema")),
    }


def normalize_proposal(tool_input: dict) -> dict:
    description = str(tool_input.get("description", "")).strip()
    raw_name = str(tool_input.get("name", "")).strip()
    fallback_name = normalize_identifier(description[:60], fallback="proposed_tool")
    name = normalize_identifier(raw_name, fallback=fallback_name)
    capability_key = normalize_identifier(
        str(tool_input.get("capability_key", "")).strip(),
        fallback=name,
    )

    normalized = {
        "name": name,
        "capability_key": capability_key,
        "description": description,
        "rationale": str(tool_input.get("rationale", "")).strip(),
        "suggested_implementation": str(tool_input.get("suggested_implementation", "")).strip(),
        "dependencies": normalize_string_list(tool_input.get("dependencies")),
        "required_secrets": normalize_required_secrets(tool_input.get("required_secrets")),
        "input_schema": normalize_input_schema(tool_input.get("input_schema")),
        "reference_urls": normalize_reference_urls(tool_input.get("reference_urls")),
    }
    for key in ("id", "proposed_at"):
        if key in tool_input:
            normalized[key] = tool_input[key]
    return normalized


def format_rejected_capability_note(entries: list[dict]) -> str:
    notes: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = normalize_identifier(str(entry.get("capability_key", "")))
        name = normalize_identifier(str(entry.get("name", "")))
        label = key or name
        if not label or label in seen:
            continue
        seen.add(label)
        if key and name and key != name:
            notes.append(f"{key} (tool name: {name})")
        else:
            notes.append(label)
    return ", ".join(notes)


def validate_proposal(
    proposal: dict,
    *,
    approved_tool_names: set[str] | None = None,
    approved_capability_keys: set[str] | None = None,
    rejected_tool_names: set[str] | None = None,
    rejected_capability_keys: set[str] | None = None,
    pending_tool_names: set[str] | None = None,
    pending_capability_keys: set[str] | None = None,
) -> list[str]:
    approved_tool_names = approved_tool_names or set()
    approved_capability_keys = approved_capability_keys or set()
    rejected_tool_names = rejected_tool_names or set()
    rejected_capability_keys = rejected_capability_keys or set()
    pending_tool_names = pending_tool_names or set()
    pending_capability_keys = pending_capability_keys or set()

    reasons: list[str] = []

    name = normalize_identifier(str(proposal.get("name", "")))
    capability_key = normalize_identifier(
        str(proposal.get("capability_key", "")),
        fallback=name,
    )
    description = str(proposal.get("description", "")).strip()
    rationale = str(proposal.get("rationale", "")).strip()
    implementation = str(proposal.get("suggested_implementation", "")).strip()
    dependencies = normalize_string_list(proposal.get("dependencies"))
    input_schema = proposal.get("input_schema")
    reference_urls = normalize_reference_urls(proposal.get("reference_urls"))

    if not name:
        reasons.append("Tool proposals need a snake_case tool name.")
    if not capability_key:
        reasons.append("Tool proposals need a stable capability_key.")
    if not description:
        reasons.append("Tool proposals need a clear description.")
    if not rationale:
        reasons.append("Tool proposals need a rationale explaining why existing tools fail.")
    if not implementation:
        reasons.append("Tool proposals need a concrete implementation sketch.")
    if "async def run(" not in implementation:
        reasons.append("Implementation sketch must include `async def run(tool_input: dict)`.")
    if not reference_urls:
        reasons.append("Include at least one public docs or reference URL.")
    if not isinstance(input_schema, dict) or input_schema.get("type") != "object":
        reasons.append("input_schema must be a JSON object schema.")

    if name in approved_tool_names:
        reasons.append(f"Tool `{name}` already exists.")
    if capability_key in approved_capability_keys:
        reasons.append(f"Capability `{capability_key}` is already implemented.")
    if name in rejected_tool_names or capability_key in rejected_capability_keys:
        reasons.append(f"Capability `{capability_key}` was previously rejected.")
    if name in pending_tool_names or capability_key in pending_capability_keys:
        reasons.append(f"Capability `{capability_key}` is already pending review.")

    haystack = "\n".join([description, rationale, implementation]).lower()
    for pattern, reason in _BANNED_TEXT_PATTERNS:
        if pattern in haystack:
            reasons.append(reason)

    lower_deps = {dep.lower() for dep in dependencies}
    banned_deps = sorted(lower_deps & _BANNED_DEPENDENCIES)
    if banned_deps:
        reasons.append(
            "Browser automation dependencies are not allowed: "
            + ", ".join(banned_deps)
        )

    deduped: list[str] = []
    seen: set[str] = set()
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            deduped.append(reason)
    return deduped
