"""ObjectStore routing for supplied inputs versus generated agent results.

Stages address every object with one logical key under the INPUT prefix
(``<prefix>/<day>/<node-or-cluster>/<section>/...``, see ``common.Layout``).
Only extracts and reports are results: they are stored in the results bucket
under the results prefix (``MONGODB_LOG_DIAG_RESULTS_PREFIX``, default: the
input prefix), everything else stays in the input bucket under the input prefix.
No fallback reads from the other bucket/prefix are permitted.
"""
from __future__ import annotations

RESULT_SECTIONS = frozenset({'extracts', 'reports'})


class SplitBucketStore:
    def __init__(self, inputs, outputs, prefix: str, results_prefix: str | None = None):
        self.inputs = inputs
        self.outputs = outputs
        self.prefix = prefix.strip('/')
        self.results_prefix = (self.prefix if results_prefix is None else results_prefix).strip('/')
        self.scheme = outputs.scheme

    # -- logical (input-prefix) key <-> physical results key ------------------------------
    @staticmethod
    def _base(prefix: str) -> str:
        return prefix + '/' if prefix else ''

    def _to_results(self, key: str) -> str:
        base = self._base(self.prefix)
        if self.results_prefix == self.prefix or not key.startswith(base):
            return key
        return self._base(self.results_prefix) + key[len(base):]

    def _from_results(self, key: str) -> str | None:
        base = self._base(self.results_prefix)
        if not key.startswith(base):
            return None
        return self._base(self.prefix) + key[len(base):]

    def is_result_key(self, key: str) -> bool:
        base = self._base(self.prefix)
        if not key.startswith(base):
            return False
        parts = key[len(base):].split('/')
        # date / node-or-cluster / section / ...
        return len(parts) >= 3 and parts[2] in RESULT_SECTIONS

    def _route(self, key):
        """(store, physical key) for one logical key."""
        if self.is_result_key(key):
            return self.outputs, self._to_results(key)
        return self.inputs, key

    def uri(self, key):
        # A day/prefix summary URI represents the results location.
        base = self._base(self.prefix)
        if key.startswith(base) and len(key[len(base):].rstrip('/').split('/')) <= 2:
            return self.outputs.uri(self._to_results(key))
        store, physical = self._route(key)
        return store.uri(physical)

    def exists(self, key):
        store, physical = self._route(key)
        return store.exists(physical)

    def _results_listing_prefix(self, prefix: str) -> str | None:
        """Physical results prefix covering a logical listing prefix (None: no result can match)."""
        base = self._base(self.prefix)
        if prefix.startswith(base):
            return self._to_results(prefix)
        if base.startswith(prefix):  # listing above the input prefix: every result may match
            return self._base(self.results_prefix)
        return None

    def list_keys(self, prefix):
        # Each key has exactly one authoritative bucket/prefix. Stale artifacts in the
        # other location must not affect discovery, skip checks or baselines.
        results_base = self._base(self.results_prefix)
        same_store = self.inputs is self.outputs
        supplied = (k for k in self.inputs.list_keys(prefix)
                    if not self.is_result_key(k)
                    and not (same_store and self.results_prefix != self.prefix and k.startswith(results_base)))
        generated = []
        physical = self._results_listing_prefix(prefix)
        if physical is not None:
            for k in self.outputs.list_keys(physical):
                logical = self._from_results(k)
                if logical is not None and logical.startswith(prefix) and self.is_result_key(logical):
                    generated.append(logical)
        return sorted(set(supplied) | set(generated))

    def download(self, key, path):
        store, physical = self._route(key)
        return store.download(physical, path)

    def open_read(self, key):
        store, physical = self._route(key)
        opener = getattr(store, "open_read", None)
        if not callable(opener):
            raise NotImplementedError("open_read")
        return opener(physical)

    def get_text(self, key):
        store, physical = self._route(key)
        return store.get_text(physical)

    def upload(self, path, key, content_type, metadata=None):
        store, physical = self._route(key)
        return store.upload(path, physical, content_type, metadata)

    def upload_stream(self, fileobj, key, content_type, metadata=None):
        store, physical = self._route(key)
        return store.upload_stream(fileobj, physical, content_type, metadata)

    def put_text(self, key, text, content_type):
        store, physical = self._route(key)
        return store.put_text(physical, text, content_type)
