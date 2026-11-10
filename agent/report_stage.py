"""Build node reports, compare baselines, and summarize customer-impacting cluster issues."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from . import diffing, offline_cve
from .common import Layout, logger, shift
from .skills import diagnostic_skill_dir

BASELINES = (("n-1", -1, "yesterday"), ("n-8", -7, "one week ago"))
# Model input budget when MONGODB_LOG_DIAG_REPORT_MAX_INPUT_CHARS / report_max_input_chars is not set.
DEFAULT_MAX_INPUT_CHARS = 80_000
# Resource investigation runs for the node: the whole report stays on the occurrence extract, plus the
# resource_investigation and concurrency_findings documents. 200k chars (~50-60k tokens) keeps the
# episode-window occurrences of a busy node; lower it with report_max_input_chars for small-context models.
DEFAULT_MAX_INPUT_CHARS_RESOURCE = 200_000


def resolve_max_input_chars(configured, resource_investigation: bool) -> int:
    """Explicit positive setting wins; otherwise 200k with resource investigation, 80k without."""
    if configured is not None and str(configured).strip():
        try:
            value = int(configured)
        except (TypeError, ValueError) as exc:
            raise ValueError("report_max_input_chars must be an integer") from exc
        if value > 0:
            return value
    return DEFAULT_MAX_INPUT_CHARS_RESOURCE if resource_investigation else DEFAULT_MAX_INPUT_CHARS


def _read_skill_text(rel: str) -> str:
    return (diagnostic_skill_dir() / rel).read_text(encoding="utf-8")


def build_system_prompt() -> str:
    return "\n\n".join([
        "CVE and application-attribution safety rules OVERRIDE conflicting text in the base skill: "
        "The offline_driver_cves document is a limited reviewed snapshot, not a web lookup. A matching "
        "customer-driver/version is a review candidate, never proof of exploitation or vulnerable use. "
        "Only name an application when the supplied evidence explicitly establishes customer ownership; "
        "an observed appName alone is not proof of ownership. Omit unverified app names, Atlas-managed "
        "application names, and findings whose only subject is Atlas-managed software. For topology and "
        "TLS errors without verified customer attribution, state that the source is not identified; do not "
        "speculate about internal or customer ownership. "
        "Never fabricate CVEs, upgrades, application impact, or universal server-driver compatibility. "
        "The reviewed offline overlay has precedence over base analysis-prompt.md.",
        "Customer-facing exclusion rule: do not include any discussion of internal Atlas components, "
        "services, agents, maintenance/platform operations, internal driver/CVE findings, or internal "
        "system namespaces. Do not name them, describe their behavior, say they are affected, or mention "
        "them as excluded/irrelevant/unknown. Omit those observations silently. If an independently "
        "supported customer symptom exists, report only that symptom and its evidence without attributing "
        "it to an internal component. If attribution is unavailable, say 'source not identified from the "
        "available evidence'. Never recommend a customer change to Atlas-managed software. Distinguish "
        "observed from plausible customer impact. A customer namespace alone does not prove client "
        "ownership. Index recommendations require query-shape metrics plus explain/index inventory; "
        "change streams and natural scans do not establish missing indexes.",
        "Query Shape Insights rule: when a query_shape_insights_status document is supplied, this node has no "
        "per-node Query Shape Insights shape for the day. State its explanation in one sentence (e.g. sharded "
        "cluster: shapes are collected on mongos, not on shard mongods) and do not present the absence of "
        "shapes as evidence that queries are healthy or that no index is needed. If it has scope 'cluster', "
        "the query_stats document holds the CLUSTER-WIDE list copied to this node: you may cite those shapes "
        "as cluster-wide workload, but never claim they ran on this node.",
        "You are running inside an automated pipeline. Follow the MongoDB Log Diagnostic skill exactly.",
        "Adapter note: the analysis prompt refers to `/home/user/extraction.json`; extraction files are "
        "supplied inline in labelled <document> tags. `current` is extractionOccurence.json; `n-1` and "
        "`n-8` are extractionshort.json baselines. Use exact numbers from `precomputed_diff`. "
        "You have no web access. Use only reviewed offline_driver_cves matches as version-range review "
        "candidates with unverified application exposure; include catalog refreshed_at. All CVE "
        "information goes only inside the driver compatibility section (section 10); never add a separate "
        "top-level 'Driver CVE Status' section. If status is no_match_in_snapshot say so in one line inside "
        "that section without claiming safety. A match with review_status "
        "unreviewed_candidate comes from an automated OSV import nobody has vetted: label it "
        "'unreviewed candidate' and never present it as confirmed. Never invent CVEs. Output only Markdown.",
        "=== references/analysis-prompt.md ===\n" + _read_skill_text("references/analysis-prompt.md"),
        "=== references/extracted-signal-reference.md ===\n" + _read_skill_text("references/extracted-signal-reference.md"),
        "=== references/analysis-prompt.offline-cve-overlay.md ===\n" + _read_skill_text("references/analysis-prompt.offline-cve-overlay.md"),
        *_optional_skill_texts("references/resource-investigation-rules.md"),
    ])


def _optional_skill_texts(*rels: str) -> list[str]:
    """Extra skill references loaded into the system prompt when the bundled skill ships them."""
    out = []
    for rel in rels:
        try:
            out.append(f"=== {rel} ===\n" + _read_skill_text(rel))
        except OSError:
            logger.info("Skill reference %s not found; not added to the system prompt", rel)
    return out


def _doc(label: str, meta: dict, body: str) -> str:
    attrs = " ".join(f'{k}="{v}"' for k, v in {"label": label, **meta}.items())
    return f"<document {attrs}>\n{body}\n</document>"


def _compact(payload: dict, max_groups: int = 60) -> dict:
    """Last-resort size reduction: keep aggregates, cap group arrays."""
    p = json.loads(json.dumps(payload))
    for path in (("error_scan", "groups"), ("slow", "operations")):
        node = p
        for key in path[:-1]:
            node = node.get(key, {})
        if isinstance(node.get(path[-1]), list):
            node[path[-1]] = node[path[-1]][:max_groups]
    p["slow_operations"] = (p.get("slow") or {}).get("operations", p.get("slow_operations"))
    p.pop("trends", None)
    p.setdefault("quality", {}).setdefault("notes", []).append(
        f"Pipeline note: file compacted to fit model context (groups capped at {max_groups}, trends removed).")
    return p


def _load_extract(store, layout: Layout, date: str, host_dir: str, log_name: str, name: str):
    key = layout.extract(date, host_dir, log_name, name)
    if not store.exists(key):
        return None, key
    return json.loads(store.get_text(key)), key


def discover_nodes(store, layout: Layout, log_date: str) -> list[tuple[str, str]]:
    day = layout.day(log_date) + "/"
    nodes = set()
    for key in store.list_keys(day):
        parts = key[len(day):].split("/")
        if len(parts) == 4 and parts[1] == "extracts" and parts[3] == "extractionOccurence.json":
            nodes.add((parts[0], parts[2]))
    return sorted(nodes)


def hosts_without_extract(store, layout: Layout, log_date: str, nodes) -> list[str]:
    """Node folders present for the day (raw logs / observability) that have no occurrence extract."""
    day = layout.day(log_date) + "/"
    with_extract = {h for h, _ in nodes}
    hosts = {k[len(day):].split("/")[0] for k in store.list_keys(day) if "/" in k[len(day):]}
    return sorted(h for h in hosts - with_extract if h != "cluster")


def _validate_offline_cves(markdown: str, allowed: set[str]) -> None:
    mentioned = {c.upper() for c in re.findall(r"\bCVE-\d{4}-\d{4,}\b", markdown, re.I)}
    extra = mentioned - {c.upper() for c in allowed}
    if extra:
        raise ValueError("Report contains CVE IDs not in reviewed offline matches: " + ", ".join(sorted(extra)))


def _redact_unvetted_cves(markdown: str, allowed: set[str]) -> tuple[str, list[str]]:
    """Replace CVE IDs that are not in the reviewed offline matches instead of discarding the report."""
    allowed_up = {c.upper() for c in allowed}
    removed: list[str] = []

    def repl(m):
        if m.group(0).upper() in allowed_up:
            return m.group(0)
        removed.append(m.group(0).upper())
        return "[unverified CVE reference removed]"
    return re.sub(r"\bCVE-\d{4}-\d{4,}\b", repl, markdown, flags=re.I), sorted(set(removed))


def _load_metrics(store, layout, log_date: str, host_dir: str):
    """Return the node's metrics.json payload, or None when absent/unparsable/without data points."""
    from .resource_investigation import metrics_present
    key = layout.observability(log_date, host_dir, "metrics")
    if not store.exists(key):
        return None
    try:
        payload = json.loads(store.get_text(key))
    except (TypeError, ValueError):
        logger.warning("Metrics file unparsable node=%s; APPENDIX 1 (metrics graphs) and resource investigation skipped", host_dir)
        return None
    return payload if metrics_present(payload) else None


