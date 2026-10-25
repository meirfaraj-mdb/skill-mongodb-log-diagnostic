import unittest

from agent.report_stage import _strip_internal_content


class InternalReportFilterTests(unittest.TestCase):
    def test_drops_internal_paragraph_but_keeps_customer_evidence(self):
        text = (
            "## Executive summary\n\n"
            "Customer applications saw repeated connection failures. Internal Atlas services are also "
            "affected by an incompatible Java driver version. Next action: validate client TLS configuration.\n"
        )
        result, removed = _strip_internal_content(text)
        self.assertNotIn("Internal Atlas", result)
        self.assertIn("Customer applications saw repeated connection failures", result)
        self.assertIn("Next action: validate client TLS configuration", result)
        self.assertGreater(removed, 0)

    def test_drops_internal_table_rows_only(self):
        text = (
            "| Finding | Action |\n|---|---|\n"
            "| Customer latency | Validate query plans |\n"
            "| mongot compatibility | Upgrade component |\n"
        )
        result, removed = _strip_internal_content(text)
        self.assertIn("Customer latency", result)
        self.assertNotIn("mongot", result)
        self.assertGreater(removed, 0)

    def test_drops_entire_internal_section(self):
        text = (
            "## Customer findings\n\nVisible client errors were observed.\n\n"
            "## Internal platform observations\n\n"
            "A platform component showed a version mismatch.\n\n"
            "## Validation\n\nCheck client-side TLS settings.\n"
        )
        result, _ = _strip_internal_content(text)
        self.assertIn("Visible client errors", result)
        self.assertNotIn("Internal platform observations", result)
        self.assertNotIn("platform component showed", result)
        self.assertIn("Check client-side TLS settings", result)

    def test_does_not_remove_ordinary_atlas_product_context(self):
        text = "## Finding\n\nThe MongoDB Atlas cluster showed customer-facing latency.\n"
        result, removed = _strip_internal_content(text)
        self.assertIn("MongoDB Atlas cluster", result)
        self.assertEqual(removed, 0)

    def test_safe_fallback_when_all_material_is_removed(self):
        result, removed = _strip_internal_content("Internal-only platform notes.")
        self.assertEqual(removed, 1)
        self.assertEqual(
            result.strip(),
            "No customer-facing findings could be substantiated from the supplied evidence.",
        )


if __name__ == "__main__":
    unittest.main()
