"""Offline tests for the multi-cloud agent + skills (no network, no cloud SDKs needed).

Runs the full pipeline (download -> extract -> report with n-1/n-8 diff) three times against
fake S3 (aws-storage skill), fake GCS (gcp-storage skill) and the real LocalStore, plus unit
tests for secrets, config normalisation, cloud detection, Bedrock/Vertex clients and sharding.
Run: python3 tests/test_multicloud.py
"""
import gzip, io, json, os, sys, tempfile, textwrap, types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.pop("SKILLS_DIR", None)
for k in ("CLOUD_PROVIDER", "ATLAS_SECRET_ID", "ATLAS_CONFIG_FILE", "AWS_LAMBDA_FUNCTION_NAME", "CLOUD_RUN_JOB", "K_SERVICE"):
    os.environ.pop(k, None)

# ---- stub diagnostic skill (same output contract as scripts/extract_mongodb_log.py) --------------
diag = Path(tempfile.mkdtemp()) / "skill-mongodb-log-diagnostic"
(diag / "scripts").mkdir(parents=True); (diag / "references").mkdir()
(diag / "SKILL.md").write_text("---\ndescription: x\n---\n")
for f in ("ftdc_decoder.py", "driver_compatibility.py"):
    (diag / "scripts" / f).write_text("#!/usr/bin/env python3\n")
(diag / "references/analysis-prompt.md").write_text("ANALYSIS_PROMPT_MARKER")
(diag / "references/extracted-signal-reference.md").write_text("SIGNAL_REF_MARKER")
(diag / "references/analysis-prompt.offline-cve-overlay.md").write_text("OFFLINE_CVE_MARKER")
(diag / "references/offline-driver-cves.json").write_text(json.dumps({"$schema": "mongodb-log-diagnostic.offline-driver-cves/v1", "catalog_version": "test", "refreshed_at": "2026-09-29", "entries": []}))
(diag / "scripts/extract_mongodb_log.py").write_text(textwrap.dedent('''\
    #!/usr/bin/env python3
    import argparse, gzip, json
    from pathlib import Path
    p = argparse.ArgumentParser(); p.add_argument("inputs", nargs="+", type=Path)
    p.add_argument("--output", required=True, type=Path); p.add_argument("--slow-ms", type=float)
    a = p.parse_args(); a.output.mkdir(parents=True, exist_ok=True)
    n = len(gzip.open(a.inputs[0], "rt").read().splitlines())
    ops = [{"namespace": "app.orders", "plan_summary": "COLLSCAN", "query_hash": "ABC", "count": n, "duration_ms": {"median": 100 * n}}]
    payload = {"schema_version": "1.3", "metadata": {"parser_version": "1.3", "slow_threshold_ms": a.slow_ms,
      "time_range": {"first": "2026-09-22T00:00:00Z", "last": "2026-09-22T23:59:00Z"}, "input_files": [a.inputs[0].name]},
      "driverCompatibility": {"count": 5, "incompatible_count": 0, "distinct_compatible_drivers": {"nodejs": [{"version": "6.%d.0" % n}]}},
      "quality": {"lines_seen": n, "parsed_records": n, "skipped_records": 0, "skipped_ratio": 0.0, "notes": []},
      "summary": {"severity_counts": {"I": n}, "category_counts": {"slow_operation": n}},
      "error_scan": {"total_error_events": n, "unique_error_groups": 1, "repeated_error_groups": 1,
        "groups": [{"fingerprint": "auth failed", "count": n, "occurrence_timestamps": {"20260922": [{"10H06": {"occur": n}}]}}]},
      "slow": {"operations": ops, "global_stats": {"count": n, "collscan_count": n, "change_stream_count": 0,
        "duration_ms": {"median": 100.0 * n, "max": 200.0 * n}, "cpuNanos": {"total": 1000 * n}}},
      "slow_operations": ops, "issue_candidates": [], "trends": []}
    (a.output / "extractionOccurence.json").write_text(json.dumps(payload))
    for g in payload["error_scan"]["groups"]: g.pop("occurrence_timestamps")
    (a.output / "extractionshort.json").write_text(json.dumps(payload))
    (a.output / "handoff.md").write_text("# MongoDB Log Extraction Handoff\\n")
    '''))
