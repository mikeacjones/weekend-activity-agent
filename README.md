# Weekend Activity Agent

An AI agent that researches weekend activities throughout the week and delivers a curated recommendation report every Thursday/Friday via Slack.

Built on [Temporal](https://temporal.io) for durable execution — every LLM call and tool invocation is individually retryable, and the agent recovers from crashes without re-doing work.

## How it works

```
Mon → Local events + weather baseline
Tue → Outdoor activities (hiking, biking, kayaking)
Wed → Unique/seasonal experiences
Thu → Fill gaps, confirm weather, flag weekday events
Thu evening → Compile and send Slack report with interactive Block Kit UI
```

Each day's research runs as a child workflow with a fine-grained agentic loop — the LLM decides which tools to call, and each step is a separate Temporal activity. If a tool call fails at step 17, Temporal replays steps 1–16 from history and retries only step 17.

### Human-in-the-loop tool creation

The agent can propose new tools mid-research (e.g., "I need an AllTrails scraper for trail conditions"). Proposals are sent to Slack with approve/reject buttons and auto-expire after 15 days. Approved tools are dynamically loaded on the next research run — including any new Python dependencies.

## Setup

### Prerequisites

- [uv](https://docs.astral.sh/uv/) for Python/dependency management
- A running [Temporal](https://temporal.io) server (`temporal server start-dev` for local)
- API keys: [Anthropic](https://console.anthropic.com/), [Tavily](https://tavily.com) (free tier)
- A Slack workspace you admin

### Slack app

Create a Slack app from the included manifest:

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From a manifest**
2. Select your workspace → **JSON** tab → paste contents of `slack-app-manifest.json`
3. Settings → Basic Information → App-Level Tokens → Generate with `connections:write` scope → copy `xapp-...`
4. Features → OAuth & Permissions → Install to Workspace → copy `xoxb-...`
5. Right-click your target channel → View channel details → copy Channel ID

### Configure

```bash
cp .env.example .env
# Fill in: ANTHROPIC_API_KEY, TAVILY_API_KEY, SLACK_BOT_TOKEN, SLACK_APP_TOKEN,
#          SLACK_CHANNEL_ID, TEMPORAL_ADDRESS
```

### Run locally

```bash
uv sync
uv run python worker.py    # Temporal worker + Slack listener (keep running)
uv run python start.py     # Start the workflows (once)
```

### Run with Docker

```bash
docker compose up -d agent                  # Worker
docker compose run --rm start               # Start workflows
docker compose run --rm cli status          # Check status
docker compose run --rm cli proposals       # List tool proposals
docker compose run --rm cli approve <id>    # Approve a proposed tool
```

## Architecture

```
WeeklyResearchWorkflow (parent, continues weekly via continue_as_new)
 ├── AgenticResearchWorkflow ×4 (child per day, isolated history)
 │    └── while not done:
 │          ├── call_llm (activity)
 │          └── execute_tool ×N (activities, each independently retryable)
 ├── compile_report (activity → Slack Block Kit JSON)
 └── send_slack_message (activity)

ToolRegistryWorkflow (long-running, manages dynamic tool proposals)
 ├── Signals: propose_tool, approve_tool, reject_tool
 ├── Queries: get_pending_proposals, get_approved_tools
 └── 15-day TTL auto-expiry

SlackInteractionWorkflow (short-lived, handles "tell me more" clicks)
 ├── call_llm_simple (activity)
 └── send_slack_message (activity, updates thinking message)

Slack Socket Mode Listener (daemon thread in worker process)
 ├── Tool approve/reject → signals ToolRegistryWorkflow
 ├── Interested/dismiss → direct Slack API
 └── More info → starts SlackInteractionWorkflow
```

## Tools

| Tool | Source | Description |
|------|--------|-------------|
| `search_events` | Tavily | Web search for local events |
| `search_outdoors` | Tavily | Outdoor activity search |
| `get_weather` | weather.gov | Multi-day forecast (free, no key) |
| `read_page` | Tavily Extract | Fetch full page content from a URL |
| `save_recommendation` | Internal | Save a finding for the weekly report |
| `propose_new_tool` | Internal | Propose a new tool for human review |

Dynamic tools approved through the HITL flow are loaded from `dynamic_tools/` at the start of each research run.

## CLI

```
uv run python cli.py status                  # Weekly workflow status
uv run python cli.py proposals --pending     # Pending tool proposals
uv run python cli.py review <id>             # View proposed implementation
uv run python cli.py approve <id>            # Approve (writes code + installs deps)
uv run python cli.py reject <id>             # Reject
```

## Configuration

All user preferences live in `config.py` — location, interests, crowd tolerance, travel radius, and per-day research prompts. Edit to taste.
