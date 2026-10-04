#!/usr/bin/env python3
"""Google Cloud Storage ObjectStore + Google Secret Manager loader (skill script)."""
from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import sys
import time
from pathlib import Path

SCHEME = "gs"
CHUNK = 8 * 1024 * 1024
logger = logging.getLogger("gcp_storage")


def _log_upload_error(error, target, phase, bytes_written, started):
    """Log only cloud error fields; never log headers, tokens, or upload URLs."""
    response = getattr(error, "response", None)
    body = getattr(response, "content", b"") if response is not None else b""
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    try:
        detail = json.loads(body).get("error", {})
        message = detail.get("message", "")
        reasons = ",".join(str(item.get("reason", "")) for item in detail.get("errors", [])
                           if isinstance(item, dict))
    except (ValueError, AttributeError, TypeError):
        message, reasons = "", ""
    logger.error("GCS upload failed target=%s phase=%s bytes_written=%d "
                 "elapsed_seconds=%.1f error_type=%s http_status=%s message=%s reasons=%s",
                 target, phase, bytes_written, time.monotonic() - started,
                 type(error).__name__, getattr(response, "status_code", "?"),
                 str(message).replace("\n", " ")[:500], reasons[:200])


def parse_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://"):
        raise ValueError(f"Not a gs:// URI: {uri}")
    bucket, _, key = uri[5:].partition("/")
    return bucket, key


class GCSStore:
    scheme = SCHEME

    def __init__(self, bucket: str, client=None, project: str | None = None):
        if client is None:
            from google.cloud import storage  # lazy import
            client = storage.Client(project=project)
        self.client = client
        self.bucket_name = bucket
        self.bucket = client.bucket(bucket)

    def uri(self, key: str) -> str:
        return f"gs://{self.bucket_name}/{key}"

    def exists(self, key: str) -> bool:
        return bool(self.bucket.blob(key).exists())

    def list_keys(self, prefix: str) -> list[str]:
        return [b.name for b in self.client.list_blobs(self.bucket_name, prefix=prefix)]

    def download(self, key: str, path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.bucket.blob(key).download_to_filename(str(path))

    def _blob(self, key: str, metadata: dict | None):
        blob = self.bucket.blob(key, chunk_size=CHUNK)
        if metadata:
            blob.metadata = {str(k): str(v) for k, v in metadata.items()}
        return blob

    def upload(self, path, key: str, content_type: str, metadata: dict | None = None) -> str:
        self._blob(key, metadata).upload_from_filename(str(path), content_type=content_type)
        return self.uri(key)

    def upload_stream(self, fileobj, key: str, content_type: str, metadata: dict | None = None) -> str:
        blob = self._blob(key, metadata)
        target = self.uri(key)
        started = time.monotonic()
        bytes_written = 0
        phase = "open_writer"
        logger.info("GCS resumable upload starting target=%s chunk_size=%d", target, CHUNK)
        writer = None
        try:
            # Explicit close only after EOF: context-manager exit would try to
            # publish a partial object if an Atlas read failed.
            writer = blob.open("wb", content_type=content_type)
            phase = "read_atlas"
            while True:
                data = fileobj.read(CHUNK)
                if not data:
                    break
                phase = "write_gcs"
                writer.write(data)
                bytes_written += len(data)
                phase = "read_atlas"
            phase = "finalize_gcs"
            writer.close()
            logger.info("GCS upload complete target=%s bytes_written=%d elapsed_seconds=%.1f",
                        target, bytes_written, time.monotonic() - started)
        except BaseException as error:
            _log_upload_error(error, target, phase, bytes_written, started)
            if writer is not None:
                try:
                    writer.terminate()  # Never finalize a partial Atlas response.
                except Exception:
                    pass
            raise
        return target

    def put_text(self, key: str, text: str, content_type: str) -> str:
        self.bucket.blob(key).upload_from_string(text.encode("utf-8"), content_type=content_type)
        return self.uri(key)

    def get_text(self, key: str) -> str:
        return self.bucket.blob(key).download_as_bytes().decode("utf-8")


def secret_version_name(secret_id: str, project: str | None = None) -> str:
    if secret_id.startswith("projects/"):
        return secret_id if "/versions/" in secret_id else f"{secret_id}/versions/latest"
    project = project or os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
    if not project:
        raise ValueError("Bare secret name needs GOOGLE_CLOUD_PROJECT (or use projects/<p>/secrets/<name>)")
    return f"projects/{project}/secrets/{secret_id}/versions/latest"


def load_secret(secret_id: str, client=None, project: str | None = None) -> dict:
    if client is None:
        from google.cloud import secretmanager  # lazy import
        client = secretmanager.SecretManagerServiceClient()
    response = client.access_secret_version(name=secret_version_name(secret_id, project))
    return json.loads(response.payload.data.decode("utf-8"))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ls").add_argument("uri")
    cp = sub.add_parser("cp"); cp.add_argument("src"); cp.add_argument("dst")
    sub.add_parser("exists").add_argument("uri")
    sub.add_parser("secret-keys").add_argument("secret_id")
    args = p.parse_args(argv)
    if args.cmd == "ls":
        bucket, prefix = parse_uri(args.uri)
        print("\n".join(GCSStore(bucket).list_keys(prefix)))
    elif args.cmd == "cp":
        if args.src.startswith("gs://"):
            bucket, key = parse_uri(args.src); GCSStore(bucket).download(key, args.dst)
        else:
            bucket, key = parse_uri(args.dst)
            ctype = mimetypes.guess_type(args.src)[0] or "application/octet-stream"
            print(GCSStore(bucket).upload(args.src, key, ctype))
    elif args.cmd == "exists":
        bucket, key = parse_uri(args.uri)
        ok = GCSStore(bucket).exists(key); print(json.dumps(ok)); return 0 if ok else 1
    elif args.cmd == "secret-keys":
        print(json.dumps(sorted(load_secret(args.secret_id)), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
