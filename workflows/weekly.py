"""
Parent workflow: orchestrates the weekly research cycle.

Mon-Thu: spawns a child AgenticResearchWorkflow per day (each with a different focus)
Thu/Fri: compiles accumulated findings into a report, sends it to Slack
Weekend: sleeps
Monday: continue_as_new — starts a fresh week with clean history
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from activities import (
        compile_report,
        get_current_weather_summary,
        send_slack_message,
    )
    from config import DAILY_RESEARCH_FOCUS

from .research import AgenticResearchWorkflow

RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
)

RESEARCH_SCHEDULE = ["Monday", "Tuesday", "Wednesday", "Thursday"]


@workflow.defn
class WeeklyResearchWorkflow:
    """Runs perpetually via continue_as_new. One cycle = one week."""

    def __init__(self):
        self.status: str = "starting"
        self.findings: list[dict] = []

    @workflow.query
    def get_status(self) -> dict:
        return {
            "status": self.status,
            "findings_count": len(self.findings),
        }

    @workflow.run
    async def run(self, location_area: str, slack_channel: str):
        self.findings = []

        for i, day in enumerate(RESEARCH_SCHEDULE):
            if i > 0:
                # Durable timer — survives worker restarts, deploys, reboot
                self.status = f"sleeping until {day}"
                await self._sleep_until_next_morning()

            self.status = f"researching ({day})"
            workflow.logger.info(f"Starting {day} research for {location_area}")

            focus = DAILY_RESEARCH_FOCUS[day]

            # Spawn daily research as a child workflow — isolated history.
            # Each day's agentic loop can generate many events; keeping it
            # in a child prevents the parent's history from bloating.
            daily = await workflow.execute_child_workflow(
                AgenticResearchWorkflow.run,
                args=[location_area, day, focus],
                id=f"research-{workflow.now().strftime('%Y-%m-%d')}-{day.lower()}",
                execution_timeout=timedelta(hours=2),
                retry_policy=RETRY,
            )
            self.findings.extend(daily)

            workflow.logger.info(
                f"{day} complete: {len(daily)} new findings, "
                f"{len(self.findings)} total"
            )

        # --- Compile and send the weekly report ---
        self.status = "compiling report"

        weather = await workflow.execute_activity(
            get_current_weather_summary,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RETRY,
        )

        # compile_report returns {"blocks": [...], "text": "..."}
        report = await workflow.execute_activity(
            compile_report,
            args=[self.findings, weather],
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RETRY,
        )

        await workflow.execute_activity(
            send_slack_message,
            args=[slack_channel, report["text"], report["blocks"]],
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RETRY,
        )

        self.status = "report sent, sleeping until next week"
        workflow.logger.info(f"Weekly report sent. {len(self.findings)} findings.")

        # Sleep until next Monday, then start fresh.
        await self._sleep_until_next_monday()
        workflow.continue_as_new(args=[location_area, slack_channel])

    async def _sleep_until_next_morning(self):
        """Sleep until 8:00 AM the next day."""
        now = workflow.now()
        tomorrow_8am = now.replace(hour=8, minute=0, second=0, microsecond=0) + timedelta(days=1)
        delta = tomorrow_8am - now
        if delta.total_seconds() > 0:
            await workflow.sleep(delta)

    async def _sleep_until_next_monday(self):
        """Sleep from now until 8:00 AM next Monday."""
        now = workflow.now()
        days_until_monday = (7 - now.weekday()) % 7
        if days_until_monday == 0:
            days_until_monday = 7
        next_monday = now.replace(hour=8, minute=0, second=0, microsecond=0) + timedelta(
            days=days_until_monday
        )
        delta = next_monday - now
        if delta.total_seconds() > 0:
            await workflow.sleep(delta)
