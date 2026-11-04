"""Keep driver CVE information only inside the driver-compatibility section of a node report.

Models sometimes add a standalone "Driver CVE Status" block near the top of the report (e.g. "No match in
the offline CVE snapshot refreshed <date> ... See Section 10"). That duplicates the driver section. This
deterministic pass removes such a block. If the block carries CVE/GHSA IDs the driver section does not
already mention, it is moved (not lost) to the end of the driver section; when there is no driver section
and the block carries such IDs, the report is returned unchanged.
"""
from __future__ import annotations

import re

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
# A bold-only line ("**Driver CVE Status**", "> **⚠️ DRIVER CVE WARNINGS — ...**") acts as a pseudo-heading.
_BOLD_LINE = re.compile(r"^>?\s*\*\*(.+?)\*\*\s*:?\s*$")
_CLEAN = re.compile(r"^[\s>*_`#]*(?:\d+(?:\.\d+)*[.)]?\s*)?(?:[^\w\s]+\s*)*", re.UNICODE)
_CVE_STATUS_TITLE = re.compile(
    r"^(?:driver\s+)?cves?(?:\s*/\s*advisor(?:y|ies))?(?:\s+(?:status|warnings?|summary|check|review|"
    r"findings?|lookup|snapshot))\b", re.I)
_DRIVER_TITLE = re.compile(r"\bdrivers?\b", re.I)
_IDS = re.compile(r"\b(?:CVE-\d{4}-\d{4,}|GHSA(?:-[0-9a-z]{4}){3})\b", re.I)
_FENCE = re.compile(r"^\s*(```|~~~)")
_PSEUDO_LEVEL = 7  # weaker than any Markdown heading


def _title(text: str) -> str:
    return _CLEAN.sub("", text).strip(" *_`:—-")


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line index, level, cleaned title) for real and pseudo headings outside code fences."""
    out, fenced = [], False
    for i, line in enumerate(lines):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        m = _HEADING.match(line)
        if m:
            out.append((i, len(m.group(1)), _title(m.group(2))))
            continue
        b = _BOLD_LINE.match(line)
        if b:  # every bold-only line bounds a pseudo section; only CVE-titled ones are candidates
            out.append((i, _PSEUDO_LEVEL, _title(b.group(1))))
    return out


def _end(heads, pos: int, n: int) -> int:
    start, level, _ = heads[pos]
    for j in range(pos + 1, len(heads)):
        if heads[j][1] <= level or (level == _PSEUDO_LEVEL and heads[j][1] == _PSEUDO_LEVEL):
            return heads[j][0]
    return n


def relocate_standalone_cve_status(markdown: str) -> tuple[str, int]:
    """Return (markdown, number of standalone CVE blocks removed or moved)."""
    lines = markdown.split("\n")
    heads = _headings(lines)
    spans = [(h[0], _end(heads, k, len(lines)), h) for k, h in enumerate(heads)]
    driver = next((s for s in spans if s[2][1] < _PSEUDO_LEVEL and _DRIVER_TITLE.search(s[2][2])
                   and not _CVE_STATUS_TITLE.match(s[2][2])), None)
    d_start, d_end = (driver[0], driver[1]) if driver else (len(lines), len(lines))
    stray = [s for s in spans if _CVE_STATUS_TITLE.match(s[2][2]) and not (d_start <= s[0] < d_end)]
    # Drop nested candidates (a pseudo heading inside an already-selected block).
    selected: list[tuple[int, int]] = []
    for s in sorted(stray):
        if not any(a <= s[0] < b for a, b in selected):
            selected.append((s[0], s[1]))
    if not selected:
        return markdown, 0
    driver_text = "\n".join(lines[d_start:d_end])
    known = {i.upper() for i in _IDS.findall(driver_text)}
    moved: list[str] = []
    for a, b in selected:
        body = [ln for ln in lines[a + 1:b]]
        if {i.upper() for i in _IDS.findall("\n".join(body))} - known:
            if driver is None:  # nowhere to move real CVE content: keep the report as is
                return markdown, 0
            moved.append("\n".join(body).strip())
    drop = set()
    for a, b in selected:
        drop.update(range(a, b))
    out: list[str] = []
    for i, line in enumerate(lines):
        if i == d_end and moved:
            out.extend(_moved_block(moved))
        if i not in drop:
            out.append(line)
    if d_end >= len(lines) and moved:
        out.extend(_moved_block(moved))
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out))
    return text, len(selected)


def _moved_block(blocks: list[str]) -> list[str]:
    return ["", "**CVE review (moved from report header)**", "", *"\n\n".join(blocks).split("\n"), ""]