def _metrics_appendix(payload: dict, host_dir: str) -> str | None:
    """Render the metrics as one A4 page embedded in the Markdown as a base64 PNG data URI.

    Returns None when no selected group has data (APPENDIX 1 is then omitted). Rendering
    failures never fail the report: the appendix states why the graph is unavailable.
    """
    import base64
    import tempfile
    from pathlib import Path
    from . import metrics_graphs

    panels, empty = metrics_graphs.plan_panels(metrics_graphs.load_series(payload))
    if not panels:
        return None
    window = payload.get("window", {}) if isinstance(payload, dict) else {}
    subtitle = " · ".join(x for x in (
        f"{str(window.get('since', ''))[:16].replace('T', ' ')} → {str(window.get('until', ''))[:16].replace('T', ' ')} UTC"
        if window else "", f"granularity {window['granularity']}" if window.get("granularity") else "",
        "solid = left axis, dashed = right axis") if x)
    try:
        with tempfile.TemporaryDirectory(prefix="metrics-graph-") as tmp:
            png = str(Path(tmp) / "metrics-appendix.png")
            metrics_graphs.render_one_page(panels, f"Node {host_dir} — metrics", png, None, subtitle=subtitle)
            data_uri = "data:image/png;base64," + base64.b64encode(Path(png).read_bytes()).decode("ascii")
    except ImportError:
        return metrics_graphs.appendix_markdown(None, panels, empty, payload,
                                                unavailable_reason="matplotlib is not installed in this runtime")
    except Exception as exc:  # never let a graph break the report
        logger.warning("Metrics graph failed node=%s error_type=%s", host_dir, type(exc).__name__)
        return metrics_graphs.appendix_markdown(None, panels, empty, payload,
                                                unavailable_reason="graph rendering failed")
    return metrics_graphs.appendix_markdown(data_uri, panels, empty, payload)


def _advisor_appendix(store, layout, log_date: str, host_dir: str) -> str | None:
    """APPENDIX 2 body from the node's advisor/advisor.json; None when absent, unparsable or empty."""
    from . import advisor_appendix
    key = layout.observability(log_date, host_dir, "advisor")
    if not store.exists(key):
        return None
    try:
        payload = json.loads(store.get_text(key))
    except (TypeError, ValueError):
        logger.warning("Advisor file unparsable node=%s; APPENDIX 2 (Performance Advisor) skipped", host_dir)
        return None
    index_definitions = None
    idx_key = layout.observability(log_date, host_dir, "index_definitions")
    if store.exists(idx_key):
        try:
            index_definitions = json.loads(store.get_text(idx_key))
        except (TypeError, ValueError):
            index_definitions = None
    body = advisor_appendix.render(payload, index_definitions)
    if body is None:
        logger.info("Advisor file has no customer suggestion/shape node=%s; APPENDIX 2 omitted", host_dir)
    return body


def _add_appendices(report_md: str, store, layout, log_date: str, host_dir: str, metrics_payload, config) -> str:
    """APPENDIX 1 (metrics) and APPENDIX 2 (advisor), each only when its data exists."""
    from .report_shape_mapping import append_advisor_appendix, append_metrics_appendix
    if metrics_payload is not None and config.get("metrics_graphs_enabled", True):
        metrics_md = _metrics_appendix(metrics_payload, host_dir)
        if metrics_md:
            report_md = append_metrics_appendix(report_md, metrics_md)
    if config.get("advisor_appendix_enabled", True):
        advisor_md = _advisor_appendix(store, layout, log_date, host_dir)
        if advisor_md:
            report_md = append_advisor_appendix(report_md, advisor_md)
    return report_md


RESOURCE_INSTRUCTIONS = (
    "Per-node metrics are supplied. Include a section titled exactly `## High resource usage investigation` "
    "(before any appendix) with subsections `### A — CPU`, `### B — Memory`, `### C — Disk`. Base it on the "
    "`resource_investigation` document (deterministic rule results; thresholds inside) and on the "
    "occurrence_timestamps of `current` (extractionOccurence.json, UTC). Rules: A) normalized system CPU > 90%, "
    "or > 70% for more than 20 minutes; B) system memory used > 75% — explain using connections (about 1 MB "
    "each), open cursors, global lock/operation queue, and errors/slow operations at the same time; C) disk "
    "pressure when DISK_QUEUE_DEPTH > 10 on several samples. Evaluate every rule on the average series "
    "(SYSTEM_NORMALIZED_CPU_*, DISK_QUEUE_DEPTH); quote the MAX_* series only as context. For each triggered rule give the episode window "
    "(UTC and project timezone), the peak, then the plausible causes ranked by evidence (namespace, log "
    "query hash, plan summary, occurrences in the window, duration/cpuNanos/docsExamined/storage bytes read, "
    "co-occurring errors) with confidence. Call causes plausible, never confirmed. If no occurrence falls in "
    "the window say 'source not identified from the available evidence'. For a rule not triggered write one "
    "line with the observed maximum; for status no_data say it was not evaluated and why. Before blaming "
    "CPU capacity or an individual query, read each episode's `contention`: when its verdict is "
    "hot_document_contention_likely the first-ranked cause is write contention on one document (conflict "
    "retries burn CPU and hold write tickets), so say so, refer to the write contention section, and do not "
    "recommend a larger tier or an index as the fix. `correlated.unsampled_active_groups` are capped groups "
    "that may have run in the window without a retained sample: mention them as a data limitation."
)