os.environ["DIAG_SKILL_DIR"] = str(diag)
os.environ["WORK_DIR"] = tempfile.mkdtemp()

from agent import handler, providers, skills, llm as llm_mod  # noqa: E402
from agent.common import Layout  # noqa: E402

atlas = skills.atlas_logs(); aws = skills.aws_storage(); gcp = skills.gcp_storage()
TZ = "Asia/Jerusalem"
DAY_LINES = {"2026-09-15": 2, "2026-09-21": 4, "2026-09-22": 6, "2026-10-05": 3}
START_TO_DAY = {atlas.day_window(d, TZ)[0]: d for d in DAY_LINES}

# ---- fakes ---------------------------------------------------------------------------------------
class FakeAtlas:
    calls = []
    def __init__(self, config): pass
    def get_json(self, path, query=None):
        return {"results": [{"hostname": f"my-cluster-shard-00-0{i}.abcd.mongodb.net", "typeName": "REPLICA_SECONDARY"} for i in range(3)]
                + [{"hostname": "other-00-00.x.mongodb.net", "typeName": "REPLICA_PRIMARY"}, {"hostname": "my-cluster-x", "typeName": "NO_DATA"}]}
    def download_log(self, group, host, log, start, end):
        FakeAtlas.calls.append((host, start)); assert end - start in (82800, 86400, 90000)
        return io.BytesIO(gzip.compress(("\n".join(["{}"] * DAY_LINES[START_TO_DAY[start]])).encode()))
atlas.AtlasClient = FakeAtlas

class NotFound(Exception):
    response = {"Error": {"Code": "404"}}

class FakeS3:
    def __init__(self): self.objects = {}; self.meta = {}
    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects: raise NotFound()
    def get_paginator(self, _):
        s3 = self
        class P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for (b, k) in sorted(s3.objects) if b == Bucket and k.startswith(Prefix)]}
        return P()
    def download_file(self, b, k, path): Path(path).write_bytes(self.objects[(b, k)])
    def upload_file(self, path, b, k, ExtraArgs=None): self.objects[(b, k)] = Path(path).read_bytes(); self.meta[(b, k)] = ExtraArgs
    def upload_fileobj(self, fh, b, k, ExtraArgs=None): self.objects[(b, k)] = fh.read(); self.meta[(b, k)] = ExtraArgs
    def put_object(self, Bucket, Key, Body, ContentType=None): self.objects[(Bucket, Key)] = Body
    def get_object(self, Bucket, Key): return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}
    def keys(self): return sorted(k for _, k in self.objects)

class FakeGCSClient:
    def __init__(self): self.objects = {}; self.meta = {}
    def bucket(self, name): return FakeBucket(self, name)
    def list_blobs(self, bucket, prefix=""):
        return [types.SimpleNamespace(name=k) for (b, k) in sorted(self.objects) if b == bucket and k.startswith(prefix)]
    def keys(self): return sorted(k for _, k in self.objects)

class FakeBucket:
    def __init__(self, client, name): self.client, self.name = client, name
    def blob(self, key, chunk_size=None): return FakeBlob(self.client, self.name, key)

class FakeBlob:
    def __init__(self, client, bucket, key): self.c, self.id, self.metadata = client, (bucket, key), None
    def exists(self): return self.id in self.c.objects
    def download_to_filename(self, path): Path(path).write_bytes(self.c.objects[self.id])
    def upload_from_filename(self, path, content_type=None): self._store(Path(path).read_bytes(), content_type)
    def upload_from_string(self, data, content_type=None): self._store(data, content_type)
    def download_as_bytes(self): return self.c.objects[self.id]
    def _store(self, data, ctype): self.c.objects[self.id] = data; self.c.meta[self.id] = (ctype, self.metadata)
    def open(self, mode, content_type=None):
        blob, buf = self, io.BytesIO()
        class W:
            def write(self, b): return buf.write(b)
            def __enter__(self): return self
            def __exit__(self, *a): blob._store(buf.getvalue(), content_type)
        return W()

