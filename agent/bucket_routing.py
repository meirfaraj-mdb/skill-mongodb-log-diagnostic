"""ObjectStore routing for supplied inputs versus generated agent results.

Both buckets share the existing prefix/date/node layout. Only extracts and
reports are results. No fallback reads from the other bucket are permitted.
"""
from __future__ import annotations


class SplitBucketStore:
    def __init__(self, inputs, outputs, prefix: str):
        self.inputs = inputs
        self.outputs = outputs
        self.prefix = prefix.strip('/')
        self.scheme = outputs.scheme

    def is_result_key(self, key: str) -> bool:
        base = self.prefix + '/' if self.prefix else ''
        if not key.startswith(base):
            return False
        parts = key[len(base):].split('/')
        # date / node-or-cluster / section / ...
        return len(parts) >= 3 and parts[2] in {'extracts', 'reports'}

    def _store(self, key):
        return self.outputs if self.is_result_key(key) else self.inputs

    def uri(self, key):
        # A day/prefix summary URI represents the results location.
        base = self.prefix + '/' if self.prefix else ''
        if key.startswith(base) and len(key[len(base):].rstrip('/').split('/')) <= 2:
            return self.outputs.uri(key)
        return self._store(key).uri(key)

    def exists(self, key):
        return self._store(key).exists(key)

    def list_keys(self, prefix):
        # Each key has exactly one authoritative bucket. Stale artifacts in the
        # other bucket must not affect discovery, skip checks or baselines.
        supplied = (k for k in self.inputs.list_keys(prefix) if not self.is_result_key(k))
        generated = (k for k in self.outputs.list_keys(prefix) if self.is_result_key(k))
        return sorted(set(supplied) | set(generated))

    def download(self, key, path):
        return self._store(key).download(key, path)

    def get_text(self, key):
        return self._store(key).get_text(key)

    def upload(self, path, key, content_type, metadata=None):
        return self._store(key).upload(path, key, content_type, metadata)

    def upload_stream(self, fileobj, key, content_type, metadata=None):
        return self._store(key).upload_stream(fileobj, key, content_type, metadata)

    def put_text(self, key, text, content_type):
        return self._store(key).put_text(key, text, content_type)
