"""Atlas stream reset never publishes partial S3 objects; retry is replayable."""
import io
import tempfile
from pathlib import Path
from unittest.mock import patch
from agent import download_stage
from agent.providers import normalize_config

CFG = normalize_config({"atlas_public_key": "p", "atlas_private_key": "s",
    "group_id": "g", "cluster_name": "c", "timezone": "UTC", "api_version": "2025-03-12",
    "bucket": "test", "prefix": "logs", "storage_provider": "s3"}, "local")

class Broken(io.BytesIO):
    def __init__(self):
        super().__init__(b"partial")
    def read(self, n=-1):
        data = super().read(n)
        if not data:
            raise ConnectionResetError("Atlas closed stream")
        return data

class Atlas:
    max_retries = 2
    def __init__(self): self.calls = 0
    def _retry_delay(self, attempt): return 0
    def get_json(self, path, query=None):
        return {"results": [{"hostname": "node-0", "typeName": "REPLICA_SECONDARY"}]}
    def download_log(self, *args):
        self.calls += 1
        return Broken() if self.calls == 1 else io.BytesIO(b"complete gzip data")

class S3:
    def __init__(self): self.uploads=[]; self.objects={}
    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            e=Exception("missing"); e.response={"Error":{"Code":"404"}}; raise e
    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        assert Path(filename).exists()
        self.uploads.append(key)
        self.objects[key] = Path(filename).read_bytes()

from importlib.util import spec_from_file_location, module_from_spec
source = Path(__file__).resolve().parents[1] / "skills/aws-storage/scripts/aws_storage.py"
spec = spec_from_file_location("s3_stream_test", source)
mod = module_from_spec(spec); spec.loader.exec_module(mod)
s3=S3(); atlas=Atlas(); store=mod.S3Store("test", client=s3)
with patch("skill_mongodb_atlas_logs_atlas_logs.time.sleep", return_value=None):
    result=download_stage.run(CFG, "2026-09-29", store=store, client=atlas)
assert atlas.calls == 2
assert len(s3.uploads) == 1, "partial Atlas body reached S3"
assert list(s3.objects.values()) == [b"complete gzip data"]
assert result["logs"][0]["status"] == "downloaded"
print("S3 stream staging and Atlas stream reset retry passed")
