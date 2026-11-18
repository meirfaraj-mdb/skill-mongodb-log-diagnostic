"""MONGODB_LOG_DIAG_ANTHROPIC_BASE_URL is the complete Messages URL, used as-is."""
import copy
import os
import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock

from agent.llm import ANTHROPIC_VERSION, AnthropicLLM, resolve_anthropic_base_url

FULL = "https://gw.example.com/anthropic/v1/messages"


class _FakeAnthropicModule(types.ModuleType):
    def __init__(self):
        super().__init__("anthropic")
        self.calls = []

    def Anthropic(self, **kwargs):  # noqa: N802 - mirrors SDK name
        self.calls.append(kwargs)
        return object()


class _Resp:
    def __init__(self, status, data=None, headers=None):
        self.status_code, self._data, self.headers = status, data or {}, headers or {}

    def json(self):
        return self._data


class _FakeHttp:
    def __init__(self, responses):
        self.responses, self.posts = list(responses), []

    def post(self, url, json=None, headers=None):
        self.posts.append({"url": url, "json": copy.deepcopy(json), "headers": dict(headers or {})})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _ok(text, stop="end_turn"):
    return _Resp(200, {"content": [{"type": "text", "text": text}], "stop_reason": stop,
                       "usage": {"input_tokens": 1, "output_tokens": 1}})


class _EnvCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("ANTHROPIC_BASE_URL", "MONGODB_LOG_DIAG_ANTHROPIC_BASE_URL"):
            os.environ.pop(name, None)


