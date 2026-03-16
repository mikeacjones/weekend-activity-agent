"""Temporal worker — registers all workflows and activities, starts Slack listener."""

import asyncio
import os

from dotenv import load_dotenv
load_dotenv()

from temporalio.client import Client
from temporalio.worker import Worker

from activities import (
    call_llm,
    call_llm_simple,
    compile_report,
    execute_tool,
    get_current_weather_summary,
    notify_tool_proposal,
    send_slack_message,
    write_dynamic_tool,
)
from workflows import (
    AgenticResearchWorkflow,
    SlackInteractionWorkflow,
    ToolRegistryWorkflow,
    WeeklyResearchWorkflow,
)

TASK_QUEUE = "weekend-activity-agent"


async def main():
    temporal_address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    client = await Client.connect(temporal_address)

    # Start Slack Socket Mode listener in a background thread.
    # Shares the Temporal client so interactions route through Temporal.
    from slack_bot import start_socket_mode
    start_socket_mode(client)

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[
            WeeklyResearchWorkflow,
            AgenticResearchWorkflow,
            ToolRegistryWorkflow,
            SlackInteractionWorkflow,
        ],
        activities=[
            call_llm,
            call_llm_simple,
            execute_tool,
            compile_report,
            send_slack_message,
            notify_tool_proposal,
            get_current_weather_summary,
            write_dynamic_tool,
        ],
    )

    print(f"Worker started. Task queue: {TASK_QUEUE}")
    print(f"Temporal: {temporal_address}")
    await worker.run()


def main_sync():
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
