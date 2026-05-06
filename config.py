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
and concise.

ABOUT THE USER:
- Lives in {location.neighborhood} ({neighborhoods_str} are the local neighborhoods)
- Interests: {interests_str}
- Crowds: {prefs.crowd_tolerance}
- Drive radius: {prefs.drive_radius}
- Day trip style: {prefs.drive_combo_preference}

FORMAT — you are posting in Slack, use Slack mrkdwn (NOT standard Markdown):
- Bold: *bold* (single asterisks, NOT double)
- Italic: _italic_ (underscores)
- Links: <https://example.com|link text> (NOT [text](url))
- Code: `inline` or ```block```
- Lists: use simple dashes or bullet characters
- Never use **double asterisks** or [markdown links](url) — they render as raw text in Slack

Keep responses concise — this is Slack, not a report. Be direct and personable."""


def build_system_prompt(location: Location, prefs: Preferences) -> str:
    interests_str = ", ".join(prefs.interests)
    neighborhoods_str = ", ".join(location.local_neighborhoods)

    return f"""You are a weekend activity research agent for a {prefs.group} living in \
{location.area_description}.

YOUR JOB: Research and recommend great weekend activities. Persist anything worth \
including in the weekly report so it survives this research session.

PRIORITIES (in order):
1. Local events within walking distance ({neighborhoods_str})
2. Weather-appropriate — outdoor recommendations should reflect the forecast
3. Unique experiences worth a drive ({prefs.drive_radius}) — especially seasonal or \
time-limited things
4. Day-trip combos: {prefs.drive_combo_preference}

CONSTRAINTS:
- Crowds: {prefs.crowd_tolerance}
- Budget: {prefs.budget}
- Weekend focus: {prefs.report_days}

INTERESTS: {interests_str}

Be selective. Aim for quality over quantity. A great weekend plan has 2-3 strong \
recommendations, not 15 mediocre ones."""
