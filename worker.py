"""Temporal worker — registers all workflows and activities, starts Slack listener."""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
load_dotenv()

from temporalio.client import Client
from temporalio.worker import Worker

from activities import (
    call_llm,
    call_llm_simple,
    compile_report,
    discuss_tool_proposal,
    execute_tool,
    get_current_weather_summary,
    notify_tool_proposal,
    send_slack_message,
    write_dynamic_tool,
)
from workflows import (
    AgenticResearchWorkflow,
    ConversationWorkflow,
    SlackInteractionWorkflow,
    ToolProposalWorkflow,
    ToolRegistryWorkflow,
    WeeklyResearchWorkflow,
)

TASK_QUEUE = "weekend-activity-agent"


async def main():
    temporal_address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    temporal_namespace = os.environ.get("TEMPORAL_NAMESPACE", "weekend-activity-agent")
    client = await Client.connect(temporal_address, namespace=temporal_namespace)

    # Start Slack Socket Mode listener in a background thread.
    # Shares the Temporal client so interactions route through Temporal.
    from slack_bot import start_socket_mode
    start_socket_mode(client)

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        activity_executor=ThreadPoolExecutor(max_workers=100),
        workflows=[
            WeeklyResearchWorkflow,
            AgenticResearchWorkflow,
            ToolRegistryWorkflow,
            ToolProposalWorkflow,
            ConversationWorkflow,
            SlackInteractionWorkflow,
        ],
        activities=[
            call_llm,
            call_llm_simple,
            compile_report,
            discuss_tool_proposal,
            execute_tool,
            get_current_weather_summary,
            notify_tool_proposal,
            send_slack_message,
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