CONCURRENCY_INSTRUCTIONS = (
    "A `concurrency_findings` document is supplied: deterministic write-contention evaluation of the FULL "
    "occurrence extract (uncapped minute timeline and hot write targets), computed before any size reduction "
    "of `current`. It has precedence over slow-group occurrence samples for times and counts. When "
    "`contention_detected` is true, include a section titled exactly `## Write contention (hot document)` right "
    "after the executive summary: one P1 finding per incident window with verdict "
    "hot_document_contention_likely (start, peak and end UTC and project timezone; namespace, filter fields, "
    "hashed target id, writes, write conflicts and their share of the window, peak writes per minute, ratio "
    "of slow operations and slow-operation CPU versus baseline, apps and query hashes), the mechanism (each "
    "write conflict is a retry that burns CPU and holds a write ticket, so the whole node slows down) and "
    "application-side fixes. Rank it above CPU capacity, index and individual-query findings in the same "
    "window and say that a larger tier or an index will not remove it. Never try to reveal hashed target "
    "values. List `coverage.heavy_groups_without_samples_in_windows` as a data limitation. When status is "
    "not_extracted, say in one line that write conflicts were not extracted (older parser), never that "
    "there were none."
)
CONTENTION_SECTION_RE = re.compile(r"(?im)^#{1,3}\s*Write contention\b")


def _contention_graph(current: dict, findings: dict, host_dir: str) -> str | None:
    """Embedded (base64 PNG) contention timeline; None when there is no incident window."""
    import base64
    import tempfile
    from pathlib import Path
    from . import concurrency_graphs as cg
    if not (findings or {}).get("incident_windows"):
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="contention-graph-") as tmp:
            png = str(Path(tmp) / "contention.png")
            if not cg.render_png(current, findings, png, f"Node {host_dir} — write contention (log)"):
                return None
            data_uri = "data:image/png;base64," + base64.b64encode(Path(png).read_bytes()).decode("ascii")
    except ImportError:
        return cg.section_markdown(None, findings, "matplotlib is not installed in this runtime")
    except Exception as exc:  # never let a graph break the report
        logger.warning("Contention graph failed node=%s error_type=%s", host_dir, type(exc).__name__)
        return cg.section_markdown(None, findings, "graph rendering failed")
    return cg.section_markdown(data_uri, findings)


def _insert_in_section(report_md: str, section_re, block: str) -> str | None:
    """Insert ``block`` at the end of the first section matching ``section_re`` (before the next
    heading of the same or higher level); None when the section is absent."""
    m = section_re.search(report_md)
    if not m:
        return None
    level = len(report_md[m.start():].split(" ", 1)[0])
    nxt = re.compile(r"(?m)^#{1,%d}\s" % level).search(report_md, m.end())
    pos = nxt.start() if nxt else len(report_md)
    return report_md[:pos].rstrip() + "\n\n" + block.rstrip() + "\n\n" + report_md[pos:].lstrip("\n")


def _add_contention_graph(report_md: str, current: dict, findings, host_dir: str, config: dict) -> str:
    """Optional graph inside `## Write contention` (else `## High resource usage investigation`)."""
    if not findings or not config.get("concurrency_graphs_enabled", True):
        return report_md
    from .resource_investigation import SECTION_RE
    for section_re in (CONTENTION_SECTION_RE, SECTION_RE):
        if section_re.search(report_md):
            block = _contention_graph(current, findings, host_dir)
            return (_insert_in_section(report_md, section_re, block) or report_md) if block else report_md
    return report_md


def _keep_windows(analysis, findings):
    """Occurrence windows kept in full while shrinking: resource episodes + contention incident windows."""
    from datetime import timedelta
    from . import resource_investigation as ri
    windows = ri.episode_windows(analysis) if analysis is not None else []
    for w in (findings or {}).get("incident_windows") or []:
        a, b = ri._ts(w["start_utc"]), ri._ts(w["end_utc"])
        if a and b:
            windows.append((a - timedelta(minutes=5), b + timedelta(minutes=6)))
    return windows


