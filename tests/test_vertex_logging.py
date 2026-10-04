"""Offline diagnostics smoke tests. Run from repository root: python3 tests/test_vertex_logging.py"""
import io
import logging
import unittest
from agent.llm import VertexLLM
from agent.common import logger


class Response:
    def __init__(self, status, payload=None, content=b"", headers=None):
        self.status_code = status
        self.payload = payload
        self.content = content
        self.headers = headers or {}

    def json(self):
        if self.payload is None:
            raise ValueError("empty body")
        return self.payload


class Session:
    def __init__(self, response=None, failure=None):
        self.response = response
        self.failure = failure
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json, timeout))
        if self.failure:
            raise self.failure
        return self.response


class VertexLoggingTests(unittest.TestCase):
    def setUp(self):
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.previous_level = logger.level
        logger.addHandler(self.handler)
        logger.setLevel(logging.INFO)
        self.config = {"vertex_project": "test-project", "vertex_location": "global", "vertex_model": "gemini-test"}

    def tearDown(self):
        logger.removeHandler(self.handler)
        logger.setLevel(self.previous_level)

    def test_qualified_gemini_model_does_not_duplicate_publisher_path(self):
        config = {**self.config, "vertex_model": "publishers/google/models/gemini-3.8-flash"}
        llm = VertexLLM(config, session=Session(Response(404)))
        self.assertEqual(llm.model, "gemini-3.8-flash")
        self.assertEqual(llm._url(), "https://aiplatform.googleapis.com/v1/projects/test-project/locations/global/publishers/google/models/gemini-3.8-flash:generateContent")
        with self.assertRaisesRegex(RuntimeError, "model=gemini-3.8-flash"):
            llm._post({})
        self.assertNotIn("/models/publishers/", self.stream.getvalue())

    def test_qualified_claude_routes_to_anthropic(self):
        config = {**self.config, "vertex_model": "publishers/anthropic/models/claude-sonnet-5"}
        llm = VertexLLM(config, session=Session(Response(404)))
        self.assertTrue(llm.is_claude)
        self.assertTrue(llm._url().endswith("/publishers/anthropic/models/claude-sonnet-5:rawPredict"))

    def test_invalid_model_path_fails_before_request(self):
        for model in ("publishers/google/models/", "publishers/google/models/foo/bar", "projects/x/locations/global/publishers/google/models/gemini-test"):
            with self.subTest(model=model), self.assertRaises(ValueError):
                VertexLLM({**self.config, "vertex_model": model}, session=Session(Response(200)))

    def test_blank_404_has_routing_metadata_but_no_prompt_or_headers(self):
        session = Session(Response(404, content=b"", headers={"Content-Type": "text/plain", "x-goog-request-id": "req-123", "Set-Cookie": "SECRET_COOKIE"}))
        llm = VertexLLM(self.config, session=session)
        with self.assertRaisesRegex(RuntimeError, "Vertex AI HTTP 404.*model=gemini-test"):
            llm._post({"contents": "SECRET_PROMPT"})
        logs = self.stream.getvalue()
        for expected in ("project=test-project", "location=global", "publisher=google", "model=gemini-test", "http_status=404", "response_bytes=0", "request_id=req-123", "/locations/global/"):
            self.assertIn(expected, logs)
        for secret in ("SECRET_PROMPT", "SECRET_COOKIE"):
            self.assertNotIn(secret, logs)

    def test_structured_error_and_success(self):
        session = Session(Response(404, {"error": {"code": 404, "status": "NOT_FOUND", "message": "model not available"}}))
        with self.assertRaises(RuntimeError):
            VertexLLM(self.config, session=session)._post({})
        self.assertIn("error_status=NOT_FOUND", self.stream.getvalue())
        self.assertIn("error_message=model not available", self.stream.getvalue())
        self.stream.seek(0); self.stream.truncate(0)
        session = Session(Response(200, {"candidates": []}))
        self.assertEqual(VertexLLM(self.config, session=session)._post({}), {"candidates": []})
        self.assertIn("Vertex request complete", self.stream.getvalue())

    def test_transport_failure_logs_class_without_exception_text(self):
        session = Session(failure=OSError("SECRET_TOKEN"))
        with self.assertRaises(OSError):
            VertexLLM(self.config, session=session)._post({})
        self.assertIn("error_type=OSError", self.stream.getvalue())
        self.assertNotIn("SECRET_TOKEN", self.stream.getvalue())


if __name__ == "__main__":
    unittest.main()