class FakeLLM:
    def __init__(self): self.calls = []
    def generate(self, system, user):
        self.calls.append((system, user)); return f"# MongoDB Log Diagnostic — Issues and Resolution\n(report {len(self.calls)})\n"

BASE = {"atlas_public_key": "pub", "atlas_private_key": "SECRET", "group_id": "g" * 24, "cluster_name": "my-cluster",
        "timezone": TZ, "api_version": "2025-03-12", "host_selector": "^my-cluster-"}

def run_all_days(cfg, store):
    providers_get_store, providers_get_llm = providers.get_store, providers.get_llm
    fake_llm = FakeLLM()
    providers.get_store = lambda c: store; providers.get_llm = lambda c: fake_llm
    try:
        for day in ("2026-09-15", "2026-09-21", "2026-09-22"):
            out = handler.run_pipeline({"stage": "all", "log_date": day}, config=cfg)
        return out, fake_llm
    finally:
        providers.get_store, providers.get_llm = providers_get_store, providers_get_llm

def check_outputs(keys, prefix, get_text, fake_llm, out):
    D1 = "2026-09-22"
    raw = [k for k in keys if k.startswith(f"{prefix}/{D1}/") and k.endswith("/mongodb/mongodb.gz")]
    assert len(raw) == 3 and all("my-cluster-shard-00-0" in k for k in raw), raw
    for f in ("manifest.json", "cluster-summary.md"):
        assert f"{prefix}/{D1}/cluster/reports/{f}" in keys
    node = "my-cluster-shard-00-00.abcd.mongodb.net"
    assert f"{prefix}/{D1}/{node}/extracts/mongodb/extractionOccurence.json" in keys
    diff = json.loads(get_text(f"{prefix}/{D1}/{node}/reports/mongodb/diff.json"))
    c = {x["label"]: x for x in diff["comparisons"]}
    assert c["n-1"]["baseline_date"] == "2026-09-21" and c["n-8"]["baseline_date"] == "2026-09-15"
    assert c["n-1"]["signals"]["slow.global_stats.count"] == {"current": 6, "baseline": 4, "delta": 2, "pct": 50.0, "label": "increased"}
    assert c["n-8"]["signals"]["error_scan.total_error_events"]["baseline"] == 2
    system, user = fake_llm.calls[-4]  # first node report of D-1 (3 node reports + cluster summary)
    assert "ANALYSIS_PROMPT_MARKER" in system and "SIGNAL_REF_MARKER" in system
    assert 'label="n-1" date="2026-09-21"' in user and 'label="n-8" date="2026-09-15"' in user and "precomputed_diff" in user
    assert "SECRET" not in user and "SECRET" not in system
    manifest = json.loads(get_text(f"{prefix}/{D1}/cluster/reports/manifest.json"))
    assert manifest["node_count"] == 3 and not manifest["failed"]
    assert out["report"]["node_count"] == 3
    return manifest

# ---- 1. AWS: aws-storage skill on fake S3 --------------------------------------------------------
cfg_aws = providers.normalize_config({**BASE, "s3_bucket": "legacy-bkt", "s3_prefix": "atlas-logs", "bedrock_model_id": "m"}, "aws")
assert (cfg_aws["bucket"], cfg_aws["prefix"], cfg_aws["storage_provider"], cfg_aws["llm_provider"]) == ("legacy-bkt", "atlas-logs", "s3", "bedrock")
s3 = FakeS3(); out, fl = run_all_days(cfg_aws, aws.S3Store("legacy-bkt", client=s3))
m = check_outputs(s3.keys(), "atlas-logs", lambda k: s3.objects[("legacy-bkt", k)].decode(), fl, out)
assert m["storage"] == "s3://legacy-bkt/atlas-logs/2026-09-22" and m["cloud"] == "aws"
assert s3.meta[("legacy-bkt", "atlas-logs/2026-09-22/my-cluster-shard-00-00.abcd.mongodb.net/mongodb/mongodb.gz")]["Metadata"]["cluster"] == "my-cluster"
n_calls = len(FakeAtlas.calls)
providers.get_store = lambda c: aws.S3Store("legacy-bkt", client=s3)
handler.run_pipeline({"stage": "download", "log_date": "2026-09-22"}, config=cfg_aws)   # skip_existing
assert len(FakeAtlas.calls) == n_calls, "re-download should be skipped"
print("AWS pipeline OK")