def report_node(store, layout, llm, system_prompt, log_date, host_dir, log_name, max_chars,
                config: dict | None = None) -> dict:
    current, cur_key = _load_extract(store, layout, log_date, host_dir, log_name, "extractionOccurence.json")
    if current is None:
        raise FileNotFoundError(cur_key)
    current_file = "extractionOccurence.json"
    config = config or {}
    from . import resource_investigation as ri
    metrics_payload = _load_metrics(store, layout, log_date, host_dir)
    analysis = None
    if metrics_payload is not None and config.get("resource_investigation_enabled", True):
        analysis = ri.analyze(metrics_payload, current, config)
        logger.info("Resource investigation node=%s triggered=%s", host_dir, analysis["triggered"])
    findings = None
    if config.get("concurrency_findings_enabled", True):
        from . import concurrency_findings
        # Computed on the FULL extract: the reduction ladder below drops the minute timeline and samples.
        findings = concurrency_findings.summarize(current, config)
        if findings and findings.get("status") == "ok":
            logger.info("Concurrency findings node=%s contention_detected=%s windows=%d hot_documents=%d",
                        host_dir, findings["contention_detected"], len(findings["incident_windows"]),
                        len(findings["hot_documents"]))
    configured_max = max_chars
    max_chars = resolve_max_input_chars(configured_max, analysis is not None)
    logger.info("Report input limit node=%s max_chars=%d (%s)", host_dir, max_chars,
                "configured" if max_chars == configured_max else
                "default with resource investigation" if analysis is not None else "default")
    # Opt-in only; when unset, offline_cve falls back to MONGODB_LOG_DIAG_OFFLINE_CVE_INCLUDE_UNREVIEWED.
    cve_options = {"include_unreviewed": True} if config.get("offline_cve_include_unreviewed") else {}
    offline_cves = offline_cve.check(current, **cve_options)
    baselines, diffs = [], []
    for label, offset, desc in BASELINES:
        bdate = shift(log_date, offset)
        base, bkey = _load_extract(store, layout, bdate, host_dir, log_name, "extractionshort.json")
        if base is None:
            logger.info("No %s baseline for %s/%s at %s", label, host_dir, log_name, bkey)
            continue
        baselines.append((label, bdate, desc, base))
        diffs.append(diffing.compare(current, base, label, bdate))
    diff_doc = {"node": host_dir, "log_name": log_name, "current_date": log_date,
                "baselines_available": [d["label"] for d in diffs],
                "baselines_missing": [l for l, _, _ in BASELINES if l not in {d["label"] for d in diffs}],
                "comparisons": diffs}

    raw_docs = {"enabled": True}

    def assemble(cur_payload, cur_name, base_payloads):
        docs = [_doc("current", {"date": log_date, "node": host_dir, "log": log_name, "file": cur_name},
                     json.dumps(cur_payload, separators=(",", ":")))]
        for label, bdate, desc, payload in base_payloads:
            docs.append(_doc(label, {"date": bdate, "meaning": desc, "node": host_dir,
                                     "file": "extractionshort.json"},
                             json.dumps(payload, separators=(",", ":"))))
        if diffs:
            docs.append(_doc("precomputed_diff", {}, json.dumps(diff_doc, separators=(",", ":"))))
        docs.append(_doc("offline_driver_cves", {}, json.dumps(offline_cves, separators=(",", ":"))))
        if analysis is not None:
            docs.append(_doc("resource_investigation", {"node": host_dir},
                             json.dumps(analysis, separators=(",", ":"), default=str)))
        if findings is not None:
            docs.append(_doc("concurrency_findings", {"node": host_dir},
                             json.dumps(findings, separators=(",", ":"), default=str)))
        for label, key in (("index_stats", layout.index_stats(log_date, host_dir)),
                           ("query_stats", layout.query_stats(log_date, host_dir))):
            if raw_docs["enabled"] and store.exists(key):
                docs.append(_doc(label, {"node": host_dir}, store.get_text(key)))
        qs_key = layout.query_stats(log_date, host_dir)
        if store.exists(qs_key):
            try:
                qs_data = json.loads(store.get_text(qs_key))
                if not _rank_shapes(qs_data) or (isinstance(qs_data, dict) and qs_data.get("scope") == "cluster"):
                    status = query_stats_status(qs_data) or {
                        "explanation": "Query Shape Insights returned no shape for this node in the log-day window."}
                    docs.append(_doc("query_shape_insights_status", {"node": host_dir},
                                     json.dumps(status, separators=(",", ":"))))
            except (ValueError, TypeError):
                pass
        header = (f"Generate the MongoDB Log Diagnostic report for node `{host_dir}` ({log_name} log), "
                  f"log day {log_date} (Atlas project timezone day). Baselines supplied: "
                  f"{', '.join(d['label'] for d in diffs) or 'none -- omit historical-comparison claims'}.")
        if analysis is not None:
            header += "\n\n" + RESOURCE_INSTRUCTIONS
        if findings is not None:
            header += "\n\n" + CONCURRENCY_INSTRUCTIONS
        return header + "\n\n" + "\n\n".join(docs)

    user = assemble(current, current_file, baselines)
    if analysis is not None:
        # Metrics present: the whole report stays on the occurrence extract (never extractionshort.json).
        # Shrink by trimming occurrences outside the high-resource episode windows first.
        windows = _keep_windows(analysis, findings)
        for keep in (200, 50, 10, 0):
            if len(user) <= max_chars:
                break
            current_file = f"extractionOccurence.json (occurrences outside resource episodes capped at {keep}/group)"
            reduced = ri.trim_occurrences(current, windows, keep)
            user = assemble(reduced, current_file, baselines)
        if len(user) > max_chars:
            current_file = "extractionOccurence.json (episode occurrences only, groups capped at 60)"
            reduced = ri.trim_occurrences(current, windows, 0, max_groups=60)
            user = assemble(reduced, current_file, baselines)
        if len(user) > max_chars and baselines:
            baselines = [(l, d, s_, _compact(p)) for l, d, s_, p in baselines]
            user = assemble(reduced, current_file, baselines)
        if len(user) > max_chars and baselines:
            logger.warning("Report input node=%s still %d chars (limit %d); dropping baselines",
                           host_dir, len(user), max_chars)
            baselines = []
            user = assemble(reduced, current_file + ", baselines dropped", [])
    else:
        if len(user) > max_chars:
            short, _ = _load_extract(store, layout, log_date, host_dir, log_name, "extractionshort.json")
            current_file = "extractionshort.json"
            user = assemble(short or current, current_file, baselines)
        if len(user) > max_chars:
            current_file = "extractionshort.json (compacted)"
            user = assemble(_compact(short or current), current_file,
                            [(l, d, s_, _compact(p)) for l, d, s_, p in baselines])
        if len(user) > max_chars and baselines:
            logger.warning("Report input node=%s still %d chars after compaction (limit %d); dropping baselines",
                           host_dir, len(user), max_chars)
            current_file = "extractionshort.json (compacted, baselines dropped)"
            baselines = []
            user = assemble(_compact(short or current), current_file, [])
    if len(user) > max_chars:
        # Raw index/query stats are unbounded; APPENDIX 3 still maps hashes from the stored file.
        logger.warning("Report input node=%s still %d chars (limit %d); dropping raw index_stats/query_stats",
                       host_dir, len(user), max_chars)
        raw_docs["enabled"] = False
        current_file += ", raw index/query stats omitted"
        base_payloads = [(l, d, s_, _compact(p)) for l, d, s_, p in baselines]
        if analysis is not None:
            user = assemble(ri.trim_occurrences(current, _keep_windows(analysis, findings), 0, max_groups=60),
                            current_file, base_payloads)
        else:
            user = assemble(_compact(_load_extract(store, layout, log_date, host_dir, log_name,
                                                   "extractionshort.json")[0] or current),
                            current_file, base_payloads)
    for groups in (30, 15):
        if len(user) <= max_chars:
            break
        logger.warning("Report input node=%s still %d chars (limit %d); capping groups at %d",
                       host_dir, len(user), max_chars, groups)
        baselines = []
        current_file = f"{current_file.split(' (')[0]} (groups capped at {groups}, baselines and raw stats omitted)"
        if analysis is not None:
            reduced = ri.trim_occurrences(current, _keep_windows(analysis, findings), 0, max_groups=groups)
        else:
            reduced = _load_extract(store, layout, log_date, host_dir, log_name, "extractionshort.json")[0] or current
        user = assemble(_compact(reduced, max_groups=groups), current_file, [])
    if len(user) > max_chars:
        # Bounded last resort: shrink every document (never truncate JSON) instead of failing the node.
        from .report_budget import fit_prompt
        user, budget = fit_prompt(user, max_chars)
        current_file += " (budget-reduced)"
        logger.warning("Report input node=%s reduced %d -> %d chars (limit %d) levels=%s dropped=%s "
                       "largest_before=%s", host_dir, budget["before_chars"], budget["after_chars"],
                       max_chars, budget.get("levels"), budget.get("dropped"), budget["sizes_before"][:4])
    logger.info("Report input node=%s chars=%d current_file=%s baselines=%s",
                host_dir, len(user), current_file, [b[0] for b in baselines])
    report_md = llm.generate(system_prompt, user)
    if analysis is not None and not ri.SECTION_RE.search(report_md):
        logger.warning("Model omitted the resource investigation section node=%s; adding deterministic fallback",
                       host_dir)
        report_md = report_md.rstrip() + "\n\n" + ri.render_markdown(analysis)
    if findings and findings.get("contention_detected") and not CONTENTION_SECTION_RE.search(report_md):
        from . import concurrency_findings
        logger.warning("Model omitted the write contention section node=%s; adding deterministic fallback", host_dir)
        report_md = report_md.rstrip() + "\n\n" + concurrency_findings.render_markdown(findings)
    from .report_shape_mapping import append_reported_shapes
    stats_key = layout.query_stats(log_date, host_dir)
    report_md = append_reported_shapes(
        report_md, current, store.get_text(stats_key) if store.exists(stats_key) else None)
    report_md, removed_internal = _strip_internal_content(report_md)
    if removed_internal:
        logger.info("Removed %d internal-only content block(s) from node report", removed_internal)
    from .report_cve_section import relocate_standalone_cve_status
    report_md, relocated_cve = relocate_standalone_cve_status(report_md)
    if relocated_cve:
        logger.info("Removed %d standalone driver-CVE status block(s); CVE details stay in the driver "
                    "section node=%s", relocated_cve, host_dir)
    # APPENDIX 1 (metrics graphs) and APPENDIX 2 (Performance Advisor) only when their data exist;
    # placed ahead of APPENDIX 3 (query forms), after the internal-content filter so the embedded
    # base64 image is never altered (the advisor renderer already omits system namespaces).
    report_md = _add_appendices(report_md, store, layout, log_date, host_dir, metrics_payload, config)
    # Optional write-contention graph (base64 PNG) inside its section, also after the internal filter.
    report_md = _add_contention_graph(report_md, current, findings, host_dir, config)
    report_md, removed_cves = _redact_unvetted_cves(report_md, {m["cve"] for m in offline_cves["matches"]})
    if removed_cves:
        logger.warning("Removed %d CVE ID(s) not in the reviewed offline matches node=%s: %s",
                       len(removed_cves), host_dir, ", ".join(removed_cves))
    uploads = {"report": store.put_text(layout.report(log_date, host_dir, log_name, "report.md"),
                                        report_md, "text/markdown; charset=utf-8")}
    if analysis is not None:
        uploads["resource_investigation"] = store.put_text(
            layout.report(log_date, host_dir, log_name, "resource-investigation.json"),
            json.dumps(analysis, indent=2, default=str), "application/json")
    if findings is not None:
        uploads["concurrency_findings"] = store.put_text(
            layout.report(log_date, host_dir, log_name, "concurrency-findings.json"),
            json.dumps(findings, indent=2, default=str), "application/json")
    if diffs:
        uploads["diff"] = store.put_text(layout.report(log_date, host_dir, log_name, "diff.json"),
                                         json.dumps(diff_doc, indent=2), "application/json")
    return {"node": host_dir, "log_name": log_name, "current_file": current_file, "max_input_chars": max_chars,
            "baselines": [b[0] for b in baselines], "offline_driver_cves": offline_cves,
            "resource_investigation": analysis["triggered"] if analysis is not None else None,
            "contention_detected": bool(findings and findings.get("contention_detected")),
            "uploads": uploads, "report_md": report_md}


