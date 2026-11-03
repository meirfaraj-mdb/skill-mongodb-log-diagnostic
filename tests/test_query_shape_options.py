"""Command options keep their logged values in query shapes and render on one line in APPENDIX 3."""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "skills/mongodb-log-diagnostic/scripts"))
import extract_mongodb_log as extractor  # noqa: E402
from agent.report_shape_mapping import append_reported_shapes, split_options  # noqa: E402

FIND = {"find": "orders", "filter": {"status": "PAID", "batchSize": 7, "x": {"$in": [1, 2, 3]}},
        "batchSize": 101, "singleBatch": False, "maxTimeMS": 30000, "limit": 1, "$db": "shop",
        "$readPreference": {"mode": "secondaryPreferred", "maxStalenessSeconds": 90},
        "readConcern": {"level": "majority", "afterClusterTime": {"$timestamp": {"t": 1, "i": 2}}},
        "comment": "customer 42 lookup", "lsid": {"id": "abc"}}


class ExtractorOptionValuesTest(unittest.TestCase):
    def test_find_options_keep_values_and_literals_stay_redacted(self):
        shape = extractor.query_shape(FIND, keep_options=True)
        self.assertEqual(shape["batchSize"], 101)
        self.assertIs(shape["singleBatch"], False)
        self.assertEqual(shape["maxTimeMS"], 30000)
        self.assertEqual(shape["$db"], "shop")
        self.assertEqual(shape["$readPreference"], {"mode": "secondaryPreferred", "maxStalenessSeconds": 90})
        self.assertEqual(shape["readConcern"], {"level": "majority", "afterClusterTime": "?"})
        # user data stays redacted, including a filter field that happens to be called batchSize
        self.assertEqual(shape["filter"], {"status": "?", "batchSize": "?", "x": {"$in": ["?", "?", "?"]}})
        self.assertEqual(shape["find"], "?")
        self.assertEqual(shape["comment"], "?")
        self.assertNotIn("lsid", shape)

    def test_default_query_shape_unchanged(self):
        self.assertEqual(extractor.query_shape({"batchSize": 5})["batchSize"], "?")

    def test_unsafe_option_text_is_redacted(self):
        shape = extractor.query_shape({"find": "c", "$db": "a b{}", "hint": "idx_1"}, keep_options=True)
        self.assertEqual(shape["$db"], "?")
        self.assertEqual(shape["hint"], "idx_1")

    def test_getmore_keeps_originating_and_own_options(self):
        shape = extractor._command_shape({"command": {"getMore": 1, "batchSize": 500}, "originatingCommand": FIND})
        self.assertEqual(shape["batchSize"], 101)
        self.assertEqual(shape["getMoreOptions"], {"batchSize": 500})

    def test_original_log_command_keeps_options(self):
        line = '{"t":{"$date":"2026-01-01T00:00:00Z"},"attr":{"command":{"find":"c","filter":{"a":5},"batchSize":10,"$db":"d"}}}'
        import json
        cmd = json.loads(extractor.redact_original_log(line))["attr"]["command"]
        self.assertEqual((cmd["batchSize"], cmd["$db"], cmd["filter"]["a"]), (10, "d", "?"))


class AppendixOptionsLineTest(unittest.TestCase):
    def test_options_on_one_line_and_not_in_json(self):
        shape = extractor.query_shape(FIND, keep_options=True)
        extraction = {"slow": {"operations": [{"query_hash": "ABCDEF12", "namespace": "shop.orders",
                                                "sample_query_shape": shape}]}}
        md = append_reported_shapes("# R\n\nhash ABCDEF12\n", extraction)
        line = next(ln for ln in md.splitlines() if ln.startswith("**Options:**"))
        for text in ("`$db: shop`", "`readPreference: secondaryPreferred (maxStalenessSeconds=90)`",
                     "`readConcern: majority`", "`batchSize: 101`", "`singleBatch: false`", "`maxTimeMS: 30000`"):
            self.assertIn(text, line)
        body = md.split("```json", 1)[1].split("```", 1)[0]
        for key in ("$db", "$readPreference", "readConcern", "maxTimeMS", "singleBatch"):
            self.assertNotIn(key, body)
        self.assertIn('"batchSize": "?"', body)  # the user filter field, still redacted
        self.assertNotIn("PAID", md)

    def test_legacy_placeholders_still_compact(self):
        text, body = split_options({"find": "?", "batchSize": "?", "$db": "?", "cursor": {}})
        self.assertEqual(text, "`$db: ?` · `batchSize: ?`")
        self.assertEqual(body, {"find": "?"})


if __name__ == "__main__":
    unittest.main()