# ---- 2. GCP: gcp-storage skill on fake GCS -------------------------------------------------------
cfg_gcp = providers.normalize_config({**BASE, "bucket": "gs://my-gcs-bkt", "prefix": "atlas-logs/", "vertex_model": "gemini-x"}, "gcp")
assert (cfg_gcp["bucket"], cfg_gcp["prefix"], cfg_gcp["storage_provider"], cfg_gcp["llm_provider"]) == ("my-gcs-bkt", "atlas-logs", "gcs", "vertex")
gcs = FakeGCSClient(); out, fl = run_all_days(cfg_gcp, gcp.GCSStore("my-gcs-bkt", client=gcs))
m = check_outputs(gcs.keys(), "atlas-logs", lambda k: gcs.objects[("my-gcs-bkt", k)].decode(), fl, out)
assert m["storage"] == "gs://my-gcs-bkt/atlas-logs/2026-09-22" and m["cloud"] == "gcp"
ctype, meta = gcs.meta[("my-gcs-bkt", "atlas-logs/2026-09-22/my-cluster-shard-00-01.abcd.mongodb.net/mongodb/mongodb.gz")]
assert ctype == "application/gzip" and meta["log-name"] == "mongodb"
print("GCP pipeline OK")

# ---- 3. local filesystem store --------------------------------------------------------------------
root = Path(tempfile.mkdtemp())
cfg_loc = providers.normalize_config({**BASE, "bucket": f"file://{root}", "prefix": "p"}, "local")
store = providers.LocalStore(root); out, fl = run_all_days(cfg_loc, store)
check_outputs(store.list_keys(""), "p", store.get_text, fl, out)
print("Local pipeline OK")

# ---- 4. cloud detection + secrets ----------------------------------------------------------------
os.environ["ATLAS_SECRET_ID"] = "arn:aws:secretsmanager:us-east-1:1:secret:x"; assert providers.detect_cloud() == "aws"
os.environ["ATLAS_SECRET_ID"] = "projects/p/secrets/x"; assert providers.detect_cloud() == "gcp"
os.environ["CLOUD_RUN_JOB"] = "j"; os.environ["ATLAS_SECRET_ID"] = "arn:aws:x"; assert providers.detect_cloud() == "gcp"
del os.environ["CLOUD_RUN_JOB"]; os.environ["CLOUD_PROVIDER"] = "aws"; assert providers.detect_cloud() == "aws"
del os.environ["CLOUD_PROVIDER"]; del os.environ["ATLAS_SECRET_ID"]
assert gcp.secret_version_name("projects/p/secrets/s") == "projects/p/secrets/s/versions/latest"
assert gcp.secret_version_name("projects/p/secrets/s/versions/3") == "projects/p/secrets/s/versions/3"
assert gcp.secret_version_name("s", project="proj") == "projects/proj/secrets/s/versions/latest"
class SM:
    def access_secret_version(self, name): self.name = name; return types.SimpleNamespace(payload=types.SimpleNamespace(data=json.dumps(BASE).encode()))
sm = SM(); assert gcp.load_secret("projects/p/secrets/s", client=sm) == BASE and sm.name.endswith("/versions/latest")
import base64
class ASM:
    def __init__(self, v): self.v = v
    def get_secret_value(self, SecretId): return self.v
