"""Vertex report calls stream by default so an egress gateway / proxy idle timeout cannot cut a long
report (Agent Engine behind an agent gateway answered 504 / read timeout on non-streamed rawPredict).
Run from repository root: python3 -m unittest tests.test_vertex_streaming"""
import io
import json
import logging
import os
import unittest
from unittest.mock import patch

from agent.common import logger
from agent.llm import CONTINUE, VertexLLM, _assemble_gemini_chunks

CLAUDE = {"vertex_project": "p", "vertex_location": "europe-west1", "vertex_model": "claude-sonnet-5"}
GEMINI = {"vertex_project": "p", "vertex_location": "global", "vertex_model": "gemini-test"}


def sse(*events):
    """SSE lines as the server sends them (event:/data:/blank), with a keepalive comment first."""
    out = [": keepalive", ""]
    for name, payload in events:
        if name:
            out.append(f"event: {name}")
        out.extend([f"data: {json.dumps(payload)}", ""])
    return out


def claude_events(text, stop="end_turn", pieces=3):
    step = max(1, len(text) // pieces)
    chunks = [text[i:i + step] for i in range(0, len(text), step)] or [""]
    return sse(("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 10}}}),
               ("content_block_start", {"type": "content_block_start", "index": 0,
                                        "content_block": {"type": "text", "text": ""}}),
               ("ping", {"type": "ping"}),
               *[("content_block_delta", {"type": "content_block_delta", "index": 0,
                                          "delta": {"type": "text_delta", "text": c}}) for c in chunks],
               ("content_block_stop", {"type": "content_block_stop", "index": 0}),
               ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop},
                                  "usage": {"output_tokens": 5}}),
               ("message_stop", {"type": "message_stop"}))


def gemini_events(text, finish="STOP", thought=None):
    chunks = ([{"candidates": [{"content": {"role": "model", "parts": [{"text": thought, "thought": True}]}}]}]
              if thought else [])
    half = len(text) // 2
    chunks += [{"candidates": [{"content": {"role": "model", "parts": [{"text": text[:half]}]}}]},
               {"candidates": [{"content": {"role": "model", "parts": [{"text": text[half:]}]},
                                "finishReason": finish}], "usageMetadata": {"totalTokenCount": 7}}]
    return sse(*[(None, c) for c in chunks])


class StreamResponse:
    def __init__(self, lines, status=200, content_type="text/event-stream", payload=None):
        self.status_code, self._lines, self._payload = status, lines, payload
        self.headers = {"Content-Type": content_type, "x-goog-request-id": "req-1"}
        self.content = b"" if payload is None else json.dumps(payload).encode()
        self.closed = False

    def iter_lines(self):
        for line in self._lines:
            if isinstance(line, Exception):
                raise line
            yield line.encode()

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def close(self):
        self.closed = True


class StreamSession:
    """Same post() signature as requests / google AuthorizedSession."""
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def post(self, url, json=None, timeout=None, stream=False, **kwargs):
        self.calls.append({"url": url, "json": json, "timeout": timeout, "stream": stream})
        return self.responses.pop(0)


