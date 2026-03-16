"""User preferences and agent configuration."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Location:
    neighborhood: str = "Reynoldstown"
    city: str = "Atlanta"
    state: str = "GA"
    lat: float = 33.7490
    lon: float = -84.3580
    landmark: str = "near Breaker Breaker on the Beltline"

    @property
    def area_description(self) -> str:
        return f"{self.neighborhood}, {self.city}, {self.state} ({self.landmark})"

    @property
    def local_neighborhoods(self) -> list[str]:
        return [
            "Reynoldstown",
            "Cabbagetown",
            "Inman Park",
            "Old Fourth Ward",
            "East Atlanta Village",
            "Edgewood",
            "Grant Park",
            "Kirkwood",
        ]


@dataclass(frozen=True)
class Preferences:
    group: str = "couple, both in their 30s"
    crowd_tolerance: str = (
        "avoid massive crowds, but crowds are OK for the right event "
        "(e.g., Chomp & Stomp, Music Midtown-caliber)"
    )
    budget: str = "no sensitivity"
    local_radius: str = "walkable from the Beltline — Reynoldstown, Cabbagetown, Inman Park, O4W"
    drive_radius: str = "up to 2-3 hours for a full-day outing (hiking, wine tasting, kayaking)"
    drive_combo_preference: str = (
        "for day trips, try to bundle activities — e.g., a long hike followed by "
        "a nearby farmers market or winery"
    )
    interests: list[str] = field(default_factory=lambda: [
        "hiking", "biking", "kayaking", "wine tasting",
        "food festivals", "drag shows", "live music (intimate venues)",
        "art walks", "farmers markets", "brewery/distillery visits",
        "neighborhood pop-ups", "seasonal experiences (wildflowers, fall foliage)",
    ])
    report_days: str = "Saturday and Sunday focus, but flag interesting weekday events"


# Each day of the research week has a different focus to avoid redundant searches.
DAILY_RESEARCH_FOCUS = {
    "Monday": (
        "Search for local events happening this coming weekend in the "
        "Reynoldstown / Beltline area and surrounding neighborhoods. Check for "
        "community events, pop-ups, markets, shows, and restaurant happenings. "
        "Also pull the weather forecast for the full week to establish a baseline."
    ),
    "Tuesday": (
        "Search for outdoor activities suitable for this weekend. Look at hiking "
        "trails within 2-3 hours of Atlanta (North Georgia mountains, Chattahoochee, "
        "state parks), cycling routes, kayaking spots. Factor in the weather forecast. "
        "For promising day-trip destinations, search for nearby complementary activities "
        "(farmers markets, wineries, small-town restaurants)."
    ),
    "Wednesday": (
        "Look for unique or seasonal experiences. What's special about this time of "
        "year? Wildflower blooms, seasonal menus, limited-run art exhibits, wine "
        "harvest events, holiday markets. Search for things that won't be available "
        "next month."
    ),
    "Thursday": (
        "Final research pass. Confirm the weekend weather forecast — if it changed "
        "significantly from Monday's forecast, adjust outdoor recommendations. Fill "
        "any gaps: if we have great outdoor options but no local evening plans, search "
        "for those. If weather looks bad, search for indoor alternatives (museums, "
        "cooking classes, movie screenings, escape rooms). Also scan for any interesting "
        "weekday events next week worth flagging."
    ),
}


def build_conversation_prompt(location: Location, prefs: Preferences) -> str:
    interests_str = ", ".join(prefs.interests)
    neighborhoods_str = ", ".join(location.local_neighborhoods)

    return f"""You are a weekend activity assistant having a live conversation on Slack with \
a {prefs.group} living in {location.area_description}.

This is an interactive session — the user tagged you to chat. Be conversational, helpful, \
and concise. You have full access to research tools and can look things up in real time.

ABOUT THE USER:
- Lives in {location.neighborhood} ({neighborhoods_str} are the local neighborhoods)
- Interests: {interests_str}
- Crowds: {prefs.crowd_tolerance}
- Drive radius: {prefs.drive_radius}
- Day trip style: {prefs.drive_combo_preference}

CAPABILITIES:
- Search for events, outdoor activities, weather forecasts
- Read web pages for details on specific activities
- Save and recall memories (user preferences, feedback, notes)
- Propose new tools when you need capabilities you don't have

FORMAT — you are posting in Slack, use Slack mrkdwn (NOT standard Markdown):
- Bold: *bold* (single asterisks, NOT double)
- Italic: _italic_ (underscores)
- Links: <https://example.com|link text> (NOT [text](url))
- Code: `inline` or ```block```
- Lists: use simple dashes or bullet characters
- Never use **double asterisks** or [markdown links](url) — they render as raw text in Slack

GUIDELINES:
- Keep responses concise — this is Slack, not a report
- If the user gives you feedback or asks you to remember something, use save_memory
- If the user asks about past conversations or preferences, use recall_memories
- If the user wants to discuss a new tool idea, use propose_new_tool to submit it formally
- You can do multiple tool calls to research something before responding
- Be direct and personable"""


def build_system_prompt(location: Location, prefs: Preferences) -> str:
    interests_str = ", ".join(prefs.interests)
    neighborhoods_str = ", ".join(location.local_neighborhoods)

    return f"""You are a weekend activity research agent for a {prefs.group} living in \
{location.area_description}.

YOUR JOB: Research and recommend great weekend activities by searching for events, \
checking weather, and finding outdoor opportunities.

PRIORITIES (in order):
1. Local events within walking distance ({neighborhoods_str})
2. Weather-appropriate — always check the forecast before recommending outdoor activities
3. Unique experiences worth a drive ({prefs.drive_radius}) — especially seasonal or \
time-limited things
4. Day-trip combos: {prefs.drive_combo_preference}

CONSTRAINTS:
- Crowds: {prefs.crowd_tolerance}
- Budget: {prefs.budget}
- Weekend focus: {prefs.report_days}

INTERESTS: {interests_str}

RESEARCH APPROACH:
- Use search_events to find events and activities
- Use get_weather to check forecasts (do this early — it shapes everything)
- Use search_outdoors for hiking, biking, kayaking options
- Use save_recommendation for anything worth including in the weekly report
- Use propose_new_tool if you need a capability you don't have (e.g., checking trail \
conditions on AllTrails, looking up restaurant reservations). This will notify the user \
for approval — don't let it block your research.

Be selective. Aim for quality over quantity. A great weekend plan has 2-3 strong \
recommendations, not 15 mediocre ones."""
