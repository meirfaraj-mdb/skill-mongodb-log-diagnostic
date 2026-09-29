"""LLM backends for report generation.

  bedrock : Amazon Bedrock Converse API (boto3; Lambda role auth)
  vertex  : Google Vertex AI (ADC service-account auth)
            - Gemini models   -> publishers/google/models/<m>:generateContent
            - Claude models   -> publishers/anthropic/models/<m>:rawPredict
Both continue automatically when the output hits the token limit.
"""
from __future__ import annotations

import os

from .common import logger

CONTINUE = "Continue the report exactly where you stopped. Do not repeat content."


class BedrockLLM:
    def __init__(self, config: dict, client=None):
        self.model_id = config.get("bedrock_model_id") or os.environ.get("BEDROCK_MODEL_ID") or "eu.anthropic.claude-sonnet-5"
        if not self.model_id:
            raise ValueError("Set bedrock_model_id in the secret or BEDROCK_MODEL_ID")
        self.max_tokens = int(config.get("report_max_tokens", 16000))
        self.max_continuations = int(config.get("report_max_continuations", 3))
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client("bedrock-runtime", region_name=config.get("bedrock_region") or os.environ.get("BEDROCK_REGION") or "eu-west-1",
                                  config=Config(read_timeout=900, connect_timeout=30, retries={"max_attempts": 4, "mode": "adaptive"}))
        self.client = client

    def _inference_config(self) -> dict:
        """Build Converse inference config compatible with the selected model.

        Claude Sonnet 5 rejects ``temperature`` in the Bedrock Converse API.
        Keep the former low-temperature setting for other supported models.
        """
        config = {"maxTokens": self.max_tokens}
        if "claude-sonnet-5" not in self.model_id.lower():
            config["temperature"] = 0.1
        return config

    def generate(self, system_prompt: str, user_content: str) -> str:
        messages = [{"role": "user", "content": [{"text": user_content}]}]
        parts = []
        for turn in range(self.max_continuations + 1):
            resp = self.client.converse(modelId=self.model_id, system=[{"text": system_prompt}], messages=messages,
                                        inferenceConfig=self._inference_config())
            text = "".join(c.get("text", "") for c in resp["output"]["message"]["content"])
            parts.append(text)
            logger.info("Bedrock turn=%d stop=%s usage=%s", turn, resp.get("stopReason"), resp.get("usage"))
            if resp.get("stopReason") != "max_tokens":
                break
            messages += [{"role": "assistant", "content": [{"text": text}]}, {"role": "user", "content": [{"text": CONTINUE}]}]
        return "".join(parts)


class VertexLLM:
    def __init__(self, config: dict, session=None):
        self.model = config.get("vertex_model") or os.environ.get("VERTEX_MODEL") or "claude-sonnet-5"
        self.location = config.get("vertex_location") or os.environ.get("VERTEX_LOCATION", "us-central1")
        self.max_tokens = int(config.get("report_max_tokens", 16000))
        self.max_continuations = int(config.get("report_max_continuations", 3))
        self.project = config.get("vertex_project") or config.get("gcp_project") or os.environ.get("GOOGLE_CLOUD_PROJECT")
        if session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession
            creds, detected = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            self.project = self.project or detected
            session = AuthorizedSession(creds)
        if not self.project:
            raise ValueError("Set vertex_project in the secret or GOOGLE_CLOUD_PROJECT")
        self.session = session
        self.is_claude = self.model.startswith("claude")

    def _url(self) -> str:
        host = "aiplatform.googleapis.com" if self.location == "global" else f"{self.location}-aiplatform.googleapis.com"
        publisher, verb = ("anthropic", "rawPredict") if self.is_claude else ("google", "generateContent")
        return (f"https://{host}/v1/projects/{self.project}/locations/{self.location}"
                f"/publishers/{publisher}/models/{self.model}:{verb}")

    def _post(self, body: dict) -> dict:
        resp = self.session.post(self._url(), json=body, timeout=900)
        if resp.status_code >= 400:
            raise RuntimeError(f"Vertex AI HTTP {resp.status_code}: {resp.text[:1000]}")
        return resp.json()

    def generate(self, system_prompt: str, user_content: str) -> str:
        parts = []
        if self.is_claude:
            messages = [{"role": "user", "content": user_content}]
            for turn in range(self.max_continuations + 1):
                data = self._post({"anthropic_version": "vertex-2023-10-16", "system": system_prompt,
                                   "messages": messages, "max_tokens": self.max_tokens, "temperature": 0.1})
                text = "".join(c.get("text", "") for c in data.get("content", []) if c.get("type") == "text")
                parts.append(text)
                logger.info("Vertex/Claude turn=%d stop=%s usage=%s", turn, data.get("stop_reason"), data.get("usage"))
                if data.get("stop_reason") != "max_tokens":
                    break
                messages += [{"role": "assistant", "content": text}, {"role": "user", "content": CONTINUE}]
        else:
            contents = [{"role": "user", "parts": [{"text": user_content}]}]
            for turn in range(self.max_continuations + 1):
                data = self._post({"systemInstruction": {"parts": [{"text": system_prompt}]}, "contents": contents,
                                   "generationConfig": {"maxOutputTokens": self.max_tokens, "temperature": 0.1}})
                cand = (data.get("candidates") or [{}])[0]
                text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
                parts.append(text)
                logger.info("Vertex/Gemini turn=%d finish=%s usage=%s", turn, cand.get("finishReason"), data.get("usageMetadata"))
                if cand.get("finishReason") != "MAX_TOKENS":
                    break
                contents += [{"role": "model", "parts": [{"text": text}]}, {"role": "user", "parts": [{"text": CONTINUE}]}]
        return "".join(parts)