class VertexClaudeStreaming(unittest.TestCase):
    def test_streams_by_default_with_stream_raw_predict(self):
        session = StreamSession([StreamResponse(claude_events("full report text"))])
        llm = VertexLLM(CLAUDE, session=session)
        self.assertTrue(llm.stream)
        self.assertEqual(llm.generate("SYS", "USER"), "full report text")
        call = session.calls[0]
        self.assertTrue(call["url"].endswith("/publishers/anthropic/models/claude-sonnet-5:streamRawPredict"))
        self.assertTrue(call["stream"])
        self.assertEqual(call["timeout"], (30, 900))  # read timeout = max silence between chunks
        self.assertIs(call["json"]["stream"], True)
        self.assertEqual(call["json"]["anthropic_version"], "vertex-2023-10-16")

    def test_max_tokens_continuation_still_works(self):
        session = StreamSession([StreamResponse(claude_events("part one ", "max_tokens")),
                                 StreamResponse(claude_events("part two"))])
        self.assertEqual(VertexLLM(CLAUDE, session=session).generate("S", "U"), "part one part two")
        messages = session.calls[1]["json"]["messages"]
        self.assertEqual(messages[1], {"role": "assistant", "content": "part one "})
        self.assertEqual(messages[2], {"role": "user", "content": CONTINUE})

    def test_truncated_stream_raises_instead_of_half_report(self):
        lines = claude_events("abc")
        lines = lines[:lines.index("event: message_delta")]  # connection cut before the stop reason
        resp = StreamResponse(lines)
        with self.assertRaises(ConnectionError):
            VertexLLM(CLAUDE, session=StreamSession([resp])).generate("S", "U")
        self.assertTrue(resp.closed)

    def test_stream_error_event_raises(self):
        lines = sse(("error", {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}))
        with self.assertRaisesRegex(RuntimeError, "overloaded_error"):
            VertexLLM(CLAUDE, session=StreamSession([StreamResponse(lines)])).generate("S", "U")

    def test_buffering_proxy_json_answer_is_accepted(self):
        payload = {"content": [{"type": "text", "text": "buffered"}], "stop_reason": "end_turn"}
        resp = StreamResponse([], content_type="application/json", payload=payload)
        self.assertEqual(VertexLLM(CLAUDE, session=StreamSession([resp])).generate("S", "U"), "buffered")

    def test_504_has_gateway_hint(self):
        resp = StreamResponse([], status=504, content_type="text/html")
        with self.assertRaisesRegex(RuntimeError, "HTTP 504.*before the first streamed byte"):
            VertexLLM(CLAUDE, session=StreamSession([resp])).generate("S", "U")

    def test_opt_out_uses_raw_predict(self):
        payload = {"content": [{"type": "text", "text": "plain"}], "stop_reason": "end_turn"}
        session = StreamSession([StreamResponse([], content_type="application/json", payload=payload)])
        with patch.dict(os.environ, {"MONGODB_LOG_DIAG_VERTEX_STREAM": "false"}):
            llm = VertexLLM(CLAUDE, session=session)
        self.assertFalse(llm.stream)
        self.assertEqual(llm.generate("S", "U"), "plain")
        self.assertTrue(session.calls[0]["url"].endswith(":rawPredict"))
        self.assertNotIn("stream", session.calls[0]["json"])
        self.assertEqual(session.calls[0]["timeout"], 900)

    def test_non_stream_504_hint_suggests_streaming(self):
        resp = StreamResponse([], status=504, content_type="text/html")
        with self.assertRaisesRegex(RuntimeError, "enable streaming"):
            VertexLLM({**CLAUDE, "vertex_stream": False}, session=StreamSession([resp])).generate("S", "U")

    def test_timeout_setting(self):
        session = StreamSession([StreamResponse(claude_events("x"))])
        VertexLLM({**CLAUDE, "vertex_timeout_seconds": 300}, session=session).generate("S", "U")
        self.assertEqual(session.calls[0]["timeout"], (30, 300))
        with self.assertRaises(ValueError):
            VertexLLM({**CLAUDE, "vertex_timeout_seconds": -1}, session=session)


class VertexGeminiStreaming(unittest.TestCase):
    def test_streams_with_sse_generate_content(self):
        session = StreamSession([StreamResponse(gemini_events("gemini report", thought="hidden"))])
        self.assertEqual(VertexLLM(GEMINI, session=session).generate("SYS", "USER"), "gemini report")
        call = session.calls[0]
        self.assertTrue(call["url"].endswith("/models/gemini-test:streamGenerateContent?alt=sse"))
        self.assertTrue(call["stream"])
        self.assertNotIn("stream", call["json"])  # Gemini selects streaming by method, not body
        self.assertEqual(call["json"]["systemInstruction"], {"parts": [{"text": "SYS"}]})

    def test_max_tokens_continuation(self):
        session = StreamSession([StreamResponse(gemini_events("first ", "MAX_TOKENS")),
                                 StreamResponse(gemini_events("second"))])
        self.assertEqual(VertexLLM(GEMINI, session=session).generate("S", "U"), "first second")
        self.assertEqual(session.calls[1]["json"]["contents"][1], {"role": "model", "parts": [{"text": "first "}]})

    def test_truncated_stream_raises(self):
        lines = gemini_events("abcdef")[:-2]  # last chunk (with finishReason) never arrives
        with self.assertRaisesRegex(ConnectionError, "before finishReason"):
            VertexLLM(GEMINI, session=StreamSession([StreamResponse(lines)])).generate("S", "U")

    def test_safety_block_keeps_existing_error(self):
        lines = sse((None, {"candidates": [{"finishReason": "SAFETY"}]}))
        with self.assertRaisesRegex(RuntimeError, "no report text"):
            VertexLLM(GEMINI, session=StreamSession([StreamResponse(lines)])).generate("S", "U")

    def test_buffered_json_array(self):
        chunks = [{"candidates": [{"content": {"parts": [{"text": "a"}]}}]},
                  {"candidates": [{"content": {"parts": [{"text": "b"}]}, "finishReason": "STOP"}]}]
        resp = StreamResponse([], content_type="application/json", payload=chunks)
        self.assertEqual(VertexLLM(GEMINI, session=StreamSession([resp])).generate("S", "U"), "ab")

    def test_assemble_error_chunk(self):
        with self.assertRaisesRegex(RuntimeError, "RESOURCE_EXHAUSTED"):
            _assemble_gemini_chunks([{"error": {"status": "RESOURCE_EXHAUSTED", "message": "quota"}}])


