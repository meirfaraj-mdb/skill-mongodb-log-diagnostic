"""Guaranteed-bounded last-resort reduction of a node report prompt.

``report_stage.report_node`` builds ``user`` as ``header + "\\n\\n" + "\\n\\n".join(documents)`` where every
document is ``<document attrs>\\n<compact JSON>\\n</document>``. Its reduction ladder caps only some arrays
(``error_scan.groups``, ``slow.operations``) and can still exceed ``REPORT_MAX_INPUT_CHARS`` because:

* ``trim_occurrences(..., 0)`` keeps EVERY occurrence inside high-resource episode windows, so a node that is
  busy most of the day keeps almost all ``occurrence_timestamps``;
* the ``resource_investigation`` document has one entry per episode (up to 288 at PT5M), each with its own
  correlated slow-op/error lists, and it is never reduced;
* ``_compact`` re-adds the ``slow_operations`` alias, duplicating ``slow.operations`` in the prompt;
* other extract arrays (``repeated_errors``, driver lists, long sample messages) are not capped.

``fit_prompt`` works on the assembled string, so ``report_node``'s closures stay unchanged. It parses the
documents, shrinks the JSON bodies step by step (least important documents first) until the prompt fits,
and records what was reduced inside each document. It never truncates JSON text, so every document remains
valid JSON. The full, unreduced data stays in the store (``resource-investigation.json``, extracts).
"""
from __future__ import annotations

import json
import re

DOC_RE = re.compile(r"<document ([^>]*)>\n(.*?)\n</document>", re.S)
LABEL_RE = re.compile(r'\blabel="([^"]*)"')

# Shrunk first -> last. ``current`` (the occurrence extract of the log day) is reduced last.
SHRINK_ORDER = ("query_stats", "index_stats", "n-8", "n-1", "precomputed_diff", "offline_driver_cves",
                "resource_investigation", "current")
# Removed entirely only after every level failed, in this order. ``current`` is never dropped.
DROP_ORDER = ("index_stats", "query_stats", "n-8", "n-1", "precomputed_diff")
# (max list items, max occurrence entries per group, max string length, max correlated items)
LEVELS = ((60, 50, 600, 10), (40, 20, 400, 5), (25, 10, 300, 5), (15, 5, 200, 3), (10, 2, 160, 3),
          (5, 0, 120, 2))
_GROUP_LISTS = {"groups", "operations", "slow_operations", "repeated_errors", "error_groups",
                "slow_operation_groups", "matches"}


def document_sizes(user: str) -> list[tuple[str, int]]:
    """``[(label, chars)]`` of every document, largest first (for logging the culprit)."""
    sizes = []
    for m in DOC_RE.finditer(user):
        label = LABEL_RE.search(m.group(1))
        sizes.append((label.group(1) if label else "?", len(m.group(0))))
    return sorted(sizes, key=lambda x: -x[1])


def _sample(seq: list, keep: int) -> list:
    """Evenly spaced ``keep`` items, always including the first and last."""
    if keep <= 0:
        return []
    if len(seq) <= keep:
        return list(seq)
    if keep == 1:
        return [seq[0]]
    step = (len(seq) - 1) / (keep - 1)
    return [seq[round(i * step)] for i in range(keep)]


def _shrink_occurrences(occ, keep: int, stats: dict):
    if not isinstance(occ, dict):
        return occ
    flat = [(day, entry) for day, entries in occ.items() if isinstance(entries, list) for entry in entries]
    if len(flat) <= keep:
        return occ
    kept = _sample(flat, keep)
    out: dict = {}
    for day, entry in kept:
        out.setdefault(day, []).append(entry)
    stats["occurrence_entries_removed"] = stats.get("occurrence_entries_removed", 0) + len(flat) - len(kept)
    return out


def _is_episode_list(items) -> bool:
    return bool(items) and all(isinstance(x, dict) and isinstance(x.get("peak"), (int, float))
                               and "start_utc" in x for x in items)


def _shrink(value, level, stats, key=None, depth=0):
    max_list, max_occ, max_str, max_corr = level
    if depth > 40:
        return value
    if isinstance(value, str):
        if len(value) > max_str:
            stats["strings_shortened"] = stats.get("strings_shortened", 0) + 1
            return value[:max_str] + "…[shortened]"
        return value
    if isinstance(value, list):
        items = value
        limit = max_corr if key in {"slow_operations", "errors"} and depth > 2 else max_list
        if _is_episode_list(items) and len(items) > limit:
            # Keep the most severe episodes, then restore chronological order.
            top = sorted(items, key=lambda x: -x["peak"])[:limit]
            items = sorted(top, key=lambda x: str(x.get("start_utc")))
            stats.setdefault("lists_capped", {})[key or "?"] = [len(value), len(items)]
        elif len(items) > limit:
            items = items[:limit]
            stats.setdefault("lists_capped", {})[key or "?"] = [len(value), len(items)]
        return [_shrink(v, level, stats, key, depth + 1) for v in items]
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k == "occurrence_timestamps":
                out[k] = _shrink_occurrences(v, max_occ, stats)
            elif k == "slow_operations" and isinstance(value.get("slow"), dict) and \
                    isinstance(value["slow"].get("operations"), list):
                stats["slow_operations_alias_removed"] = True  # duplicate of slow.operations
            elif k == "trends" and depth == 0:
                stats["trends_removed"] = True
            else:
                out[k] = _shrink(v, level, stats, k, depth + 1)
        return out
    return value


