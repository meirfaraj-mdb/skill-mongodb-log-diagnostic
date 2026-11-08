"""Vertex Gemini uses REST generateContent with the shared AuthorizedSession (no google-genai).
Run from repository root: python3 tests/test_vertex_gemini_rest.py"""
import sys
import unittest

from agent.llm import CONTINUE, VertexLLM


class Response:
    def __init__(self, payload):
        self.status_code = 200
        self.payload = payload
        self.content = b""
        self.headers = {}

    def json(self):
        return self.payload


class Session:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json, timeout))
        return Response(self.payloads.pop(0))


def gemini(text, finish="STOP", thought=None):
    parts = ([{"text": thought, "thought": True}] if thought else []) + [{"text": text}]
    return {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}],
            "usageMetadata": {"totalTokenCount": 1}}


CONFIG = {"vertex_project": "p", "vertex_location": "global", "vertex_model": "gemini-test"}


class VertexGeminiRestTests(unittest.TestCase):
    def test_single_turn_uses_generate_content(self):
        session = Session([gemini("report")])
        llm = VertexLLM(CONFIG, session=session)
        self.assertEqual(llm.generate("SYS", "USER"), "report")
        url, body, timeout = session.calls[0]
        self.assertTrue(url.endswith("/publishers/google/models/gemini-test:generateContent"))
        self.assertEqual(body["systemInstruction"], {"parts": [{"text": "SYS"}]})
        self.assertEqual(body["contents"], [{"role": "user", "parts": [{"text": "USER"}]}])
        self.assertEqual(body["generationConfig"]["maxOutputTokens"], llm.max_tokens)
        self.assertEqual(timeout, 900)

    def test_max_tokens_continues(self):
        session = Session([gemini("a", "MAX_TOKENS"), gemini("b")])
        self.assertEqual(VertexLLM(CONFIG, session=session).generate("S", "U"), "ab")
        contents = session.calls[1][1]["contents"]
        self.assertEqual(contents[1], {"role": "model", "parts": [{"text": "a"}]})
        self.assertEqual(contents[2], {"role": "user", "parts": [{"text": CONTINUE}]})

    def test_thought_parts_are_skipped(self):
        session = Session([gemini("final", thought="hidden reasoning")])
        self.assertEqual(VertexLLM(CONFIG, session=session).generate("S", "U"), "final")

    def test_empty_text_raises(self):
        session = Session([{"candidates": [{"finishReason": "SAFETY"}]}])
        with self.assertRaisesRegex(RuntimeError, "no report text"):
            VertexLLM(CONFIG, session=session).generate("S", "U")

    def test_genai_sdk_not_imported(self):
        VertexLLM(CONFIG, session=Session([gemini("x")])).generate("S", "U")
        self.assertNotIn("google.genai", sys.modules)


if __name__ == "__main__":
    unittest.main()
