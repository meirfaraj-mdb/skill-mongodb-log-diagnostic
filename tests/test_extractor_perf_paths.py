"""Regression tests for the extractor speed-up (pre-filter, fast JSON, streaming input, parser 1.5)."""
import gzip
import importlib
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/mongodb-log-diagnostic/scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))
import extract_mongodb_log as xm  # noqa: E402
from agent import extract_stage, report_shape_mapping, resource_investigation  # noqa: E402
from agent.providers import LocalStore  # noqa: E402

SLOW = ('{"t":{"$date":"2026-10-30T00:01:20.000Z"},"s":"I","c":"COMMAND","id":51803,"ctx":"conn1",'
        '"msg":"Slow query","attr":{"type":"command","ns":"shop.orders","appName":"api","command":{"find":"orders",'
        '"filter":{"status":"A"},"limit":5,"$db":"shop"},"planSummary":"COLLSCAN","planCacheKey":"11AA22BB",'
        '"queryFramework":"classic","queryHash":"1234ABCD","keysExamined":0,"docsExamined":%d,"nreturned":5,'
        '"durationMillis":%d,"cpuNanos":1000,"storage":{"data":{"bytesRead":10}}}}')
ROUTINE = '{"t":{"$date":"2026-10-30T00:02:00.000Z"},"s":"I","c":"NETWORK","id":22943,"ctx":"listener","msg":"Connection accepted","attr":{"remote":"10.0.0.1:5000"}}'


def run_extractor(text: str, *extra: str, env=None, stdin=False) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        cmd = [sys.executable, str(SCRIPTS / "extract_mongodb_log.py"), "--output", str(out), "--slow-ms", "100", *extra]
        if stdin:
            subprocess.run(cmd + ["--input-name", "node-input.gz", "-"], input=text.encode(), check=True,
                           capture_output=True, env=env)
        else:
            log = Path(tmp) / "node-input.gz"
            log.write_bytes(gzip.compress(text.encode()))
            subprocess.run(cmd + [str(log)], check=True, capture_output=True, env=env)
        return {n: (out / n).read_bytes() for n in ("extractionOccurence.json", "extractionshort.json", "handoff.md")}


class FastJsonTests(unittest.TestCase):
    def reload_with(self, choice, modules):
        with patch.dict(os.environ, {"MONGODB_LOG_DIAG_JSON": choice}), patch.dict(sys.modules, modules):
            return xm._select_fast_loads()

    def test_backend_order_msgspec_then_orjson_then_stdlib(self):
        decoder = types.SimpleNamespace(decode=json.loads)
        msgspec = types.ModuleType("msgspec"); msgspec.json = types.SimpleNamespace(Decoder=lambda: decoder)
        orjson = types.ModuleType("orjson"); orjson.loads = json.loads
        self.assertEqual(self.reload_with("auto", {"msgspec": msgspec, "orjson": orjson})[0], "msgspec")
        self.assertEqual(self.reload_with("auto", {"msgspec": None, "orjson": orjson})[0], "orjson")
        self.assertEqual(self.reload_with("orjson", {"msgspec": msgspec, "orjson": orjson})[0], "orjson")
        self.assertEqual(self.reload_with("auto", {"msgspec": None, "orjson": None}), ("json", None))
        self.assertEqual(self.reload_with("stdlib", {"msgspec": msgspec, "orjson": orjson}), ("json", None))

    def test_unsafe_inputs_always_use_stdlib(self):
        for text in ('{"a":"\\ud800"}', '{"n":1234567890123456789}', '{"a":NaN}', '{"a":1e999}', '{"a":"x\ty"}'):
            self.assertTrue(xm._FAST_UNSAFE.search(text), text)
        self.assertIsNone(xm._FAST_UNSAFE.search(SLOW % (10, 200)))

    def test_fast_decoder_errors_fall_back_to_stdlib_outcome(self):
        def broken(_):
            raise RuntimeError("decoder-specific")
        with patch.object(xm, "_FAST_LOADS", broken):
            self.assertEqual(xm._loads_line('{"a":1}'), {"a": 1})
            with self.assertRaises(json.JSONDecodeError):
                xm._loads_line("2026-10-30T00:00:00.000+0000 I NETWORK [conn1] legacy text")

    def test_backend_does_not_change_output(self):
        text = "\n".join([SLOW % (900, 300), ROUTINE, SLOW % (50, 120)]) + "\n"
        stub = Path(tempfile.mkdtemp()) / "msgspec"
        stub.mkdir()
        (stub / "__init__.py").write_text("import json as _j\nclass _D:\n    decode = staticmethod(_j.loads)\n"
                                          "json = type('m', (), {'Decoder': _D})\n")
        env_fast = {**os.environ, "PYTHONPATH": str(stub.parent), "MONGODB_LOG_DIAG_JSON": "msgspec"}
        env_std = {**os.environ, "MONGODB_LOG_DIAG_JSON": "stdlib"}
        self.assertEqual(run_extractor(text, env=env_fast), run_extractor(text, env=env_std))


