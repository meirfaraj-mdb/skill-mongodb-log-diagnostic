"""Keep driver CVE information only inside the driver-compatibility section of a node report.

Models sometimes add a standalone "Driver CVE Status" block near the top of the report (e.g. "No match in
the offline CVE snapshot refreshed <date> ... See Section 10"). That duplicates the driver section. This
deterministic pass removes such a block. If the block carries CVE/GHSA IDs the driver section does not
already mention, it is moved (not lost) to the end of the driver section; when there is no driver section
and the block carries such IDs, the report is returned unchanged.

The "MongoDB server version and CVE review" section (offline server CVE snapshot, placed just before the
driver section) is a legitimate CVE section: it and any CVE sub-block inside it are never moved or removed.
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
_SERVER_TITLE = re.compile(r"\bmongodb\s+server\b|\bserver\s+version\b", re.I)
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
    servers = [(s[0], s[1]) for s in spans if s[2][1] < _PSEUDO_LEVEL and _SERVER_TITLE.search(s[2][2])
               and not _DRIVER_TITLE.search(s[2][2])]
    stray = [s for s in spans if _CVE_STATUS_TITLE.match(s[2][2]) and not (d_start <= s[0] < d_end)
             and not any(a <= s[0] < b for a, b in servers)]
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


# --- Deterministic CVE status guard ------------------------------------------------------------------
# The model sometimes writes "No match ..." although the offline check found matching entries. This pass makes the report agree with offline_mongodb_server_cves / offline_driver_cves.
_SERVER_SECTION = re.compile(r"(?im)^#{1,3}\s*(?:\d+(?:\.\d+)*[.)]?\s*)?MongoDB server version\b[^\n]*$")
_DRIVER_SECTION = re.compile(r"(?im)^#{1,3}\s*(?:\d+(?:\.\d+)*[.)]?\s*)?Driver compatibility\b[^\n]*$")
_NO_MATCH_LINE = re.compile(r"(?im)^[^\n]*\bno match(?:es)?\b[^\n]*(?:offline|snapshot)[^\n]*$")
_ANY_HEADING = re.compile(r"(?m)^#{1,3}\s+\S")


def _section_span(markdown: str, heading: re.Pattern) -> tuple[int, int] | None:
    m = heading.search(markdown)
    if not m:
        return None
    level = len(m.group(0)) - len(m.group(0).lstrip("#"))
    nxt = re.compile(r"(?m)^#{1,%d}\s+\S" % level).search(markdown, m.end())
    return m.start(), (nxt.start() if nxt else len(markdown))


def _server_table(result: dict) -> list[str]:
    observed = result.get("observed_server") or {}
    refreshed = (result.get("catalog") or {}).get("refreshed_at") or "unknown date"
    lines = [f"Observed MongoDB server version **{observed.get('version')}** ({observed.get('source')}). "
             f"Offline NVD snapshot refreshed {refreshed}: **{result['match_count']} version-range match(es)**.", "",
             "| CVE | Severity | CVSS | Fixed in (same line) | Summary |",
             "|---|---|---|---|---|"]
    for m in result.get("matches") or []:
        summary = str(m.get("summary") or "").replace("|", "/")[:160]
        note = f" ({m['platform_note']})" if m.get("platform_note") else ""
        lines.append(f"| [{m['cve']}]({m.get('source')}) | {m.get('severity')} | {m.get('cvss_score') or ''} | "
                     f"{m.get('fixed_in') or 'see advisory'} | {summary}{note} |")
    if result.get("highest_fixed_in"):
        lines += ["", f"Patch target to confirm: **{result['highest_fixed_in']}** or later on this release line "
                      "(highest fix release among the matches; check the advisories). On Atlas, confirm the running "
                      "version and schedule the maintenance window rather than a manual binary upgrade."]
    lines += ["", "A version-range match is not proof that the vulnerable feature, edition or platform is in use."]
    return lines


def _driver_lines(result: dict) -> list[str]:
    refreshed = (result.get("catalog") or {}).get("refreshed_at") or "unknown date"
    out = []
    if result.get("matches"):
        out += [f"Offline driver CVE snapshot refreshed {refreshed}: **{result['match_count']} version-range match(es)** "
                "(application exposure unverified):", "",
                "| Driver / version | CVE | Severity | Affected range |", "|---|---|---|---|"]
        for m in result["matches"]:
            rng = "; ".join(f"{r.get('introduced')} – {r.get('fixed') or r.get('last_affected') or 'unfixed'}"
                            for r in m.get("affected") or [])
            out.append(f"| {m.get('matched_component')} {m.get('matched_version')} | [{m.get('cve')}]({m.get('source')}) | "
                       f"{m.get('severity')} | {rng} |")
    if result.get("drivers_not_covered_by_snapshot"):
        out.append("Not covered by the offline driver CVE snapshot (no verdict): "
                   + ", ".join(result["drivers_not_covered_by_snapshot"]) + ".")
    return out


def enforce_cve_status(markdown: str, server: dict | None, drivers: dict | None) -> tuple[str, list[str]]:
    """Replace a false "No match" statement (or add missing CVE evidence). Returns (markdown, fixes)."""
    fixes: list[str] = []
    server_wrong = bool(server) and server.get("status") != "no_match_in_snapshot"
    if server and server.get("status") == "matches_found":
        span = _section_span(markdown, _SERVER_SECTION)
        body = markdown[span[0]:span[1]] if span else ""
        missing = [m["cve"] for m in server.get("matches") or [] if m["cve"].upper() not in body.upper()]
        false_no_match = bool(_NO_MATCH_LINE.search(body)) and server_wrong
        if span is None or missing or false_no_match:
            block = _server_table(server)
            if span is None:
                heading = "## MongoDB server version and CVE review"
                driver = _section_span(markdown, _DRIVER_SECTION)
                insert = driver[0] if driver else len(markdown)
                markdown = markdown[:insert].rstrip() + "\n\n" + heading + "\n\n" + "\n".join(block) + "\n\n" + markdown[insert:].lstrip()
                fixes.append("added missing MongoDB server CVE section")
            else:
                head_end = markdown.index("\n", span[0]) + 1 if "\n" in markdown[span[0]:span[1]] else span[1]
                markdown = markdown[:head_end] + "\n" + "\n".join(block) + "\n\n" + markdown[span[1]:].lstrip("\n")
                fixes.append("replaced MongoDB server CVE section (" + ("false 'no match'" if false_no_match else
                             "missing " + ", ".join(missing)) + ")")
    if drivers and drivers.get("status") in ("matches_found", "not_covered_by_snapshot"):
        span = _section_span(markdown, _DRIVER_SECTION)
        if span:
            body = markdown[span[0]:span[1]]
            missing = [m["cve"] for m in drivers.get("matches") or [] if str(m.get("cve") or "").upper() not in body.upper()]
            no_match = _NO_MATCH_LINE.search(body)
            lines = _driver_lines(drivers)
            if lines and (no_match or missing):
                replacement = "\n".join(lines)
                if no_match:
                    new_body = body[:no_match.start()] + replacement + body[no_match.end():]
                else:
                    new_body = body.rstrip() + "\n\n" + replacement + "\n\n"
                markdown = markdown[:span[0]] + new_body + markdown[span[1]:]
                fixes.append("corrected driver CVE status (" + ("false 'no match'" if no_match else
                             "missing " + ", ".join(missing)) + ")")
    return re.sub(r"\n{3,}", "\n\n", markdown), fixes
