"""No-network tests for apply_gateway_fixes.py on representative source shapes."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).with_name("apply_gateway_fixes.py")


def fixture(root):
    sources = {
        'google_adk_agent/agent.py': '''
import logging
import os
from .runtime import configure_gcp_runtime
logger = logging.getLogger(__name__)
def _orchestration_model_from_secret() -> str:
    configure_gcp_runtime()
    from agent.providers import load_raw_config
    config = load_raw_config("gcp")
    return config["vertex_model"]
def build_root_agent():
    model = _orchestration_model_from_secret()
    from google.adk.agents import LlmAgent
    return LlmAgent(name="diagnostic", model=model)
root_agent = build_root_agent()
''',
        'google_adk_agent/runtime.py': '''
import os
def configure_gcp_runtime() -> None:
    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    os.environ["ATLAS_SECRET_ID"] = "projects/" + project + "/secrets/atlas-log-agent/versions/latest"
''',
        'agent/providers.py': '''
import json
import os
import logging
logger = logging.getLogger(__name__)
CLOUD_DEFAULTS = {"gcp": {"storage_provider": "gcs", "llm_provider": "vertex"}}
def detect_cloud():
    return "gcp"
def load_raw_config(cloud):
    raise AssertionError("SECRET SHOULD NOT BE READ")
def normalize_config(raw, cloud):
    return {**raw, "storage_provider": "gcs", "llm_provider": "vertex", "cluster_name": "test"}
def load_config():
    cloud = detect_cloud()
    cfg = normalize_config(load_raw_config(cloud), cloud)
    return cfg
''',
        'agent/handler.py': '''
import os
from . import download_stage, extract_stage, observability_stage, report_stage, skills
from .common import logger
from .providers import load_config
_CONFIG = None
def _config():
    return load_config()
def run_pipeline(event: dict, config: dict | None = None) -> dict:
    config = config or _config()
    stage = event.get("stage") or "all"
    log_date = event.get("log_date") or os.environ.get("LOG_DATE")
    if not log_date or log_date == "auto":
        log_date = skills.atlas_logs().previous_day(config["timezone"])
    out: dict = {"log_date": log_date, "stage": stage, "cloud": config.get("cloud")}
    logs = event.get("logs")
    if stage in ("all", "download"):
        if config.get("input_mode") == "existing_bucket":
            out["download"] = {"status": "skipped_existing_bucket", "log_date": log_date,
                               "logs": logs or []}
        else:
            out["download"] = download_stage.run(config, log_date)
            logs = out["download"]["logs"]
    if stage in ("all", "extract"):
        out["extract"] = extract_stage.run(config, log_date, logs=logs,
                                           skip_existing=not event.get("force_reextract", False))
    if stage in ("all", "observability"):
        out["observability"] = observability_stage.run(config, log_date)
    if stage in ("all", "report", "cluster-summary"):
        out["report"] = report_stage.run(config, log_date, summary_only=(stage == "cluster-summary"))
    return out
''',
        'requirements-gcp.txt': 'google-adk>=1.0.0\ngoogle-cloud-aiplatform[agent_engines]>=1.93.0\n',
        'google_adk_agent/requirements.txt': 'google-adk>=1.0.0\ngoogle-cloud-aiplatform[agent_engines]>=1.93.0\n',
    }
    for name, text in sources.items():
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(textwrap.dedent(text).lstrip())
    for package in ('agent', 'google_adk_agent'):
        (root/package/'__init__.py').touch()


class GatewayFixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fixture(self.root)
        self.run_script()

    def run_script(self):
        subprocess.run([sys.executable, str(SCRIPT)], cwd=self.root, check=True,
                       capture_output=True, text=True)

    def test_repeatable_and_compiles(self):
        before = {str(p.relative_to(self.root)):p.read_text() for p in self.root.rglob('*.py')}
        self.run_script()
        self.assertEqual(before, {str(p.relative_to(self.root)):p.read_text() for p in self.root.rglob('*.py')})
        for p in self.root.rglob('*.py'):
            compile(p.read_text(), str(p), 'exec')

    def test_bucket_config_does_not_load_secret(self):
        module = types.ModuleType('patched_providers')
        exec((self.root/'agent/providers.py').read_text(), module.__dict__)
        with patch.dict(os.environ, {
            'MONGODB_LOG_DIAG_INPUT_MODE': 'existing_bucket',
            'MONGODB_LOG_DIAG_BUCKET': 'test-bucket',
            'MONGODB_LOG_DIAG_PREFIX': 'atlas-logs',
            'MONGODB_LOG_DIAG_LOG_NAMES': '["auto"]',
        }, clear=True):
            cfg = module.load_config()
        self.assertEqual(cfg['input_mode'], 'existing_bucket')
        self.assertEqual(cfg['log_names'], ['auto'])
        self.assertEqual(cfg['bucket'], 'test-bucket')

    def test_root_agent_does_not_fetch_secret_at_import(self):
        google = types.ModuleType('google')
        google.__path__ = []
        adk = types.ModuleType('google.adk')
        adk.__path__ = []
        agents = types.ModuleType('google.adk.agents')
        agents.LlmAgent = lambda **kw: types.SimpleNamespace(**kw)
        with patch.dict(sys.modules, {'google': google, 'google.adk': adk,
                                      'google.adk.agents': agents}):
            with patch.dict(os.environ, {
                'MONGODB_LOG_DIAG_VERTEX_MODEL': 'publishers/google/models/gemini-2.5-flash',
                'MONGODB_LOG_DIAG_VERTEX_PROJECT': 'test-project',
                'MONGODB_LOG_DIAG_INPUT_MODE': 'existing_bucket',
            }, clear=True):
                runtime = types.ModuleType('google_adk_agent.runtime')
                exec((self.root/'google_adk_agent/runtime.py').read_text(), runtime.__dict__)
                pkg = types.ModuleType('google_adk_agent')
                pkg.__path__ = [str(self.root/'google_adk_agent')]
                with patch.dict(sys.modules, {'google_adk_agent': pkg,
                                              'google_adk_agent.runtime': runtime}):
                    spec = importlib.util.spec_from_file_location('google_adk_agent.agent', self.root/'google_adk_agent/agent.py')
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    self.assertEqual(module.root_agent.model, 'gemini-2.5-flash')
                    runtime.configure_gcp_runtime()
                    self.assertNotIn('ATLAS_SECRET_ID', os.environ)

    def test_bucket_all_skips_live_observability(self):
        handler = self.root/'agent/handler.py'
        self.assertIn('"skipped_existing_bucket"', handler.read_text())
        self.assertIn('observability_stage.run(config, log_date)', handler.read_text())
        # Exact branch assertions ensure explicit stage is also skipped, while Atlas path remains.
        import ast
        tree = ast.parse(handler.read_text())
        run = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run_pipeline')
        branch = next(n for n in run.body if isinstance(n, ast.If) and
                      'observability' in ast.unparse(n.test))
        self.assertIsInstance(branch.body[0], ast.If)
        self.assertIn('existing_bucket', ast.unparse(branch.body[0].test))
        self.assertIn('observability_stage.run', ast.unparse(branch.body[0].orelse))

    def test_minimum_versions(self):
        for path in ('requirements-gcp.txt', 'google_adk_agent/requirements.txt'):
            text = (self.root/path).read_text()
            self.assertIn('google-adk>=1.18.0', text)
            self.assertIn('google-cloud-aiplatform[agent_engines]>=1.126.1', text)


if __name__ == '__main__':
    unittest.main()
