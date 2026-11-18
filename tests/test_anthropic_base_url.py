import os
import sys
import types
import unittest
from unittest import mock

from agent import llm
from agent.llm import AnthropicLLM, resolve_anthropic_base_url


class _FakeAnthropicModule(types.ModuleType):
    def __init__(self):
        super().__init__("anthropic")
        self.calls = []

    def Anthropic(self, **kwargs):  # noqa: N802 - mirrors SDK name
        self.calls.append(kwargs)
        return object()


class ResolveBaseUrlTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("ANTHROPIC_BASE_URL", None)

    def test_default_is_none(self):
        self.assertIsNone(resolve_anthropic_base_url({}))

    def test_config_value_and_trailing_slash(self):
        self.assertEqual(resolve_anthropic_base_url({"anthropic_base_url": "https://gw.example.com/anthropic/"}),
                         "https://gw.example.com/anthropic")

    def test_standard_env_fallback(self):
        os.environ["ANTHROPIC_BASE_URL"] = "https://proxy.example.com"
        self.assertEqual(resolve_anthropic_base_url({}), "https://proxy.example.com")

    def test_config_wins_over_standard_env(self):
        os.environ["ANTHROPIC_BASE_URL"] = "https://proxy.example.com"
        self.assertEqual(resolve_anthropic_base_url({"anthropic_base_url": "https://gw.example.com"}),
                         "https://gw.example.com")

    def test_http_only_for_localhost(self):
        self.assertEqual(resolve_anthropic_base_url({"anthropic_base_url": "http://localhost:8080"}),
                         "http://localhost:8080")
        with self.assertRaises(ValueError):
            resolve_anthropic_base_url({"anthropic_base_url": "http://gw.example.com"})

    def test_rejects_bad_values(self):
        for bad in ("gw.example.com", "ftp://gw.example.com", "https://user:pw@gw.example.com",
                    "https://gw.example.com?key=x", "https://gw.example.com#frag"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                resolve_anthropic_base_url({"anthropic_base_url": bad})


class AnthropicClientTest(unittest.TestCase):
    def setUp(self):
        self.fake = _FakeAnthropicModule()
        patches = [mock.patch.dict(sys.modules, {"anthropic": self.fake}),
                   mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=False)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("ANTHROPIC_BASE_URL", None)

    def test_default_endpoint_not_passed(self):
        AnthropicLLM({})
        self.assertEqual(self.fake.calls, [{"timeout": 900.0, "max_retries": 3}])

    def test_override_passed_to_sdk(self):
        model = AnthropicLLM({"anthropic_base_url": "https://gw.example.com/"})
        self.assertEqual(model.base_url, "https://gw.example.com")
        self.assertEqual(self.fake.calls[0]["base_url"], "https://gw.example.com")

    def test_injected_client_untouched(self):
        sentinel = object()
        model = AnthropicLLM({"anthropic_base_url": "https://gw.example.com"}, client=sentinel)
        self.assertIs(model.client, sentinel)
        self.assertEqual(self.fake.calls, [])


if __name__ == "__main__":
    unittest.main()
