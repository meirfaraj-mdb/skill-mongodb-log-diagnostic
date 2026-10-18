import importlib.util
from pathlib import Path
import tempfile
import unittest

INSTALLER = Path(__file__).resolve().parents[1] / "scripts" / "install_atlas_wrapper.py"
spec = importlib.util.spec_from_file_location("atlas_installer", INSTALLER)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class AddDependenciesTests(unittest.TestCase):
    def test_adds_dependencies_without_losing_project_fields(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pyproject.toml"
            path.write_text('[project]\nname = "test-agent"\nversion = "0.1.0"\ndependencies = ["existing>=1"]\n\n[tool.hatch]\n', encoding="utf-8")
            installer.add_dependencies(path)
            import tomllib
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["project"]["name"], "test-agent")
            self.assertIn("existing>=1", data["project"]["dependencies"])
            self.assertIn("agent-engine-sdk-langgraph==0.11.8", data["project"]["dependencies"])

    def test_adds_dependency_array_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pyproject.toml"
            path.write_text('[project]\nname = "test-agent"\nversion = "0.1.0"\n\n[tool.hatch]\n', encoding="utf-8")
            installer.add_dependencies(path)
            import tomllib
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertIn("agent-engine-sdk-langgraph==0.11.8", data["project"]["dependencies"])


if __name__ == "__main__":
    unittest.main()
