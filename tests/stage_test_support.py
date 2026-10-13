import gzip
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType('test_agent'); pkg.__path__ = [str(ROOT / 'agent')]
sys.modules['test_agent'] = pkg
common = types.ModuleType('test_agent.common')
class Layout:
    @classmethod
    def from_config(cls, config): return cls()
    def day(self, day): return 'logs/' + day
    def raw_log(self, day, host, name): return f'logs/{day}/{host}/mongodb/{name}.gz'
    def extract(self, day, host, name, file): return f'logs/{day}/{host}/extracts/{name}/{file}'
common.Layout = Layout
common.EXTRACT_FILES = ('extractionOccurence.json', 'extractionshort.json', 'handoff.md')
common.WORK_DIR = Path(tempfile.gettempdir())
import logging
common.logger = logging.getLogger('test')
sys.modules[common.__name__] = common
skills = types.ModuleType('test_agent.skills'); skills.diagnostic_skill_dir = lambda: Path('.')
sys.modules[skills.__name__] = skills
spec = importlib.util.spec_from_file_location('test_agent.extract_stage', ROOT / 'agent/extract_stage.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
class Store:
    def __init__(self, data): self.data = data
    def list_keys(self, prefix): return [k for k in self.data if k.startswith(prefix)]
    def download(self, key, path): Path(path).write_bytes(self.data[key])

