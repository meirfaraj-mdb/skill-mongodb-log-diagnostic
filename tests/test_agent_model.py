"""Offline model selection tests; never contact Secret Manager or Vertex."""
import importlib
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in (
            "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "ATLAS_SECRET_ID")}
        self.raw = {"llm_provider": "vertex", "vertex_model": "publishers/google/models/gemini-2.5-flash",
                    "vertex_project": "report-project", "vertex_location": "global"}
        self.providers = types.ModuleType("agent.providers")
        self.providers.load_raw_config = lambda cloud: self.raw
        self.agent_package = types.ModuleType("agent")
        self.agent_package.__path__ = []
        self.runtime = types.ModuleType("google_adk_agent.runtime")
        self.runtime.configure_gcp_runtime = lambda: os.environ.setdefault(
            "ATLAS_SECRET_ID", "projects/secret-project/secrets/atlas-log-agent/versions/latest")
        self.adk = types.ModuleType("google.adk")
        self.agents = types.ModuleType("google.adk.agents")
        self.agents.LlmAgent = lambda **kwargs: kwargs
        self.modules = patch.dict(sys.modules, {
            "agent": self.agent_package, "agent.providers": self.providers,
            "google_adk_agent.runtime": self.runtime, "google": types.ModuleType("google"),
            "google.adk": self.adk, "google.adk.agents": self.agents})
        self.modules.start()
        sys.modules.pop("google_adk_agent.agent", None)

    def tearDown(self):
        sys.modules.pop("google_adk_agent.agent", None)
        self.modules.stop()
        for key, value in self.saved.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value

    def test_qualified_model_and_location(self):
        module = importlib.import_module("google_adk_agent.agent")
        self.assertEqual(module.root_agent["model"], "gemini-2.5-flash")
        self.assertEqual(os.environ["GOOGLE_CLOUD_PROJECT"], "report-project")
        self.assertEqual(os.environ["GOOGLE_CLOUD_LOCATION"], "global")
        self.assertEqual(os.environ["GOOGLE_GENAI_USE_VERTEXAI"], "TRUE")
        self.assertIn("secret-project", os.environ["ATLAS_SECRET_ID"])

    def test_bare_model(self):
        self.raw["vertex_model"] = "gemini-2.5-pro"
        self.assertEqual(importlib.import_module("google_adk_agent.agent").root_agent["model"], "gemini-2.5-pro")

    def test_rejects_claude_and_bad_publisher(self):
        for value in ("claude-sonnet-5", "publishers/anthropic/models/claude-sonnet-5", "publishers/google/models/evil/path"):
            self.raw["vertex_model"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                importlib.import_module("google_adk_agent.agent")
            sys.modules.pop("google_adk_agent.agent", None)


if __name__ == "__main__": unittest.main()
