"""
CompileReportWorkflow — synthesizes weekly findings into a Slack Block Kit report.

Separates the LLM call from JSON parsing so that if parsing fails, Temporal
retries only the parse step (the expensive LLM call result is already persisted
in workflow history).
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from activities import call_llm_for_report, parse_report

RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
)


@workflow.defn
class CompileReportWorkflow:
    """Generates the weekly Slack report from accumulated findings + weather."""

    @workflow.run
    async def run(self, findings: list[dict], weather_summary: str) -> dict:
        raw = await workflow.execute_activity(
            call_llm_for_report,
            args=[findings, weather_summary],
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RETRY,
        )

        return await workflow.execute_activity(
            parse_report,
            args=[raw],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RETRY,
        )