class ResolveUrlTest(_EnvCase):
    def test_default_is_none(self):
        self.assertIsNone(resolve_anthropic_base_url({}))

    def test_complete_url_kept_as_is(self):
        self.assertEqual(resolve_anthropic_base_url({"anthropic_base_url": FULL}), FULL)
        self.assertEqual(resolve_anthropic_base_url({"anthropic_base_url": FULL + "/"}), FULL)

    def test_sdk_base_env_is_not_the_override(self):
        os.environ["ANTHROPIC_BASE_URL"] = "https://proxy.example.com"
        self.assertIsNone(resolve_anthropic_base_url({}))

    def test_url_without_messages_warns_but_is_used(self):
        with self.assertLogs("mongodb_log_agent", level="WARNING"):
            self.assertEqual(resolve_anthropic_base_url({"anthropic_base_url": "https://gw.example.com/x"}),
                             "https://gw.example.com/x")

    def test_bare_host_rejected_with_hint(self):
        for bare in ("https://genai-litellm.example.com", "https://genai-litellm.example.com/"):
            with self.subTest(bare=bare), self.assertRaises(ValueError) as ctx:
                resolve_anthropic_base_url({"anthropic_base_url": bare})
            self.assertIn("https://genai-litellm.example.com/v1/messages", str(ctx.exception))

    def test_http_only_for_localhost(self):
        self.assertEqual(resolve_anthropic_base_url({"anthropic_base_url": "http://localhost:8080/v1/messages"}),
                         "http://localhost:8080/v1/messages")
        with self.assertRaises(ValueError):
            resolve_anthropic_base_url({"anthropic_base_url": "http://gw.example.com/v1/messages"})

    def test_rejects_bad_values(self):
        for bad in ("gw.example.com/v1/messages", "ftp://gw.example.com/v1/messages",
                    "https://user:pw@gw.example.com/v1/messages",
                    "https://gw.example.com/v1/messages?key=x", "https://gw.example.com/v1/messages#f"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                resolve_anthropic_base_url({"anthropic_base_url": bad})


class ClientSelectionTest(_EnvCase):
    def setUp(self):
        super().setUp()
        self.fake = _FakeAnthropicModule()
        patcher = mock.patch.dict(sys.modules, {"anthropic": self.fake})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_uses_sdk_without_base_url(self):
        AnthropicLLM({})
        self.assertEqual(self.fake.calls, [{"timeout": 900.0, "max_retries": 3}])

    def test_override_bypasses_sdk(self):
        model = AnthropicLLM({"anthropic_base_url": FULL}, http_client=_FakeHttp([]))
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(model.client.messages.url, FULL)

    def test_injected_client_untouched(self):
        sentinel = object()
        model = AnthropicLLM({"anthropic_base_url": FULL}, client=sentinel)
        self.assertIs(model.client, sentinel)

    def test_missing_key_still_fails(self):
        os.environ.pop("ANTHROPIC_API_KEY")
        with self.assertRaises(ValueError):
            AnthropicLLM({"anthropic_base_url": FULL}, http_client=_FakeHttp([]))


class StdlibHttpTest(_EnvCase):
    """The default override transport needs no third-party package (no httpx/anthropic)."""

    def setUp(self):
        super().setUp()
        import json as _json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        seen = self.seen = []
        replies = self.replies = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = _json.loads(self.rfile.read(int(self.headers["content-length"])))
                seen.append({"path": self.path, "key": self.headers.get("x-api-key"),
                             "version": self.headers.get("anthropic-version"), "body": body})
                status, payload = replies.pop(0)
                out = _json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/anthropic/v1/messages"

    def test_round_trip_without_httpx(self):
        self.replies.append((200, {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}))
        with mock.patch.dict(sys.modules, {"httpx": None, "anthropic": None}):
            model = AnthropicLLM({"anthropic_base_url": self.url, "anthropic_model": "m"})
            self.assertEqual(model.generate("s", "u"), "ok")
        self.assertEqual(self.seen[0]["path"], "/anthropic/v1/messages")
        self.assertEqual(self.seen[0]["key"], "test-key")
        self.assertEqual(self.seen[0]["version"], ANTHROPIC_VERSION)

    def test_http_error_body_reported(self):
        self.replies.append((401, {"error": {"type": "authentication_error", "message": "bad key"}}))
        model = AnthropicLLM({"anthropic_base_url": self.url})
        with self.assertRaises(RuntimeError) as ctx:
            model.generate("s", "u")
        self.assertIn("HTTP 401", str(ctx.exception))
        self.assertIn("bad key", str(ctx.exception))


class EndpointRequestTest(_EnvCase):
    def test_posts_to_exact_url_with_headers_and_continuation(self):
        http = _FakeHttp([_ok("Part one", "max_tokens"), _ok("Part two")])
        model = AnthropicLLM({"anthropic_base_url": FULL, "anthropic_model": "claude-test",
                              "report_max_tokens": 100}, http_client=http)
        self.assertEqual(model.generate("sys", "payload"), "Part onePart two")
        self.assertEqual([p["url"] for p in http.posts], [FULL, FULL])
        self.assertEqual(http.posts[0]["headers"]["x-api-key"], "test-key")
        self.assertEqual(http.posts[0]["headers"]["anthropic-version"], ANTHROPIC_VERSION)
        self.assertEqual(http.posts[0]["json"], {"model": "claude-test", "max_tokens": 100, "system": "sys",
                                                 "messages": [{"role": "user", "content": "payload"}]})
        self.assertEqual(http.posts[1]["json"]["messages"][1], {"role": "assistant", "content": "Part one"})

    def test_retries_retryable_status_then_succeeds(self):
        http = _FakeHttp([_Resp(529, headers={"retry-after": "0"}), _ok("done")])
        model = AnthropicLLM({"anthropic_base_url": FULL}, http_client=http)
        model.client.messages._sleep = lambda _s: None
        self.assertEqual(model.generate("s", "u"), "done")
        self.assertEqual(len(http.posts), 2)

    def test_client_error_raises_with_endpoint_and_no_key(self):
        http = _FakeHttp([_Resp(404, {"error": {"type": "not_found_error", "message": "no route"}})])
        model = AnthropicLLM({"anthropic_base_url": FULL}, http_client=http)
        with self.assertRaises(RuntimeError) as ctx:
            model.generate("s", "u")
        self.assertIn("HTTP 404", str(ctx.exception))
        self.assertIn(FULL, str(ctx.exception))
        self.assertNotIn("test-key", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
