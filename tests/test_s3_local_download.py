"""Local configuration must route Atlas downloads to S3 and stop on partial uploads."""
import io
import tempfile
from pathlib import Path
from unittest.mock import patch

from agent import download_stage
from agent.common import Layout
from agent.providers import normalize_config, get_store

CFG = normalize_config({
    "atlas_public_key": "public", "atlas_private_key": "private",
    "group_id": "group", "cluster_name": "cluster", "timezone": "UTC",
    "api_version": "2025-03-12", "bucket": "test-bucket", "prefix": "atlas-logs",
    "storage_provider": "s3", "llm_provider": "claude_cli",
}, "local")
assert CFG["cloud"] == "local" and CFG["storage_provider"] == "s3"

class FakeS3:
    def __init__(self):
        self.objects = {}
        self.fail_key = None
    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            error = RuntimeError("not found")
            error.response = {"Error": {"Code": "404"}}
            raise error
    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        if key == self.fail_key:
            raise RuntimeError("AccessDenied: s3:PutObject")
        self.objects[(bucket, key)] = Path(filename).read_bytes()

class FakeAtlasClient:
    def __init__(self): self.calls = []
    def get_json(self, path, query=None):
        return {"results": [{"hostname": f"node-{i}", "typeName": "REPLICA_SECONDARY"} for i in range(2)]}
    def download_log(self, group, host, log, start, end):
        self.calls.append(host)
        return io.BytesIO(b"gzip content")

DATE = "2026-09-29"
s3 = FakeS3()
with patch("agent.skills.aws_storage") as aws:
    from importlib.util import spec_from_file_location, module_from_spec
    path = Path(__file__).resolve().parents[1] / "skills/aws-storage/scripts/aws_storage.py"
    spec = spec_from_file_location("test_aws_storage", path)
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    aws.return_value = mod
    with patch.object(mod, "_boto3") as sdk:
        sdk.return_value.client.return_value = s3
        store = get_store(CFG)
        client = FakeAtlasClient()
        first = download_stage.run(CFG, DATE, store=store, client=client)
        assert len(first["logs"]) == 2
        assert all(x["location"].startswith("s3://test-bucket/") for x in first["logs"])
        assert len(s3.objects) == 2
        repeat = download_stage.run(CFG, DATE, store=store, client=client)
        assert all(x["status"] == "skipped_existing" for x in repeat["logs"])
        assert client.calls == ["node-0", "node-1"]
        key = Layout.from_config(CFG).raw_log(DATE, "node-1", "mongodb")
        del s3.objects[("test-bucket", key)]
        s3.fail_key = key
        try:
            download_stage.run(CFG, DATE, store=store, client=client)
        except RuntimeError as exc:
            assert "node-1/mongodb" in str(exc) and "PutObject" in str(exc)
        else:
            raise AssertionError("A partial S3 upload must stop the pipeline")
print("Local config/S3 upload and error propagation passed")
