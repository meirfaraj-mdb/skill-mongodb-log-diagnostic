"""No-network regression tests for the report generation SDK migration."""
import importlib.util
import logging
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class GoogleGenAITests(unittest.TestCase):
    def _load(self):
        source = Path(__file__).resolve().parents[1] / 'agent' / 'llm.py'
        package = types.ModuleType('agent')
        package.__path__ = [str(source.parent)]
        common = types.ModuleType('agent.common')
        common.logger = logging.getLogger('test-genai')
        spec = importlib.util.spec_from_file_location('agent.llm', source)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'agent': package, 'agent.common': common}):
            spec.loader.exec_module(module)
        return module

    def test_gemini_uses_client_and_continues_with_history(self):
        calls = []
        finish = lambda value: types.SimpleNamespace(value=value)
        def response(text, reason):
            return types.SimpleNamespace(
                candidates=[types.SimpleNamespace(
                    content=types.SimpleNamespace(parts=[types.SimpleNamespace(text=text)]),
                    finish_reason=finish(reason))], usage_metadata=None)
        responses = iter([response('first ', 'MAX_TOKENS'), response('second', 'STOP')])
        def generate_content(**kw):
            calls.append(kw)
            return next(responses)
        client = types.SimpleNamespace(models=types.SimpleNamespace(generate_content=generate_content))
        fake_types = types.ModuleType('google.genai.types')
        fake_types.Part = types.SimpleNamespace(from_text=lambda text: types.SimpleNamespace(text=text))
        fake_types.Content = lambda **kw: types.SimpleNamespace(**kw)
        fake_types.GenerateContentConfig = lambda **kw: types.SimpleNamespace(**kw)
        google = types.ModuleType('google')
        google.__path__ = []
        genai = types.ModuleType('google.genai')
        genai.__path__ = []
        genai.types = fake_types
        google.genai = genai
        with patch.dict(sys.modules, {'google': google, 'google.genai': genai,
                                      'google.genai.types': fake_types}):
            llm = self._load().VertexLLM(
                {'vertex_model': 'publishers/google/models/gemini-2.5-flash',
                 'vertex_project': 'example', 'vertex_location': 'global'}, client=client)
            self.assertEqual(llm.generate('safe instruction', 'redacted extract'), 'first second')
        self.assertEqual([call['model'] for call in calls], ['gemini-2.5-flash'] * 2)
        self.assertEqual(calls[1]['contents'][1].role, 'model')
        self.assertEqual(calls[1]['contents'][2].role, 'user')
        self.assertEqual(calls[0]['config'].system_instruction, 'safe instruction')

    def test_client_explicitly_uses_vertex_v1(self):
        captured = {}
        fake_types = types.ModuleType('google.genai.types')
        fake_types.HttpOptions = lambda **kw: types.SimpleNamespace(**kw)
        genai = types.ModuleType('google.genai')
        genai.__path__ = []
        genai.types = fake_types
        def client_factory(**kw):
            captured.update(kw)
            return object()
        genai.Client = client_factory
        google = types.ModuleType('google')
        google.__path__ = []
        google.genai = genai
        with patch.dict(sys.modules, {'google': google, 'google.genai': genai,
                                      'google.genai.types': fake_types}):
            self._load().VertexLLM({'vertex_model': 'gemini-2.5-flash',
                                     'vertex_project': 'example', 'vertex_location': 'global'})
        self.assertTrue(captured['vertexai'])
        self.assertEqual(captured['project'], 'example')
        self.assertEqual(captured['location'], 'global')
        self.assertEqual(captured['http_options'].api_version, 'v1')

    def test_claude_still_uses_raw_predict(self):
        llm = self._load().VertexLLM(
            {'vertex_model': 'publishers/anthropic/models/claude-sonnet-5',
             'vertex_project': 'example'}, session=object())
        with patch.object(llm, '_post', return_value={
                'content': [{'type': 'text', 'text': 'ok'}], 'stop_reason': 'end_turn'}):
            self.assertEqual(llm.generate('system', 'input'), 'ok')


if __name__ == '__main__':
    unittest.main()