# Only include aggregated shape metrics; never pass query filters or literal values to the model.
_SHAPE_FIELDS = ("namespace", "ns", "database", "collection", "command", "queryShapeHash",
                 "queryHash", "execCount", "avgWorkingMillis", "docsExaminedRatio", "docsExamined",
                 "docsReturned", "keysExamined", "bytesRead", "totalExecMicros")
_METRIC_FIELDS = ("sum", "avg", "max", "total", "value")


def _safe_scalar(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str) and len(value) < 160 and not any(c in value for c in "{}\n\r"):
        return value
    return None


def _shape_records(obj, depth=0):
    if depth > 8:
        return
    if isinstance(obj, list):
        for item in obj:
            yield from _shape_records(item, depth + 1)
    elif isinstance(obj, dict):
        if any(k in obj for k in ("queryShapeHash", "docsExaminedRatio", "avgWorkingMillis")) or (
                "metrics" in obj and ("key" in obj or "namespace" in obj)):
            yield obj
            return
        for value in obj.values():
            if isinstance(value, (list, dict)):
                yield from _shape_records(value, depth + 1)


def _shape(record):
    result = {}
    sources = [record]
    if isinstance(record.get("key"), dict):
        sources += [record["key"], record["key"].get("queryShape", {})]
    if isinstance(record.get("metrics"), dict):
        sources.append(record["metrics"])
    for source in sources:
        if not isinstance(source, dict):
            continue
        for field in _SHAPE_FIELDS:
            value = source.get(field)
            if field in ("namespace", "ns", "database", "collection", "command", "queryShapeHash", "queryHash"):
                value = _safe_scalar(value)
            elif isinstance(value, dict):
                value = {k: value[k] for k in _METRIC_FIELDS
                         if isinstance(value.get(k), (int, float)) and not isinstance(value[k], bool)}
            elif isinstance(value, bool) or not isinstance(value, (int, float)):
                value = None
            if value is not None and field not in result:
                result[field] = value
    return result


def _metric_number(value):
    if isinstance(value, dict):
        return max((v for v in value.values() if isinstance(v, (int, float))), default=0)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _rank_shapes(data):
    shapes = [_shape(r) for r in _shape_records(data)]
    shapes = [s for s in shapes if s]
    shapes.sort(key=lambda s: (_metric_number(s.get("docsExaminedRatio")),
                               _metric_number(s.get("avgWorkingMillis")),
                               _metric_number(s.get("execCount"))), reverse=True)
    return shapes


def query_stats_status(data) -> dict:
    """Compact, deterministic explanation of an empty Query Shape Insights file."""
    if not isinstance(data, dict):
        return {}
    diagnostics = data.get("diagnostics") if isinstance(data.get("diagnostics"), dict) else {}
    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    collection = payload.get("queryStatsCollection") if isinstance(payload.get("queryStatsCollection"), dict) else {}
    out = {k: diagnostics[k] for k in ("reason", "explanation", "per_process_summaries", "cluster_wide_summaries",
                                         "process_type", "fallback") if diagnostics.get(k) is not None}
    if data.get("scope") == "cluster":
        out["scope"] = "cluster"
        out["attribution_note"] = data.get("attribution_note") or (
            "Cluster-wide Query Shape Insights: not attributable to a single node.")
    if collection:
        out["collection_mode"] = collection.get("mode")
        if collection.get("rateLimitPerSecond") is not None:
            out["rate_limit_per_second"] = collection["rateLimitPerSecond"]
    return out


def summarize_query_stats(store, layout, log_date, nodes, limit=15):
    output = []
    cluster_copy = None
    for host in sorted({host for host, _ in nodes}):
        key = layout.query_stats(log_date, host)
        index_available = store.exists(layout.index_stats(log_date, host))
        if not store.exists(key):
            output.append({"node": host, "status": "not_collected", "index_stats_available": index_available})
            continue
        try:
            data = json.loads(store.get_text(key))
            shapes = _rank_shapes(data)
            if isinstance(data, dict) and data.get("scope") == "cluster":
                # Same cluster-wide list copied to every node: listed once under node "cluster".
                item = {"node": host, "status": "cluster_wide_copy", "index_stats_available": index_available,
                        "shapes_found": len(shapes), **query_stats_status(data)}
                if cluster_copy is None and shapes:
                    cluster_copy = shapes
            else:
                item = {"node": host, "status": "available" if shapes else "empty",
                        "index_stats_available": index_available,
                        "shapes_found": len(shapes), "top_shapes": shapes[:limit]}
                if not shapes:
                    item.update(query_stats_status(data))
            output.append(item)
        except (ValueError, TypeError) as exc:
            output.append({"node": host, "status": "invalid_query_stats",
                           "index_stats_available": index_available, "error_type": type(exc).__name__})
    cluster_key = getattr(layout, "cluster_query_stats", None)
    cluster_key = cluster_key(log_date) if cluster_key else None
    if cluster_key and store.exists(cluster_key):
        try:
            shapes = _rank_shapes(json.loads(store.get_text(cluster_key)))
            output.append({"node": "cluster", "scope": "cluster", "status": "available" if shapes else "empty",
                           "note": "Cluster-wide Query Shape Insights (on sharded clusters collected on mongos); "
                                   "not attributable to a single shard node.",
                           "shapes_found": len(shapes), "top_shapes": shapes[:limit]})
        except (ValueError, TypeError) as exc:
            output.append({"node": "cluster", "scope": "cluster", "status": "invalid_query_stats",
                           "error_type": type(exc).__name__})
    elif cluster_copy:
        output.append({"node": "cluster", "scope": "cluster", "status": "available",
                       "note": "Cluster-wide Query Shape Insights (copied to the node files); "
                               "not attributable to a single node.",
                       "shapes_found": len(cluster_copy), "top_shapes": cluster_copy[:limit]})
    return output