class LegacyStubFallback(unittest.TestCase):
    def test_stub_without_stream_kwarg_uses_non_streaming_call(self):
        class Resp:
            status_code, headers, content = 200, {}, b""
            def json(self): return {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
        class Session:
            def __init__(self): self.calls = []
            def post(self, url, json=None, timeout=None):
                self.calls.append(url); return Resp()
        s = Session()
        llm = VertexLLM(CLAUDE, session=s)
        self.assertFalse(llm.stream)
        self.assertEqual(llm.generate("S", "U"), "ok")
        self.assertTrue(s.calls[0].endswith(":rawPredict"))


class Logging(unittest.TestCase):
    def test_logs_streaming_mode_without_prompt(self):
        buf = io.StringIO(); h = logging.StreamHandler(buf)
        logger.addHandler(h); old = logger.level; logger.setLevel(logging.INFO)
        try:
            VertexLLM(CLAUDE, session=StreamSession([StreamResponse(claude_events("x"))])).generate("S", "SECRET_PROMPT")
        finally:
            logger.removeHandler(h); logger.setLevel(old)
        self.assertIn("streaming=True", buf.getvalue())
        self.assertIn("stream=True", buf.getvalue())
        self.assertNotIn("SECRET_PROMPT", buf.getvalue())


class RealHttpStreaming(unittest.TestCase):
    """Real sockets (urllib3 is not available everywhere, so a tiny urllib-based session is used):
    the server stays silent less than the read timeout between chunks, but the whole answer takes
    much longer than the read timeout. Only a streamed call survives that."""

    def test_slow_stream_longer_than_read_timeout_completes(self):
        import http.server
        import threading
        import time
        from urllib import request as urlrequest

        events = claude_events("slow but complete report", pieces=6)

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                Handler.body = body
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for line in events:
                    self.wfile.write((line + "\n").encode()); self.wfile.flush()
                    if line == "":
                        time.sleep(0.15)  # 9 gaps * 0.15 s = ~1.4 s total, each gap < 0.5 s timeout

            def log_message(self, *a): pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]

        class Resp:
            def __init__(self, raw):
                self.raw, self.status_code = raw, raw.status
                self.headers = dict(raw.headers); self.content = b""
            def iter_lines(self):
                for line in self.raw:
                    yield line.rstrip(b"\r\n")
            def close(self): self.raw.close()

        class UrllibSession:
            def post(self, url, json=None, timeout=None, stream=False):
                local = "http://127.0.0.1:%d/%s" % (port, url.split("/", 3)[3])
                req = urlrequest.Request(local, data=__import__("json").dumps(json).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
                read_timeout = timeout[1] if isinstance(timeout, tuple) else timeout
                return Resp(urlrequest.urlopen(req, timeout=read_timeout))

        try:
            started = time.monotonic()
            llm = VertexLLM({**CLAUDE, "vertex_timeout_seconds": 1}, session=UrllibSession())
            llm.timeout = 0.5  # sub-second read timeout for the test
            self.assertEqual(llm.generate("S", "U"), "slow but complete report")
            self.assertGreater(time.monotonic() - started, 0.5)  # total time > read timeout
            self.assertIs(Handler.body["stream"], True)
        finally:
            server.shutdown(); server.server_close()


if __name__ == "__main__":
    unittest.main()
