"""Tests for env-only non-secret configuration and credentials-only secret."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from agent import providers


class EnvSecretSplitTests(unittest.TestCase):
    def test_existing_bucket_never_reads_secret(self):
        with patch.dict(os.environ, {
            "MONGODB_LOG_DIAG_INPUT_MODE": "existing_bucket",
            "MONGODB_LOG_DIAG_BUCKET": "logs-bucket",
            "MONGODB_LOG_DIAG_LOG_NAMES": "auto,mongod",
            "MONGODB_LOG_DIAG_CLUSTER_SUMMARY": "false",
            "MONGODB_LOG_DIAG_EXPECTED_NODE_COUNT": "3",
            "ATLAS_SECRET_ID": "not-accessible",
        }, clear=True):
            self.assertEqual(providers.load_raw_config("gcp"), {})
            cfg = providers.normalize_config({}, "gcp")
            self.assertEqual(cfg["input_mode"], "existing_bucket")
            self.assertEqual(cfg["log_names"], ["auto", "mongod"])
            self.assertIs(cfg["cluster_summary"], False)
            self.assertEqual(cfg["expected_node_count"], 3)

    def test_secret_rejects_noncredential_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secret.json"
            path.write_text(json.dumps({"atlas_public_key": "public",
                                        "atlas_private_key": "private", "bucket": "old-bucket"}))
            with patch.dict(os.environ, {"MONGODB_LOG_DIAG_INPUT_MODE": "atlas_api",
                                      "ATLAS_CONFIG_FILE": str(path)}, clear=True):
                with self.assertRaisesRegex(ValueError, "only atlas_public_key"):
                    providers.load_raw_config("local")

    def test_secret_accepts_only_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secret.json"
            credentials = {"atlas_public_key": "public", "atlas_private_key": "private"}
            path.write_text(json.dumps(credentials))
            with patch.dict(os.environ, {"MONGODB_LOG_DIAG_INPUT_MODE": "atlas_api",
                                      "ATLAS_CONFIG_FILE": str(path)}, clear=True):
                self.assertEqual(providers.load_raw_config("local"), credentials)

    def test_invalid_env_values_fail(self):
        with patch.dict(os.environ, {"MONGODB_LOG_DIAG_EXPECTED_NODE_COUNT": "three"}, clear=True):
            with self.assertRaisesRegex(ValueError, "EXPECTED_NODE_COUNT"):
                providers._env_config()
        with patch.dict(os.environ, {"MONGODB_LOG_DIAG_CLUSTER_SUMMARY": "maybe"}, clear=True):
            with self.assertRaisesRegex(ValueError, "CLUSTER_SUMMARY"):
                providers._env_config()


if __name__ == "__main__":
    unittest.main()