class StreamingInputTests(unittest.TestCase):
    def test_stdin_equals_file_input(self):
        text = "\n".join([SLOW % (900, 300), ROUTINE] * 3) + "\n"
        self.assertEqual(run_extractor(text, stdin=True), run_extractor(text))

    def test_blocks_keep_split_and_newline_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "s"; root.mkdir()
            files = {"mongod.log.part-1.gz": gzip.compress(b'{"a":1}\n{"b":'), "mongod.log.part-2": b'2}\n{"c":3}',
                     "mongod.log.1.gz": gzip.compress(b'{"d":4}'), "mongod.log.2": b'{"e":5}\n'}
            for key, data in files.items():
                (root / key).write_bytes(data)
            store = LocalStore(root)
            expected = b'{"a":1}\n{"b":2}\n{"c":3}\n{"d":4}\n{"e":5}\n'
            self.assertEqual(b"".join(extract_stage.iter_input_blocks(store, list(files), Path(tmp))), expected)

            class DownloadOnly:  # stores without open_read still work (temp file per source)
                def download(self, key, path):
                    store.download(key, path)
            self.assertEqual(b"".join(extract_stage.iter_input_blocks(DownloadOnly(), list(files), Path(tmp))), expected)
            self.assertEqual(gzip.open(extract_stage.assemble_input(store, list(files), Path(tmp))).read(), expected)

    def test_streaming_runner_reports_rc_stderr_and_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, err = extract_stage._run_streaming(
                [sys.executable, "-c", "import sys; sys.stdin.read(); sys.stderr.write('boom'); sys.exit(3)"],
                ".", iter([b"x" * 1000]), 30, Path(tmp))
            self.assertEqual((rc, err), (3, "boom"))
            with self.assertRaises(subprocess.TimeoutExpired):
                extract_stage._run_streaming([sys.executable, "-c", "import time; time.sleep(30)"], ".",
                                             iter([b"x"]), 1, Path(tmp))

    def test_extractor_version_is_read_from_bundle(self):
        self.assertEqual(extract_stage.extractor_version(SCRIPTS.parent), xm.VERSION)
        self.assertIsNone(extract_stage.extractor_version(Path("/nonexistent")))


class ManifestVersionTests(unittest.TestCase):
    def test_extract_from_older_parser_is_redone(self):
        from agent.common import EXTRACT_FILES, Layout
        date, layout = "2026-10-30", Layout("p")
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalStore(Path(tmp) / "s")
            raw = layout.raw_log(date, "node-1", "mongodb")
            (store.root / raw).parent.mkdir(parents=True)
            (store.root / raw).write_bytes(gzip.compress((SLOW % (900, 300) + "\n").encode()))
            entry = extract_stage.group_logs(extract_stage.discover_logs(store, layout, date))[0]
            for name in EXTRACT_FILES:
                store.put_text(layout.extract(date, "node-1", "mongodb", name), "old", "text/plain")
            manifest = layout.extract(date, "node-1", "mongodb", extract_stage.MANIFEST)
            store.put_text(manifest, json.dumps({"version": 1, "source_keys": entry["source_keys"]}), "application/json")
            result = extract_stage.extract_one(store, layout, date, entry, 100, SCRIPTS.parent, True)
            self.assertEqual(result["status"], "extracted")  # pre-1.5 manifest: not reused
            self.assertEqual(json.loads(store.get_text(manifest))["parser_version"], xm.VERSION)
            again = extract_stage.extract_one(store, layout, date, entry, 100, SCRIPTS.parent, True)
            self.assertEqual(again["status"], "skipped_existing")


class Parser15Tests(unittest.TestCase):
    def test_occurrences_are_lossless_and_group_constants_hoisted(self):
        text = "\n".join([SLOW % (900, 300), SLOW % (950, 400)]) + "\n"
        data = json.loads(run_extractor(text)["extractionOccurence.json"])
        self.assertEqual(data["schema_version"], "1.5")
        group = data["slow"]["operations"][0]
        self.assertEqual((group["planCacheKey"], group["queryFramework"]), ("11AA22BB", "classic"))
        details = [d for items in group["occurrence_timestamps"].values() for e in items for d in e.values()]
        self.assertEqual([d["duration_ms"] for d in details], [300, 400])
        for d in details:
            for gone in ("namespace", "durationMillis", "has_sort_stage", "planCacheKey", "queryFramework"):
                self.assertNotIn(gone, d)
            self.assertIn("cpuNanos", d)

    def test_correlate_reads_cpu_nanos(self):
        text = "\n".join([SLOW % (900, 300), SLOW % (950, 400)]) + "\n"
        data = json.loads(run_extractor(text)["extractionOccurence.json"])
        result = resource_investigation.correlate(data, datetime(2026, 10, 30, tzinfo=timezone.utc),
                                                  datetime(2026, 10, 31, tzinfo=timezone.utc))
        self.assertEqual(result["slow_operations"][0]["cpu_nanos_total"], 2000)
        legacy = {"slow": {"operations": [{"namespace": "a.b", "occurrence_timestamps": {
            "20261030": [{"00:01:20.00": {"duration_ms": 1, "cpu_nanos": 7}}]}}]}}
        self.assertEqual(resource_investigation.correlate(legacy, datetime(2026, 10, 30, tzinfo=timezone.utc),
                         datetime(2026, 10, 31, tzinfo=timezone.utc))["slow_operations"][0]["cpu_nanos_total"], 7)


class ShapeAppendixTests(unittest.TestCase):
    def test_64_hex_log_hash_gets_options_block(self):
        h = "ab" * 32
        ext = {"slow": {"operations": [{"query_hash": h.upper(), "namespace": "shop.orders",
               "sample_query_shape": {"find": "orders", "$db": "shop", "filter": {"s": "?"}, "limit": 50}}]}}
        out = report_shape_mapping.append_reported_shapes(f"# R\n\nHash `{h}` is slow.\n", ext)
        self.assertIn(f"Log query hash `{h.upper()}`", out)
        self.assertIn("**Options:**", out)
        self.assertIn("$db: shop", out)


if __name__ == "__main__":
    unittest.main()