# Remove internal-only material from customer-facing Markdown even if the model emits it.
# The filter intentionally targets known Atlas-managed component names, internal markers, and
# system namespaces; it does not broadly remove ordinary mentions of MongoDB Atlas or customer software.
_INTERNAL_REPORT_RE = re.compile(
    r"\binternal\b|"
    r"\batlas[\s-]+managed\b|"
    r"\bplatform[\s-]+operations?\b|\bsystem[\s-]+namespaces?\b|"
    r"\b(?:mongot|automation[\s-]+agent|monitoring[\s-]+module)\b|"
    r"\batlas[\s-]+(?:automation|monitoring)(?:[\s-]+(?:agent|module|client|service))?\b|"
    r"\batlas[\s-]+search[\s-]+(?:java[\s-]+)?driver\b|"
    r"__mdb_internal(?:_[\w.$-]*)?|"
    r"(?:^|[\s`])(?:admin|config)\.system(?:\.[\w.$-]+)?|"
    r"(?:^|[\s`])local\.oplog\.rs\b",
    re.I,
)
_HEADING_RE = re.compile(r"^\s*(#{1,6})\s+.*$")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")


def _strip_internal_content(markdown: str) -> tuple[str, int]:
    """Drop internal-only paragraphs, list items, table rows, and titled sections."""
    blocks = re.split(r"\n[ \t]*\n", markdown.strip()) if markdown.strip() else []
    kept: list[str] = []
    removed = 0
    skip_heading_level: int | None = None

    for block in blocks:
        lines = block.splitlines()
        if not lines:
            continue
        first_heading = _HEADING_RE.match(lines[0])
        if skip_heading_level is not None:
            if first_heading and len(first_heading.group(1)) <= skip_heading_level:
                skip_heading_level = None
            else:
                removed += 1
                continue
        if first_heading and _INTERNAL_REPORT_RE.search(lines[0]):
            skip_heading_level = len(first_heading.group(1))
            removed += 1
            continue

        # Preserve valid tables while removing only rows containing internal-only content.
        if len(lines) >= 2 and all(line.lstrip().startswith("|") for line in lines):
            table_lines = []
            for idx, line in enumerate(lines):
                if idx < 2 and _INTERNAL_REPORT_RE.search(line):
                    table_lines = []
                    removed += 1
                    break
                if idx >= 2 and _INTERNAL_REPORT_RE.search(line):
                    removed += 1
                    continue
                table_lines.append(line)
            if table_lines and len(table_lines) > 2:
                kept.append("\n".join(table_lines))
            elif table_lines:
                removed += 1
            continue

        # Drop offending bullets independently so unrelated customer findings survive.
        if _LIST_ITEM_RE.match(lines[0]):
            items: list[list[str]] = []
            for line in lines:
                if _LIST_ITEM_RE.match(line) or not items:
                    items.append([line])
                else:
                    items[-1].append(line)
            safe_items = []
            for item in items:
                text = "\n".join(item)
                if _INTERNAL_REPORT_RE.search(text):
                    removed += 1
                else:
                    safe_items.append(text)
            if safe_items:
                kept.append("\n".join(safe_items))
            continue

        # Preserve supported customer facts in a paragraph while dropping only the
        # sentence that names an internal component or makes an internal-only claim.
        sentences = re.split(r"(?<=[.!?])\s+", block)
        safe_sentences = []
        dropped_sentences = 0
        for sentence in sentences:
            if _INTERNAL_REPORT_RE.search(sentence):
                dropped_sentences += 1
            elif sentence.strip():
                safe_sentences.append(sentence.strip())
        if dropped_sentences:
            removed += dropped_sentences
            if safe_sentences:
                kept.append(" ".join(safe_sentences))
            continue
        kept.append(block)

    result = "\n\n".join(kept).strip()
    if not result:
        result = "No customer-facing findings could be substantiated from the supplied evidence."
    return result + "\n", removed


_DATA_URI_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(data:image/[a-z+]+;base64,[A-Za-z0-9+/=]+\)")


def _without_embedded_images(markdown: str) -> str:
    """Replace base64-embedded images (APPENDIX 1) by a placeholder before sending a report to a model."""
    return _DATA_URI_IMAGE_RE.sub(lambda m: f"[embedded image omitted: {m.group(1)[:120]}]", markdown)


def _validate_customer_summary(text: str) -> None:
    if re.search(r"\bCVE-\d{4}-\d{4,}\b", text, re.I):
        raise ValueError("Cluster summary contains a CVE without customer-use verification")


CLUSTER_PROMPT = (
    "Begin with a short ## Executive summary before the priority sections: "
    "three observed or plausible customer risks, the top immediate actions, "
    "and the main uncertainty or missing evidence. Do not assert unverified impact. "
    "Write a concise customer-facing Markdown cluster summary. Prioritize confirmed or plausible "
    "customer-facing availability, application query latency, query-shape regressions, and indexing "
    "candidates. Completely omit Atlas-internal or Atlas-managed components, services, agents, platform "
    "operations, their names, and their driver/CVE findings, even as exclusions or uncertain hypotheses. "
    "If logs show a customer-relevant symptom but its source is unknown, report only the symptom and say "
    "the source is not identified; do not speculate about internal systems. For each priority give impact, "
    "evidence (node/namespace/query hash/metrics), "
    "affected customer application if attributable, owner/action, and confidence; otherwise say "
    "application attribution unknown. Query Shape Insights may use a recent lookback rather than the "
    "log day: don't claim same-day timing without timestamps. Rank customer query shapes by work, "
    "latency, volume and documents examined per returned document. Treat high scan ratio as an index "
    "candidate, not proof of missing index; require explain and index inventory before asserting an "
    "index is missing or naming a key order. Change streams, $natural, oplog, admin, and "
    "__mdb_internal_search are not evidence of a missing customer index. If shapes/indexStats are "
    "inconclusive, say 'No index gap verified from available evidence' and say what to collect. "
    "Use only supplied node reports and bounded query evidence; do not invent metrics or CVEs. "
    "Do not instruct customers to change vendor-managed platform components. State n-1/n-8 trends only if "
    "present. Do not fill a quota of actions. Never expose credentials, IPs or query literals. "
    "Query-shape evidence with status 'empty' is not proof of a healthy workload: state its reason/explanation "
    "in one sentence. An item with scope 'cluster' holds cluster-wide (mongos) shapes; use it for query-shape "
    "conclusions without attributing those shapes to a specific shard node."
)

