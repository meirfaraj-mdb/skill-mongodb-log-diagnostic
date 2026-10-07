"""Verify the application diagnostic precedes runtime and ADK imports."""
import ast
import pathlib
import unittest

FILE = pathlib.Path(__file__).resolve().parents[1] / 'google_adk_agent' / 'agent.py'

class ImportOrderTests(unittest.TestCase):
    def test_diagnostic_runs_before_runtime_and_agent_initialization(self):
        tree = ast.parse(FILE.read_text())
        diag_imports = [n.lineno for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == 'tls_diagnostics']
        calls = [n.lineno for n in tree.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name) and n.value.func.id == 'log_gateway_tls_trust']
        runtime_imports = [n.lineno for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == 'runtime']
        root_builds = [n.lineno for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'root_agent' for t in n.targets)]
        self.assertEqual(len(diag_imports), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(runtime_imports), 1)
        self.assertEqual(len(root_builds), 1)
        self.assertLess(diag_imports[0], calls[0])
        self.assertLess(calls[0], runtime_imports[0])
        self.assertLess(runtime_imports[0], root_builds[0])

if __name__ == '__main__':
    unittest.main()
