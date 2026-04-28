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

SLACK_BLOCK_LIMIT = 50

# Monday=0 through Thursday=3
RESEARCH_DAYS = {
    0: "Monday",
    1: "Tuesday",
    2: "Wednesday",
    3: "Thursday",
}


def _chunk_blocks(
    blocks: list[dict], limit: int = SLACK_BLOCK_LIMIT
) -> list[list[dict]]:
    """Split blocks into chunks that fit Slack's per-message block limit."""
    if not blocks:
        return [[]]
    return [blocks[i : i + limit] for i in range(0, len(blocks), limit)]


@workflow.defn
class WeeklyResearchWorkflow:
    """Runs perpetually via continue_as_new. One cycle = one week."""

    def __init__(self):
        self.status: str = "starting"
        self.findings: list[dict] = []
        self._retry_synthesis = False

    @workflow.signal
    async def retry_synthesis(self):
        """Signal from a human that the blocking issue (e.g. credits) is resolved."""
        self._retry_synthesis = True

    @workflow.query
    def get_status(self) -> dict:
        return {
            "status": self.status,
            "findings_count": len(self.findings),
        }

    @workflow.run
    async def run(self, location_area: str, slack_channel: str):
        self.findings = []
        self._retry_synthesis = False

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

        failed_days = []

        for i, (day_num, day_name) in enumerate(remaining):
            research_day = now + timedelta(days=today - day_num)
            if i > 0:
                self.status = f"sleeping until {day_name}"
                await self._sleep_until_next_morning()

            self.status = f"researching ({day_name})"
            workflow.logger.info(f"Starting {day_name} research for {location_area}")

            focus = DAILY_RESEARCH_FOCUS[day_name]

            try:
                daily = await workflow.execute_child_workflow(
                    AgenticResearchWorkflow.run,
                    args=[location_area, f"{day_name}, {research_day.date()}", focus],
                    id=f"research-{workflow.now().strftime('%Y-%m-%d')}-{day_name.lower()}",
                    execution_timeout=timedelta(hours=2),
                    retry_policy=RETRY,
                )
                self.findings.extend(daily)
                workflow.logger.info(
                    f"{day_name} complete: {len(daily)} new findings, "
                    f"{len(self.findings)} total"
                )
            except Exception as e:
                failed_days.append(day_name)
                workflow.logger.error(
                    f"{day_name} research failed after all retries: {e}. "
                    f"Preserving {len(self.findings)} findings from prior days."
                )

        # --- Compile and send the weekly report ---
        if not self.findings:
            self.status = "no findings this week, sleeping until next week"
            workflow.logger.warn("No findings collected this week, skipping report.")
            await self._sleep_until_next_monday()
            workflow.continue_as_new(args=[location_area, slack_channel])
            return

        self.status = "compiling report"

        try:
            weather = await workflow.execute_activity(
                get_current_weather_summary,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RETRY,
            )
        except Exception as e:
            workflow.logger.warn(f"Weather fetch failed: {e}")
            weather = "Weather forecast unavailable."

        # Synthesis loop — retries with human-in-the-loop on persistent failure
        synthesis_attempt = 0
        while True:
            synthesis_attempt += 1
            try:
                report = await workflow.execute_child_workflow(
                    CompileReportWorkflow.run,
                    args=[self.findings, weather],
                    id=f"compile-report-{workflow.now().strftime('%Y-%m-%d')}-{synthesis_attempt}",
                    execution_timeout=timedelta(minutes=10),
                )

                chunks = _chunk_blocks(report["blocks"])
                total = len(chunks)
                first = await workflow.execute_activity(
                    send_slack_message,
                    args=[
                        slack_channel,
                        report["text"]
                        if total == 1
                        else f"{report['text']} (1/{total})",
                        chunks[0],
                    ],
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=RETRY,
                )
                for idx, chunk in enumerate(chunks[1:], start=2):
                    await workflow.execute_activity(
                        send_slack_message,
                        args=[
                            slack_channel,
                            f"{report['text']} ({idx}/{total})",
                            chunk,
                            None,
                            first["ts"],
                        ],
                        start_to_close_timeout=timedelta(minutes=2),
                        retry_policy=RETRY,
                    )
                break  # success
            except Exception as e:
                self.status = "synthesis failed — waiting for retry signal"
                workflow.logger.error(
                    f"Report synthesis attempt {synthesis_attempt} failed: {e}"
                )

                try:
                    await workflow.execute_activity(
                        send_slack_message,
                        args=[
                            slack_channel,
                            (
                                f":warning: Weekly report synthesis failed "
                                f"(attempt {synthesis_attempt}): {e}\n\n"
                                f"I have *{len(self.findings)} findings* ready to "
                                f"compile. Once the issue is resolved, send me the "
                                f"`retry_synthesis` signal and I'll try again.\n\n"
                                f"```temporal workflow signal "
                                f"--name retry_synthesis --input '\"null\"' "
                                f"--workflow-id {workflow.info().workflow_id} "
                                f"--namespace weekend-activity-agent```"
                            ),
                            None,
                        ],
                        start_to_close_timeout=timedelta(minutes=2),
                        retry_policy=RETRY,
                    )
                except Exception:
                    workflow.logger.error(
                        "Could not send failure notification to Slack"
                    )

                self._retry_synthesis = False
                await workflow.wait_condition(lambda: self._retry_synthesis)
                self._retry_synthesis = False
                self.status = "retrying synthesis"

        if failed_days:
            workflow.logger.warn(
                f"Weekly report sent but {', '.join(failed_days)} research "
                f"failed. Report based on {len(self.findings)} findings from "
                f"successful days only."
            )

        self.status = "report sent, sleeping until next week"
        workflow.logger.info(f"Weekly report sent. {len(self.findings)} findings.")

        await self._sleep_until_next_monday()
        workflow.continue_as_new(args=[location_area, slack_channel])

    async def _sleep_until_next_morning(self):
        """Sleep until 8:00 AM the next day."""
        now = workflow.now()
        tomorrow_8am = now.replace(
            hour=8, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)
        delta = tomorrow_8am - now
        if delta.total_seconds() > 0:
            await workflow.sleep(delta)

    async def _sleep_until_next_monday(self):
        """Sleep from now until 8:00 AM next Monday."""
        now = workflow.now()
        days_until_monday = (7 - now.weekday()) % 7
        if days_until_monday == 0:
            days_until_monday = 7
        next_monday = now.replace(
            hour=8, minute=0, second=0, microsecond=0
        ) + timedelta(days=days_until_monday)
        delta = next_monday - now
        if delta.total_seconds() > 0:
            await workflow.sleep(delta)
