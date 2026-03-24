"""
Parent workflow: orchestrates the weekly research cycle.

Mon-Thu: spawns a child AgenticResearchWorkflow per day (each with a different focus)
Thu/Fri: compiles accumulated findings into a report, sends it to Slack
Weekend: sleeps
Monday: continue_as_new — starts a fresh week with clean history

Day-aware: if started mid-week, picks up from the current day.
If started on a weekend, sleeps until Monday.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from activities import (
        get_current_weather_summary,
        send_slack_message,
    )
    from config import DAILY_RESEARCH_FOCUS

from .compile_report import CompileReportWorkflow
from .research import AgenticResearchWorkflow

RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
)

# Monday=0 through Thursday=3
RESEARCH_DAYS = {
    0: "Monday",
    1: "Tuesday",
    2: "Wednesday",
    3: "Thursday",
}


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

        now = workflow.now()
        today = now.weekday()  # 0=Mon, 6=Sun

        # If it's Friday-Sunday, sleep until Monday
        if today > 3:
            self.status = "sleeping until Monday"
            await self._sleep_until_next_monday()
            workflow.continue_as_new(args=[location_area, slack_channel])
            return

        # Build the remaining schedule from today onward
        remaining = [
            (day_num, day_name)
            for day_num, day_name in sorted(RESEARCH_DAYS.items())
            if day_num >= today
        ]

        for i, (day_num, day_name) in enumerate(remaining):
            if i > 0:
                self.status = f"sleeping until {day_name}"
                await self._sleep_until_next_morning()

            self.status = f"researching ({day_name})"
            workflow.logger.info(f"Starting {day_name} research for {location_area}")

            focus = DAILY_RESEARCH_FOCUS[day_name]

            daily = await workflow.execute_child_workflow(
                AgenticResearchWorkflow.run,
                args=[location_area, day_name, focus],
                id=f"research-{workflow.now().strftime('%Y-%m-%d')}-{day_name.lower()}",
                execution_timeout=timedelta(hours=2),
                retry_policy=RETRY,
            )
            self.findings.extend(daily)

            workflow.logger.info(
                f"{day_name} complete: {len(daily)} new findings, "
                f"{len(self.findings)} total"
            )

        # --- Compile and send the weekly report ---
        self.status = "compiling report"

        weather = await workflow.execute_activity(
            get_current_weather_summary,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RETRY,
        )

        report = await workflow.execute_child_workflow(
            CompileReportWorkflow.run,
            args=[self.findings, weather],
            id=f"compile-report-{workflow.now().strftime('%Y-%m-%d')}",
            execution_timeout=timedelta(minutes=10),
        )

        await workflow.execute_activity(
            send_slack_message,
            args=[slack_channel, report["text"], report["blocks"]],
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RETRY,
        )

        self.status = "report sent, sleeping until next week"
        workflow.logger.info(f"Weekly report sent. {len(self.findings)} findings.")

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
