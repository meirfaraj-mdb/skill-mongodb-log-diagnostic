"""Vertex AI Agent Engine wrapper for the MongoDB log diagnostic pipeline."""
import os as _os
import sys as _sys

_vendor = _os.path.join(_os.path.dirname(__file__),"_vendor")
if _os.path.isdir(_vendor) and _vendor not in _sys.path:
    _sys.path.insert(0,vendor)

# Exposed so  'adk deploy agent-engine' can discover the agent from the package.
from .agent import root_agent


__all__ = ["root_agent"]
