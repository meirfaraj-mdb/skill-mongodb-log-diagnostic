import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from agent.report_cve_section import relocate_standalone_cve_status as relocate  # noqa: E402

DRIVER = """## 10. Driver compatibility and CVE review

| Driver | Observed version |
|---|---|
| mongo-go-driver | 1.11.0 |

No match in the offline CVE snapshot refreshed 2026-10-04.
"""
TOP = """## Driver CVE Status

No match in the offline CVE snapshot refreshed 2026-10-04. All observed mongo-csharp-driver and mongo-go-driver
versions returned status: no_match_in_snapshot. See Section 10 for driver version observations.
"""


class RelocateCveStatusTest(unittest.TestCase):
    def test_top_no_match_section_is_removed_driver_section_kept(self):
        md = f"# Report\n\n{TOP}\n## 1. Executive summary\n\n- ok\n\n{DRIVER}"
        out, n = relocate(md)
        self.assertEqual(n, 1)
        self.assertNotIn("Driver CVE Status", out)
        self.assertNotIn("See Section 10", out)
        self.assertIn("## 1. Executive summary", out)
        self.assertIn("## 10. Driver compatibility and CVE review", out)
        self.assertEqual(out.count("No match in the offline CVE snapshot"), 1)

    def test_bold_pseudo_heading_variant(self):
        md = ("# Report\n\n**Driver CVE Status**\n\nNo match in the offline CVE snapshot.\n\n"
              "**Other bold**\n\nkept paragraph\n\n" + DRIVER)
        out, n = relocate(md)
        self.assertEqual(n, 1)
        self.assertNotIn("Driver CVE Status", out)
        self.assertIn("kept paragraph", out)

    def test_unknown_cve_ids_are_moved_into_driver_section(self):
        md = f"# Report\n\n## Driver CVE Status\n\nCVE-2021-20329 review candidate.\n\n## 1. Exec\n\nx\n\n{DRIVER}"
        out, n = relocate(md)
        self.assertEqual(n, 1)
        self.assertLess(out.index("## 10."), out.index("CVE-2021-20329"))
        self.assertNotIn("## Driver CVE Status", out)

    def test_nested_cve_heading_inside_driver_section_is_untouched(self):
        md = "# R\n\n## 10. Driver compatibility\n\n### CVE warnings\n\nCVE-2021-20329 row\n"
        self.assertEqual(relocate(md), (md, 0))

    def test_without_driver_section_ids_are_kept(self):
        md = "# R\n\n## Driver CVE Status\n\nCVE-2021-20329 candidate\n"
        self.assertEqual(relocate(md), (md, 0))

    def test_without_driver_section_no_match_block_is_dropped(self):
        out, n = relocate("# R\n\n" + TOP + "\n## 1. Exec\n\nok\n")
        self.assertEqual(n, 1)
        self.assertNotIn("Driver CVE Status", out)

    def test_driver_compatibility_title_is_not_a_candidate(self):
        md = "# R\n\n## Driver compatibility and CVE review\n\nNo match.\n"
        self.assertEqual(relocate(md), (md, 0))


if __name__ == "__main__":
    unittest.main()