TREND_INSTRUCTIONS = (
    "A `trend_alerts` document lists, deterministically, problems that are new versus BOTH n-1 and n-8 or "
    "at least +{pct:g}% above n-1 or n-8: unindexed (COLLSCAN) or poorly indexed queries, in-memory sorts, "
    "error groups and aggregate signals. The pipeline inserts the section `## New or sharply increasing "
    "issues (vs n-1 and n-8)` with the exact table right after your executive summary: do not write that "
    "section or repeat its table. Mention the most important new or increasing items in the executive "
    "summary and the priorities with their exact numbers; call them changes versus the baselines, not "
    "proven causes, and keep index wording as candidates to validate."
)


CLUSTER_SUMMARY_SOURCES = ("extract", "node_reports", "both")
NODE_REPORTS_SUMMARY_FILE = "cluster-summary.from-node-reports.md"

NODE_REPORTS_ONLY_INSTRUCTIONS = (
    "SOURCE RESTRICTION: this summary is built ONLY from the per-node report Markdown files below "
    "(each already analysed its extract, n-1/n-8 baselines, query statistics and metrics). No raw "
    "extraction, trend table or separate query-shape evidence is supplied. Consolidate across nodes: "
    "merge findings that appear on several nodes into one cluster-level item and list the nodes, keep "
    "node-specific findings attributed to their node, and resolve or flag contradictions between "
    "reports. Copy numbers exactly as the node reports state them; never recompute, add up or "
    "extrapolate. Mention n-1/n-8 changes only where a node report states them. Do not "
    "repeat appendices; refer to them by node."
)


def _notification_instructions(config: dict) -> str:
    """Ask for the machine-readable priority table only when a notification mode is enabled."""
    from . import notifications
    return (" " + notifications.TABLE_INSTRUCTIONS) if notifications.enabled(config) else ""


def _run_notifications(config: dict, log_date: str, store) -> dict | None:
    """P0 -> Jira / P1 -> Slack after the cluster reports exist. Never fails the report stage."""
    from . import notifications
    if not notifications.enabled(config) or not config.get("notify_after_report", True):
        return None
    try:
        result = notifications.run(config, log_date, store=store)
    except Exception as exc:
        logger.exception("Notifications failed")
        return {"status": "failed", "error": str(exc)[:500]}
    return {k: result.get(k) for k in ("status", "environment", "jira_mode", "slack_mode", "uri")}


def _cluster_summary_source(config: dict) -> str:
    value = str(config.get("cluster_summary_source") or "extract").strip().lower().replace("-", "_")
    if value not in CLUSTER_SUMMARY_SOURCES:
        logger.warning("Unknown cluster_summary_source=%r; using 'extract'", value)
        return "extract"
    return value


def _finish_cluster_summary(llm, system: str, user: str, trend_md: str | None = None) -> str:
    """Shared generation, executive-summary retry and customer-safety validation."""
    from . import trend_alerts
    summary = llm.generate(system, user)
    if not re.search(r"(?im)^#{1,2}\s*Executive summary\b", summary[:1200]):
        summary = llm.generate(
            system + " REQUIRE the heading ## Executive summary at the top. "
            "Keep all conclusions grounded in the same supplied evidence.", user)
    if not re.search(r"(?im)^#{1,2}\s*Executive summary\b", summary[:1200]):
        raise ValueError("Cluster summary omitted its required executive summary")
    if trend_md:
        summary = trend_alerts.place_section(summary, trend_md)
    summary, removed_internal = _strip_internal_content(summary)
    if removed_internal:
        logger.info("Removed %d internal-only content block(s) from cluster summary", removed_internal)
    _validate_customer_summary(summary)
    return summary


def cluster_summary_from_node_reports(store, layout, llm, config: dict, log_date: str, results) -> str:
    """Alternative cluster summary whose only input is the stored node report.md files.

    Written next to cluster-summary.md so both versions can be compared; no extract, trend or
    query-stats file is read.
    """
    body = "\n\n".join(_doc("node_report", {"node": r["node"], "log": r["log_name"]},
                            _without_embedded_images(r["report_md"])) for r in results)
    user = (f"Cluster `{config['cluster_name']}`, day {log_date}. Inputs: {len(results)} node reports "
            "(no extracts).\n\n" + body)
    summary = _finish_cluster_summary(llm, CLUSTER_PROMPT + " " + NODE_REPORTS_ONLY_INSTRUCTIONS
                                      + _notification_instructions(config), user)
    header = (f"<!-- cluster summary variant: node_reports (built only from {len(results)} node report.md "
              "files; compare with cluster-summary.md, which also uses extract-based trend alerts and "
              "query-shape evidence) -->\n")
    uri = store.put_text(layout.cluster_report(log_date, NODE_REPORTS_SUMMARY_FILE),
                         header + summary, "text/markdown; charset=utf-8")
    logger.info("Cluster summary (node_reports) nodes=%d chars=%d uri=%s", len(results), len(summary), uri)
    return uri


def _trend_alerts(store, layout, log_date: str, nodes, config) -> dict | None:
    """Per-node comparison with n-1 and n-8 (only nodes with both baselines are compared)."""
    from . import trend_alerts
    per_node = []
    for host, name in nodes:
        current, _ = _load_extract(store, layout, log_date, host, name, "extractionshort.json")
        if current is None:
            current, _ = _load_extract(store, layout, log_date, host, name, "extractionOccurence.json")
        if current is None:
            continue
        baselines = {}
        for label, offset, _desc in BASELINES:
            bdate = shift(log_date, offset)
            payload, _ = _load_extract(store, layout, bdate, host, name, "extractionshort.json")
            baselines[label] = (bdate, payload)
        per_node.append(trend_alerts.node_alerts(host, name, current, baselines, config))
    return trend_alerts.cluster_alerts(per_node, config) if per_node else None


def write_fallback_report(store, layout, log_date, host_dir, log_name, error, config=None) -> dict:
    """Always leave one report per node: deterministic content when the model-based report failed."""
    config = config or {}
    lines = [f"# MongoDB Log Diagnostic — node `{host_dir}` ({log_name} log), {log_date}", "",
             "> **Partial report.** The model-based report could not be generated for this node "
             f"(`{type(error).__name__}`: {str(error)[:300]}). The sections below are produced "
             "deterministically from the extract and metrics; rerun `--stage report` after fixing the cause.", ""]
    current, _ = _load_extract(store, layout, log_date, host_dir, log_name, "extractionOccurence.json")
    metrics_payload = None
    analysis = None
    try:
        metrics_payload = _load_metrics(store, layout, log_date, host_dir)
        if metrics_payload is not None and current is not None and config.get("resource_investigation_enabled", True):
            from . import resource_investigation as ri
            analysis = ri.analyze(metrics_payload, current, config)
            lines += [ri.render_markdown(analysis), ""]
    except Exception as exc:  # fallback must never fail
        logger.warning("Fallback resource investigation failed node=%s error_type=%s", host_dir, type(exc).__name__)
    report_md = "\n".join(lines)
    try:
        if current is not None:
            from .report_shape_mapping import append_reported_shapes
            stats_key = layout.query_stats(log_date, host_dir)
            report_md = append_reported_shapes(
                report_md, current, store.get_text(stats_key) if store.exists(stats_key) else None)
        report_md = _add_appendices(report_md, store, layout, log_date, host_dir, metrics_payload, config)
    except Exception as exc:
        logger.warning("Fallback appendix failed node=%s error_type=%s", host_dir, type(exc).__name__)
    uri = store.put_text(layout.report(log_date, host_dir, log_name, "report.md"),
                         report_md, "text/markdown; charset=utf-8")
    logger.warning("Wrote partial fallback report node=%s uri=%s", host_dir, uri)
    return {"node": host_dir, "log_name": log_name, "current_file": "extractionOccurence.json (fallback)",
            "baselines": [], "partial": True, "error": str(error)[:1000],
            "resource_investigation": analysis["triggered"] if analysis else None,
            "uploads": {"report": uri}, "report_md": report_md}


