"""Vertex AI Agent Engine wrapper for the MongoDB log diagnostic pipeline."""

# Exposed so  'adk deploy agent-engine' can discover the agent from the package.
from .agent import root_agent

__all__ = ["root_agent"]