assert aws.load_secret("x", client=ASM({"SecretString": json.dumps(BASE)})) == BASE
assert aws.load_secret("x", client=ASM({"SecretBinary": base64.b64encode(json.dumps(BASE).encode()).decode()})) == BASE
try:
    providers.normalize_config({**BASE}, "aws"); raise AssertionError("missing bucket must fail")
except ValueError: pass
try:
    providers.normalize_config({"bucket": "b"}, "aws"); raise AssertionError("missing atlas keys must fail")
except ValueError: pass
print("Secrets / config / detection OK")

# ---- 5. LLM clients ------------------------------------------------------------------------------
class Resp:
    def __init__(self, data): self.data, self.status_code, self.text = data, 200, ""
    def json(self): return self.data
class Session:
    def __init__(self, replies): self.replies, self.calls = list(replies), []
    def post(self, url, json=None, timeout=None): self.calls.append((url, json)); return Resp(self.replies.pop(0))
s = Session([{"candidates": [{"content": {"parts": [{"text": "A"}]}, "finishReason": "MAX_TOKENS"}]},
             {"candidates": [{"content": {"parts": [{"text": "B"}]}, "finishReason": "STOP"}]}])
v = llm_mod.VertexLLM({"vertex_model": "gemini-2.5-pro", "vertex_project": "proj", "vertex_location": "europe-west1"}, session=s)
assert v.generate("SYS", "USER") == "AB"
assert s.calls[0][0] == "https://europe-west1-aiplatform.googleapis.com/v1/projects/proj/locations/europe-west1/publishers/google/models/gemini-2.5-pro:generateContent"
assert s.calls[0][1]["systemInstruction"]["parts"][0]["text"] == "SYS" and len(s.calls[1][1]["contents"]) == 3
s = Session([{"content": [{"type": "text", "text": "C"}], "stop_reason": "end_turn"}])
v = llm_mod.VertexLLM({"vertex_model": "claude-sonnet-4@20250514", "vertex_project": "proj", "vertex_location": "global"}, session=s)
assert v.generate("SYS", "USER") == "C"
assert s.calls[0][0].startswith("https://aiplatform.googleapis.com/v1/projects/proj/locations/global/publishers/anthropic/models/claude-sonnet-4@20250514:rawPredict")
assert s.calls[0][1]["anthropic_version"] == "vertex-2023-10-16" and s.calls[0][1]["system"] == "SYS"
class BR:
    def __init__(self): self.n = 0
    def converse(self, **kw):
        self.n += 1
        return {"output": {"message": {"content": [{"text": f"P{self.n}"}]}}, "stopReason": "max_tokens" if self.n == 1 else "end_turn"}
assert llm_mod.BedrockLLM({"bedrock_model_id": "m"}, client=BR()).generate("S", "U") == "P1P2"
print("LLM clients OK")

# ---- 6. Atlas skill standalone (local dir sink + CLI) + sharding ---------------------------------
out_dir = Path(tempfile.mkdtemp())
res = atlas.archive_logs(BASE, "2026-09-22", sink=atlas.local_dir_sink(out_dir))
assert len(res["logs"]) == 3 and (out_dir / "2026-09-22/my-cluster-shard-00-02.abcd.mongodb.net/mongodb/mongodb.gz").is_file()
cfg_file = out_dir / "cfg.json"; cfg_file.write_text(json.dumps(BASE))
before = len(FakeAtlas.calls)
assert atlas.main(["--config", str(cfg_file), "download", "--output-dir", str(out_dir), "--log-date", "2026-09-22"]) == 0
assert len(FakeAtlas.calls) == before, "CLI must skip existing files"
from agent import extract_stage
assert extract_stage._shard([0, 1, 2, 3], 1, 3) == [1] and extract_stage._shard([0, 1, 2], 0, 3) == [0]
os.environ["CLOUD_RUN_TASK_INDEX"], os.environ["CLOUD_RUN_TASK_COUNT"] = "2", "3"
assert extract_stage._shard(["a", "b", "c"], None, None) == ["c"]
print("Atlas skill + sharding OK")
print("ALL TESTS PASSED")