def _merge(old: dict, new: dict) -> dict:
    out = dict(old)
    for k, v in new.items():
        if isinstance(v, int) and not isinstance(v, bool):
            out[k] = out.get(k, 0) + v
        elif isinstance(v, dict):
            caps = dict(out.get(k, {}))
            for name, (before, after) in v.items():
                caps[name] = [caps.get(name, [before])[0], after]
            out[k] = caps
        else:
            out[k] = v
    return out


class _Doc:
    def __init__(self, attrs: str, body: str):
        self.attrs, self.body, self.original_chars = attrs, body, len(body)
        label = LABEL_RE.search(attrs)
        self.label = label.group(1) if label else "?"
        try:
            self.data = json.loads(body)
        except ValueError:
            self.data = None  # non-JSON document: only droppable
        self.level = None
        self.dropped = False

    def shrink(self, level_index: int) -> None:
        if self.data is None or self.dropped:
            return
        stats: dict = {}
        # Levels are monotonic (each is stricter), so reduce the previous result: fast on huge inputs.
        reduced = _shrink(self.data, LEVELS[level_index], stats)
        self.data = reduced
        self.stats = _merge(getattr(self, "stats", {}), stats)
        note = {"level": level_index + 1, "original_chars": self.original_chars, **self.stats,
                "note": "Pipeline budget reduction: lists/occurrences sampled to fit the model context; "
                        "counts and aggregates are unchanged; absent items are not evidence of absence."}
        rendered = dict(reduced, _pipeline_budget=note) if isinstance(reduced, dict) else reduced
        self.body = json.dumps(rendered, separators=(",", ":"), default=str)
        self.level = level_index + 1

    def size(self) -> int:
        return len(self.render()) if not self.dropped else 0

    def render(self) -> str:
        if self.dropped:
            return ""
        attrs = self.attrs + (f' reduced="budget level {self.level}"' if self.level else "")
        return f"<document {attrs}>\n{self.body}\n</document>"


def fit_prompt(user: str, max_chars: int) -> tuple[str, dict]:
    """Return ``(prompt, receipt)`` with ``len(prompt) <= max_chars``.

    Raises ``RuntimeError`` (with a per-document size breakdown) only when even the header plus the
    maximally reduced ``current`` document cannot fit.
    """
    receipt = {"before_chars": len(user), "max_chars": max_chars, "sizes_before": document_sizes(user)}
    if len(user) <= max_chars:
        receipt["after_chars"] = len(user)
        return user, receipt
    first = DOC_RE.search(user)
    if not first:
        raise RuntimeError(f"Report input {len(user)} chars exceeds {max_chars} and has no documents")
    header = user[:first.start()].rstrip("\n")
    docs = [_Doc(m.group(1), m.group(2)) for m in DOC_RE.finditer(user)]

    def render() -> str:
        return header + "\n\n" + "\n\n".join(d.render() for d in docs if not d.dropped)

    sizes = {id(d): d.size() for d in docs}

    def total() -> int:
        live = [d for d in docs if not d.dropped]
        return len(header) + 2 + sum(sizes[id(d)] for d in live) + 2 * max(len(live) - 1, 0)

    def order(labels):
        rank = {name: i for i, name in enumerate(labels)}
        return sorted(docs, key=lambda d: rank.get(d.label, -1))  # unknown labels first

    for level_index in range(len(LEVELS)):
        for doc in order(SHRINK_ORDER):
            doc.shrink(level_index)
            sizes[id(doc)] = doc.size()
            if total() <= max_chars:
                break
        if total() <= max_chars:
            break
    if total() > max_chars:
        for label in DROP_ORDER:
            for doc in docs:
                if doc.label == label and not doc.dropped:
                    doc.dropped = True
                    receipt.setdefault("dropped", []).append(label)
            if total() <= max_chars:
                break
    prompt = render()
    receipt.update(after_chars=len(prompt), sizes_after=document_sizes(prompt),
                   levels={d.label: d.level for d in docs if d.level})
    if len(prompt) > max_chars:
        raise RuntimeError(f"Report input {len(prompt)} chars exceeds REPORT_MAX_INPUT_CHARS={max_chars} after "
                           f"budget reduction; largest documents: {receipt['sizes_after'][:4]}")
    return prompt, receipt
