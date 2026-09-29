"""Google ADK root agent deployed to Vertex AI Agent Engine.

The agent is an orchestration wrapper, not a second implementation of diagnostics:
its tools invoke ``agent.handler.run_pipeline`` from the shared pipeline.
"""
from __future__ import annotations

from typing import Literal


def _configure_gcp_runtime() -> None:
    """Configure GCP runtime when the optional ADK runtime helper is available."""
    try:
        from google_adk_agent.runtime import configure_gcp_runtime
        configure_gcp_runtime()
    except ModuleNotFoundError:
        # Local/shared pipeline invocation does not require ADK dependencies.
        pass


def _run(stage: str, log_date: str | None = None, skip_existing: bool = True) -> dict:
    """Run a deterministic pipeline stage and return its execution receipt."""
    _configure_gcp_runtime()
    from agent.handler import run_pipeline
    return run_pipeline({
        "stage": stage,
        "log_date": log_date,
        "skip_existing": skip_existing,
        "force_reextract": not skip_existing,
    })


def run_daily_diagnostics(log_date: str | None = None) -> dict:
    """Download, extract, compare D-1/D-2/D-8, and upload reports."""
    return _run("all", log_date)


def run_existing_bucket_diagnostics(log_date: str) -> dict:
    """Analyze existing bucket logs without calling the Atlas API."""
    return _run("all", log_date)


def run_diagnostic_stage(
    stage: Literal["download", "extract", "observability", "report"],
    log_date: str | None = None,
    skip_existing: bool = True,
) -> dict:
    """Run one recovery, backfill, or observability stage."""
    return _run(stage, log_date, skip_existing)


def build_root_agent():
    """Construct lazily so local pipeline tests need not install Google ADK."""
    from google.adk.agents import LlmAgent
    return LlmAgent(
        name="mongodb_log_diagnostic",
        model="claude-sonnet-5",
        description="Runs MongoDB Atlas log diagnostics and uploads reports to cloud storage.",
        instruction=(
            "You operate the MongoDB log diagnostic workflow. For a daily Atlas API run, "
            "call run_daily_diagnostics. When the user asks to analyze existing bucket data "
            "without Atlas API access, call run_existing_bucket_diagnostics and require a date. "
            "For recovery or backfill, call run_diagnostic_stage with exactly one requested stage. "
            "Do not claim a run completed until the tool returns. Summarize only tool results."
        ),
        tools=[run_daily_diagnostics, run_existing_bucket_diagnostics, run_diagnostic_stage],
    )


# Google ADK / Agent Engine discovery convention.
root_agent = build_root_agent()
