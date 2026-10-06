"""Google ADK orchestration wrapper around the shared diagnostic pipeline."""
from __future__ import annotations

import logging
import os
from typing import Literal

from .runtime import configure_gcp_runtime

logger = logging.getLogger(__name__)


def _run(stage: str, log_date: str | None = None, skip_existing: bool = True) -> dict:
    """Run a deterministic pipeline stage and return an execution receipt."""
    configure_gcp_runtime()
    from agent.handler import run_pipeline
    return run_pipeline({"stage": stage, "log_date": log_date, "skip_existing": skip_existing,
                         "force_reextract": not skip_existing})


def run_daily_diagnostics(log_date: str | None = None) -> dict:
    """Run the daily pipeline (omitted date means D-1 in configured timezone)."""
    return _run("all", log_date)


def run_existing_bucket_diagnostics(log_date: str) -> dict:
    """Analyze GCS logs at <prefix>/<date>/<host>/<log>.gz without Atlas access.

    Set MONGODB_LOG_DIAG_INPUT_MODE=existing_bucket and provide a date.
    """
    return _run("all", log_date)


def run_diagnostic_stage(
    stage: Literal["download", "extract", "observability", "report"],
    log_date: str | None = None,
    skip_existing: bool = True,
) -> dict:
    """Run one recovery/backfill stage for a YYYY-MM-DD date."""
    return _run(stage, log_date, skip_existing)


def _orchestration_model_from_env() -> str:
    """Load ADK model settings from env without accessing the Atlas secret."""
    configure_gcp_runtime()
    if os.environ.get("MONGODB_LOG_DIAG_LLM_PROVIDER", "vertex").strip().lower() != "vertex":
        raise ValueError("ADK orchestration requires MONGODB_LOG_DIAG_LLM_PROVIDER=vertex")
    # ADK must use Gemini, even if the report pipeline uses a Claude Vertex model.
    model = os.environ.get("MONGODB_LOG_DIAG_ADK_MODEL", "").strip()
    if model.startswith("publishers/google/models/"):
        model = model.removeprefix("publishers/google/models/")
    if not model.startswith("gemini-") or "/" in model or ":" in model:
        raise ValueError("Set MONGODB_LOG_DIAG_ADK_MODEL to an accessible Gemini model")
    project = (os.environ.get("MONGODB_LOG_DIAG_VERTEX_PROJECT")
               or os.environ.get("GOOGLE_CLOUD_PROJECT"))
    location = (os.environ.get("MONGODB_LOG_DIAG_VERTEX_LOCATION")
                or os.environ.get("VERTEX_LOCATION") or "us-central1")
    if not project:
        raise ValueError("Set MONGODB_LOG_DIAG_VERTEX_PROJECT or GOOGLE_CLOUD_PROJECT")
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "TRUE"
    os.environ["GOOGLE_CLOUD_PROJECT"] = project
    os.environ["GOOGLE_CLOUD_LOCATION"] = location
    logger.info("ADK orchestration configured model=%s project=%s location=%s", model, project, location)
    return model


def build_root_agent():
    """Construct the ADK agent with non-secret model configuration."""
    model = _orchestration_model_from_env()
    from google.adk.agents import LlmAgent
    return LlmAgent(
        name="mongodb_log_diagnostic",
        model=model,
        description="Runs MongoDB Atlas log diagnostics and uploads reports to Google Cloud Storage.",
        instruction=(
            "You operate the MongoDB Atlas log diagnostic workflow. "
            "For a daily Atlas API run, call run_daily_diagnostics. "
            "When the user asks to analyze existing GCS data without Atlas API access, "
            "call run_existing_bucket_diagnostics and require a date. For a requested backfill or "
            "recovery, call run_diagnostic_stage with exactly one requested stage. In existing-bucket mode, "
            "do not run observability because it deliberately has no MongoDB or Atlas access. "
            "Do not claim a run completed until the tool returns. Summarize only tool results."
        ),
        tools=[run_daily_diagnostics, run_existing_bucket_diagnostics, run_diagnostic_stage],
    )


root_agent = build_root_agent()
