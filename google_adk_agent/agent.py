"""Google ADK root agent deployed to Vertex AI Agent Engine.

The agent is an orchestration wrapper, not a second implementation of diagnostics:
its tools invoke ``agent.handler.run_pipeline`` from the shared pipeline.
"""
from __future__ import annotations

import logging
import os
from typing import Literal

from .runtime import configure_gcp_runtime

logger = logging.getLogger(__name__)


def _run(stage: str, log_date: str | None = None, skip_existing: bool = True) -> dict:
    """Run a deterministic pipeline stage and return a compact execution receipt."""
    configure_gcp_runtime()
    # Import after configuration: providers read cloud/secret environment at invocation.
    from agent.handler import run_pipeline
    result = run_pipeline({"stage": stage, "log_date": log_date, "skip_existing": skip_existing,
                           "force_reextract": not skip_existing})
    return result


def run_daily_diagnostics(log_date: str | None = None) -> dict:
    """Download, extract, compare D-1/D-2/D-8, and upload all reports.

    Use this for a normal daily run. ``log_date`` is YYYY-MM-DD; omitted means D-1
    in the timezone in the existing Secret Manager configuration.
    """
    return _run("all", log_date)


def run_existing_bucket_diagnostics(log_date: str) -> dict:
    """Analyze logs already in GCS without calling the Atlas API.

    Requires the secret to set ``input_mode`` to ``existing_bucket``. The date is
    required and raw logs must already be under ``<prefix>/<date>/<host>/<log>.gz``.
    """
    return _run("all", log_date)


def run_diagnostic_stage(
    stage: Literal["download", "extract", "observability", "report"],
    log_date: str | None = None,
    skip_existing: bool = True,
) -> dict:
    """Run one recovery/backfill stage for a YYYY-MM-DD date.

    Download fetches Atlas logs into GCS. Extraction handles one node at a time and
    skips a node whose canonical extract is already uploaded unless `skip_existing`
    is false. Observability collects node-local indexStats and, for Atlas only,
    Query Shape Insights. Report generates D-2/D-8 diffs when available.
    """
    return _run(stage, log_date, skip_existing)


def _orchestration_model_from_secret() -> str:
    """Read the same secret as the pipeline, without logging its payload.

    ADK needs its model before it can call tools. This runs at agent import, so the
    deployment identity (during import validation) and runtime identity must be able
    to read the secret. Do not call load_config here: that validates Atlas credentials
    and other pipeline settings before the agent can even start.
    """
    configure_gcp_runtime()
    from agent.providers import load_raw_config

    config = load_raw_config("gcp")
    if config.get("llm_provider", "vertex") != "vertex":
        raise ValueError("ADK orchestration requires llm_provider=vertex in the secret")
    model = config.get("vertex_model") or os.environ.get("VERTEX_MODEL")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Set vertex_model to an accessible Gemini model in the Atlas secret")
    model = model.strip()
    if model.startswith("publishers/google/models/"):
        model = model.removeprefix("publishers/google/models/")
    if not model.startswith("gemini-") or "/" in model or ":" in model:
        raise ValueError("ADK orchestration requires a Gemini vertex_model in the Atlas secret; "
                         "Claude report models cannot be used by this LlmAgent")

    project = config.get("vertex_project") or config.get("gcp_project") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = config.get("vertex_location") or os.environ.get("VERTEX_LOCATION") or "us-central1"
    if not project:
        raise ValueError("Set vertex_project or gcp_project in the Atlas secret")
    # configure_gcp_runtime has already resolved ATLAS_SECRET_ID; changing the model
    # project here must not move secret lookup to another project on later tool calls.
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "TRUE"
    os.environ["GOOGLE_CLOUD_PROJECT"] = project
    os.environ["GOOGLE_CLOUD_LOCATION"] = location
    logger.info("ADK orchestration configured model=%s project=%s location=%s", model, project, location)
    return model


def build_root_agent():
    """Construct the ADK agent using the report model from the runtime secret."""
    model = _orchestration_model_from_secret()
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


# Google ADK / Agent Engine discovery convention.
root_agent = build_root_agent()
