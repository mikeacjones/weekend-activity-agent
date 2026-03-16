"""Start the weekly research workflow and tool registry."""

import asyncio
import os

from dotenv import load_dotenv
from temporalio.client import Client

from workflows import ToolRegistryWorkflow, WeeklyResearchWorkflow

TASK_QUEUE = "weekend-activity-agent"


async def main():
    load_dotenv()

    temporal_address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    temporal_namespace = os.environ.get("TEMPORAL_NAMESPACE", "weekend-activity-agent")
    slack_channel = os.environ.get("SLACK_CHANNEL_ID", "")
    client = await Client.connect(temporal_address, namespace=temporal_namespace)

    # Start the tool registry (long-running, manages dynamic tool proposals)
    try:
        await client.start_workflow(
            ToolRegistryWorkflow.run,
            id="tool-registry",
            task_queue=TASK_QUEUE,
        )
        print("Started ToolRegistryWorkflow (id: tool-registry)")
    except Exception as e:
        if "already started" in str(e).lower() or "already running" in str(e).lower():
            print("ToolRegistryWorkflow already running.")
        else:
            raise

    # Start the weekly research cycle
    try:
        handle = await client.start_workflow(
            WeeklyResearchWorkflow.run,
            args=["Reynoldstown, Atlanta, GA", slack_channel],
            id="weekly-events-reynoldstown",
            task_queue=TASK_QUEUE,
        )
        print(f"Started WeeklyResearchWorkflow (id: {handle.id})")
    except Exception as e:
        if "already started" in str(e).lower() or "already running" in str(e).lower():
            print("WeeklyResearchWorkflow already running.")
        else:
            raise


def main_sync():
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
