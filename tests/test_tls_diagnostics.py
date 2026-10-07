"""No-network tests for read-only container trust inspection."""
import hashlib
import importlib.util
import os
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "google_adk_agent" / "tls_diagnostics.py"
spec = importlib.util.spec_from_file_location("tls_diagnostics_under_test", MODULE)
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)


class TrustDiagnosticsTests(unittest.TestCase):
    def test_empty_fingerprint_is_explicitly_not_checked(self):
        with patch.dict(os.environ, {}, clear=True):
            result = diag.gateway_tls_trust_diagnostic()
        self.assertIsNone(result["gateway_ca_sha256"])
        self.assertIn("not_checked", result["fingerprint_check_status"])

    def test_invalid_fingerprint_does_not_log_certificate(self):
        with patch.dict(os.environ, {diag.FINGERPRINT_ENV: "bad"}):
            result = diag.gateway_tls_trust_diagnostic()
        self.assertIn("diagnostic_error", result)
        self.assertNotIn("container_ca_file_scan", result)

    def test_bundles_and_hash_links_found_without_exposing_pem(self):
        context = ssl.create_default_context()
        der = context.get_ca_certs(binary_form=True)[0]
        fingerprint = hashlib.sha256(der).hexdigest().upper()
        pem = ssl.DER_cert_to_PEM_cert(der)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / "ca-bundle.crt"
            bundle.write_text(pem + pem)
            (root / "abcdef01.0").symlink_to(bundle.name)
            result = diag._scan_store([str(bundle)], [temp], fingerprint)
            self.assertTrue(result["gateway_ca_found_in_scanned_files"])
            self.assertEqual(result["matching_paths"], [str(bundle)])
            self.assertNotIn("BEGIN CERTIFICATE", str(result))

    def test_present_in_container_context_and_store(self):
        der = ssl.create_default_context().get_ca_certs(binary_form=True)[0]
        fingerprint = hashlib.sha256(der).hexdigest().upper()
        with patch.dict(os.environ, {diag.FINGERPRINT_ENV: fingerprint}), patch.object(diag, "_scan_store", return_value={"gateway_ca_found_in_scanned_files": True}):
            result = diag.gateway_tls_trust_diagnostic()
        self.assertTrue(result["gateway_ca_in_python_default_context"])
        self.assertFalse(result["grpc_effective_roots_checked"])


if __name__ == "__main__":
    unittest.main()
