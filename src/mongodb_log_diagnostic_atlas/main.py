"""LangGraph orchestration wrapper for the shared MongoDB diagnostics pipeline."""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Literal


def _add_shared_source_to_path() -> None:
    """Find the workspace root containing the unchanged agent/ and skills/ dirs."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "agent" / "handler.py").is_file() and (parent / "skills").is_dir():
            root = str(parent)
            if root not in sys.path:
                sys.path.insert(0, root)
            os.environ.setdefault("SKILLS_DIR", str(parent / "skills"))
            return
    raise RuntimeError(
        "Shared pipeline source was not packaged. Run scripts/install_atlas_wrapper.py "
        "to copy agent/ and skills/ into the Atlas workspace."
    )


_add_shared_source_to_path()

from .runtime import atlas_credentials_file, configure_pipeline_runtime  # noqa: E402

from agent_engine_sdk_langgraph import App  # noqa: E402
from langchain_core.messages import SystemMessage  # noqa: E402
from langgraph.graph import END, START, MessagesState, StateGraph  # noqa: E402
from langgraph.prebuilt import ToolNode  # noqa: E402

APP_NAME = "mongodb-log-diagnostic"
SYSTEM_PROMPT = """You operate the MongoDB log diagnostic pipeline.

Use the registered tools to run diagnostics; do not claim a run completed until a
pipeline tool returns successfully. For a daily Atlas API run, call
run_daily_diagnostics. For files already in the configured bucket, use
run_existing_bucket_diagnostics and require the exact YYYY-MM-DD date. For
recovery/backfill, call run_diagnostic_stage with the single requested stage.
Do not invent results, paths, or diagnoses. Summarize only the tool receipt.
"""

app = App(app_name=APP_NAME, app_version="1.0.0")


def _run(event: dict) -> str:
    """Run the existing pipeline and return a compact JSON receipt to the model."""
    configure_pipeline_runtime()
    with atlas_credentials_file():
        from agent.handler import run_pipeline
        result = run_pipeline(event)
    # The pipeline returns receipts/URIs, not raw logs. Bound tool output in case
    # a backfill contains many nodes, while preserving valid JSON.
    receipt = {
        key: result[key]
        for key in ("log_date", "stage", "cloud", "download", "extract", "observability", "report")
        if key in result
    }
    encoded = json.dumps(receipt, default=str, ensure_ascii=False)
    if len(encoded) > 24_000:
        encoded = json.dumps({
            "log_date": result.get("log_date"),
            "stage": result.get("stage"),
            "cloud": result.get("cloud"),
            "receipt_truncated": True,
            "note": "The pipeline completed but its full receipt was omitted due to size.",
        }, ensure_ascii=False)
    return encoded


@app.tool()
def run_daily_diagnostics(log_date: str | None = None, skip_existing: bool = True) -> str:
    """Run the complete daily Atlas log diagnostic pipeline.

    Args:
        log_date: Optional YYYY-MM-DD date. Omit to use the configured previous day.
        skip_existing: Reuse already downloaded/extracted artifacts when true.
    """
    if log_date:
        date.fromisoformat(log_date)
    return _run({"stage": "all", "log_date": log_date, "skip_existing": skip_existing,
                 "force_reextract": not skip_existing})


@app.tool()
def run_existing_bucket_diagnostics(log_date: str) -> str:
    """Analyze logs already in the configured bucket for one YYYY-MM-DD date.

    This mode does not download new Atlas logs. Set MONGODB_LOG_DIAG_INPUT_MODE=existing_bucket.
    """
    date.fromisoformat(log_date)
    if os.environ.get("MONGODB_LOG_DIAG_INPUT_MODE", "atlas_api").strip().lower() != "existing_bucket":
        raise ValueError("Set MONGODB_LOG_DIAG_INPUT_MODE=existing_bucket before using this tool")
    return _run({"stage": "all", "log_date": log_date, "skip_existing": True})


@app.tool()
def run_diagnostic_stage(
    stage: Literal["download", "extract", "observability", "report", "cluster-summary"],
    log_date: str | None = None,
    skip_existing: bool = True,
) -> str:
    """Run one pipeline stage for recovery or backfill.

    Args:
        stage: Exactly one stage: download, extract, observability, report, or cluster-summary.
        log_date: Optional YYYY-MM-DD date; omit only when the pipeline can infer it.
        skip_existing: Reuse existing artifacts where supported.
    """
    if log_date:
        date.fromisoformat(log_date)
    return _run({"stage": stage, "log_date": log_date, "skip_existing": skip_existing,
                 "force_reextract": not skip_existing})


def _build_model():
    """Build an orchestration model; separate from the pipeline's report LLM."""
    provider = os.environ.get("ATLAS_AGENT_MODEL_PROVIDER", "bedrock").strip().lower()
    model_name = (os.environ.get("ATLAS_AGENT_MODEL")
                  or os.environ.get("MONGODB_LOG_DIAG_BEDROCK_MODEL_ID", "")).strip()
    if not model_name:
        raise ValueError(
            "Set ATLAS_AGENT_MODEL to an accessible model ID. This orchestration model is "
            "separate from MONGODB_LOG_DIAG_LLM_PROVIDER used for report generation."
        )

    if provider == "bedrock":
        from langchain_aws import ChatBedrockConverse
        kwargs = {"model_id": model_name, "temperature": 0}
        region = (os.environ.get("ATLAS_AGENT_MODEL_REGION")
                  or os.environ.get("MONGODB_LOG_DIAG_BEDROCK_REGION")
                  or os.environ.get("AWS_REGION"))
        if region:
            kwargs["region_name"] = region
        # Uses boto3's standard credential chain; prefer scoped runtime identity.
        return ChatBedrockConverse(**kwargs)

    if provider == "anthropic-compatible":
        from langchain_anthropic import ChatAnthropic
        base_url = os.environ.get("ATLAS_AGENT_MODEL_BASE_URL", "").strip()
        api_key = os.environ.get("LLM_API_KEY")
        if not base_url or not api_key:
            raise ValueError(
                "anthropic-compatible requires ATLAS_AGENT_MODEL_BASE_URL and the LLM_API_KEY secret"
            )
        return ChatAnthropic(model=model_name, base_url=base_url, api_key=api_key, temperature=0)

    raise ValueError("ATLAS_AGENT_MODEL_PROVIDER must be bedrock|anthropic-compatible")


@app.entrypoint
def build_agent():
    """Create the Atlas-wrapped LangGraph tool-calling graph."""
    llm = app.llm(_build_model())
    tools = app.get_tools()
    llm_with_tools = llm.bind_tools(app.get_tool_schemas())

    def call_model(state: MessagesState):
        messages = [SystemMessage(content=SYSTEM_PROMPT), *state["messages"]]
        return {"messages": [llm_with_tools.invoke(messages)]}

    graph = StateGraph(MessagesState)
    graph.add_node("agent", call_model)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "agent")

    def route_tools(state: MessagesState):
        last = state["messages"][-1]
        return "tools" if getattr(last, "tool_calls", None) else END

    graph.add_conditional_edges("agent", route_tools, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile(checkpointer=app.checkpointer())


app.run()