def run(config: dict, log_date: str, store=None, llm=None, summary_only: bool = False) -> dict:
    from .providers import get_llm, get_store
    layout = Layout.from_config(config)
    store = store or get_store(config)
    llm = llm or get_llm(config)
    nodes = discover_nodes(store, layout, log_date)
    if not nodes:
        raise RuntimeError(f"No extractions found for {log_date}")
    expected = int(config.get("expected_node_count", 3))
    logger.info("Report nodes for %s: %s", log_date, [f"{h}/{n}" for h, n in nodes])
    if len(nodes) != expected:
        logger.warning("Found %d node extracts for %s (expected %d); node folders without "
                       "extractionOccurence.json: %s", len(nodes), log_date, expected,
                       hosts_without_extract(store, layout, log_date, nodes) or "none")
    results, failed = [], []
    if summary_only:
        if len(nodes) < 2:
            raise RuntimeError("Cluster summary requires at least two node extracts")
        missing = [f"{host}/{name}" for host, name in nodes
                   if not store.exists(layout.report(log_date, host, name, "report.md"))]
        if missing:
            raise RuntimeError("Missing existing node reports: " + ", ".join(missing) +
                               "; run --stage report first")
        for host, name in nodes:
            key = layout.report(log_date, host, name, "report.md")
            results.append({"node": host, "log_name": name, "report_md": store.get_text(key),
                            "uploads": {"report": store.uri(key)}})
    else:
        # None -> per-node default: 60k chars, 90k when resource investigation runs for the node.
        max_chars = config.get("report_max_input_chars")
        system_prompt = build_system_prompt()
        for host, name in nodes:
            try:
                results.append(report_node(store, layout, llm, system_prompt, log_date,
                                           host, name, max_chars, config=config))
            except Exception as error:
                logger.exception("Report failed for %s/%s", host, name)
                failed.append({"node": host, "log_name": name, "error": str(error)[:1000]})
                try:
                    results.append(write_fallback_report(store, layout, log_date, host, name, error, config))
                except Exception:
                    logger.exception("Fallback report failed for %s/%s", host, name)
    cluster_uri, trend_uri, node_reports_uri = None, None, None
    source = _cluster_summary_source(config)
    want_summary = bool(results) and config.get("cluster_summary", True) and len(results) > 1
    if want_summary and source in ("extract", "both"):
        evidence = summarize_query_stats(store, layout, log_date, nodes)
        logger.info("Cluster summary inputs nodes=%d query_stats=%s summary_only=%s",
                    len(nodes), [(x["node"], x["status"], x.get("shapes_found")) for x in evidence],
                    summary_only)
        body = "\n\n".join(_doc("node_report", {"node": r["node"], "log": r["log_name"]},
                                _without_embedded_images(r["report_md"])) for r in results)
        from . import trend_alerts
        alerts, trend_md = None, None
        try:
            alerts = _trend_alerts(store, layout, log_date, [(r["node"], r["log_name"]) for r in results], config)
            trend_md = trend_alerts.render_markdown(alerts) if alerts else None
        except Exception as exc:  # trends must never block the cluster summary
            logger.warning("Trend comparison failed error_type=%s", type(exc).__name__)
        if alerts:
            trend_uri = store.put_text(layout.cluster_report(log_date, "trend-alerts.json"),
                                       json.dumps(alerts, indent=2, default=str), "application/json")
            logger.info("Trend alerts status=%s nodes_compared=%s items=%d uri=%s", alerts["status"],
                        alerts["nodes_compared"], sum(len(n.get("items", [])) for n in alerts["nodes"]), trend_uri)
        system = CLUSTER_PROMPT + _notification_instructions(config)
        user = (f"Cluster `{config['cluster_name']}`, day {log_date}.\n\n"
                + _doc("query_shape_insights", {"date": log_date}, json.dumps(evidence)))
        if trend_md:
            system += " " + TREND_INSTRUCTIONS.format(pct=alerts["thresholds"]["increase_pct"])
            user += "\n\n" + _doc("trend_alerts", {"date": log_date, "baselines": "n-1,n-8"},
                                  json.dumps(trend_alerts.model_view(alerts), default=str))
        user += "\n\n" + body
        summary = _finish_cluster_summary(llm, system, user, trend_md)
        cluster_uri = store.put_text(layout.cluster_report(log_date, "cluster-summary.md"),
                                     summary, "text/markdown; charset=utf-8")
    if want_summary and source in ("node_reports", "both"):
        try:
            node_reports_uri = cluster_summary_from_node_reports(store, layout, llm, config, log_date, results)
        except Exception as exc:
            if source == "node_reports":
                raise
            logger.warning("Node-report cluster summary failed error_type=%s", type(exc).__name__)
    catalog = offline_cve.load_catalog()
    manifest = {
        "cluster": config["cluster_name"], "log_date": log_date, "cloud": config.get("cloud"),
        "storage": store.uri(layout.day(log_date)),
        "cluster_reports": store.uri(layout.cluster_report(log_date, "")),
        "llm_provider": config.get("llm_provider"),
        "offline_driver_cve_catalog": {k: catalog.get(k) for k in
                                       ("catalog_name", "catalog_version", "refreshed_at", "coverage_note")},
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "node_count": len(nodes), "expected_node_count": expected,
        "reports": [{k: v for k, v in r.items() if k != "report_md"} for r in results],
        "cluster_summary": cluster_uri, "cluster_summary_source": source,
        "cluster_summary_from_node_reports": node_reports_uri, "failed": failed,
        "trend_alerts": trend_uri,
    }
    store.put_text(layout.cluster_report(log_date, "manifest.json"),
                   json.dumps(manifest, indent=2), "application/json")
    if cluster_uri or node_reports_uri:
        notified = _run_notifications(config, log_date, store)
        if notified is not None:
            manifest["notifications"] = notified
            store.put_text(layout.cluster_report(log_date, "manifest.json"),
                           json.dumps(manifest, indent=2), "application/json")
    if summary_only and not (cluster_uri or node_reports_uri):
        raise RuntimeError("Cluster summary generation disabled or failed")
    if not results:
        raise RuntimeError(json.dumps(manifest))
    return manifest
