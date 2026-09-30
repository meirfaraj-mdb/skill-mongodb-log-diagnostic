#!/usr/bin/env python3
"""Amazon S3 ObjectStore + AWS Secrets Manager loader (skill script)."""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import sys
import shutil
import tempfile
from pathlib import Path

SCHEME = "s3"


def _boto3():
    import boto3  # lazy: importing this module must not require boto3
    return boto3


def parse_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("s3://"):
        raise ValueError(f"Not an s3:// URI: {uri}")
    bucket, _, key = uri[5:].partition("/")
    return bucket, key


class S3Store:
    scheme = SCHEME

    def __init__(self, bucket: str, client=None, region: str | None = None):
        self.bucket = bucket
        self.s3 = client or _boto3().client("s3", region_name=region)

    def uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"

    def exists(self, key: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception as error:
            code = str(getattr(error, "response", {}).get("Error", {}).get("Code", ""))
            if code in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
        return keys

    def download(self, key: str, path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.s3.download_file(self.bucket, key, str(path))

    @staticmethod
    def _extra(content_type: str, metadata: dict | None) -> dict:
        extra = {"ContentType": content_type}
        if metadata:
            extra["Metadata"] = {str(k): str(v) for k, v in metadata.items()}
        return extra

    def upload(self, path, key: str, content_type: str, metadata: dict | None = None) -> str:
        self.s3.upload_file(str(path), self.bucket, key, ExtraArgs=self._extra(content_type, metadata))
        return self.uri(key)

    def upload_stream(self, fileobj, key: str, content_type: str, metadata: dict | None = None) -> str:
        # boto3's multipart upload may read/retry the source out of order. Atlas
        # HTTP responses cannot be replayed or seeked; stage the complete body
        # before starting S3 transfer. upload_file then retries from a local file.
        with tempfile.TemporaryDirectory(prefix="atlas-s3-upload-") as tmp:
            staged = Path(tmp) / "log.gz"
            with staged.open("wb") as dest:
                shutil.copyfileobj(fileobj, dest, 8 * 1024 * 1024)
            self.upload(staged, key, content_type, metadata)
        return self.uri(key)

    def put_text(self, key: str, text: str, content_type: str) -> str:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=text.encode("utf-8"), ContentType=content_type)
        return self.uri(key)

    def get_text(self, key: str) -> str:
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read().decode("utf-8")


def load_secret(secret_id: str, client=None, region: str | None = None) -> dict:
    client = client or _boto3().client("secretsmanager", region_name=region)
    value = client.get_secret_value(SecretId=secret_id)
    if value.get("SecretString"):
        raw = value["SecretString"]
    else:
        blob = value["SecretBinary"]
        if isinstance(blob, str):
            blob = base64.b64decode(blob)
        raw = blob.decode("utf-8")
    return json.loads(raw)


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
        print("\n".join(S3Store(bucket).list_keys(prefix)))
    elif args.cmd == "cp":
        if args.src.startswith("s3://"):
            bucket, key = parse_uri(args.src); S3Store(bucket).download(key, args.dst)
        else:
            bucket, key = parse_uri(args.dst)
            ctype = mimetypes.guess_type(args.src)[0] or "application/octet-stream"
            print(S3Store(bucket).upload(args.src, key, ctype))
    elif args.cmd == "exists":
        bucket, key = parse_uri(args.uri)
        ok = S3Store(bucket).exists(key); print(json.dumps(ok)); return 0 if ok else 1
    elif args.cmd == "secret-keys":
        print(json.dumps(sorted(load_secret(args.secret_id)), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
