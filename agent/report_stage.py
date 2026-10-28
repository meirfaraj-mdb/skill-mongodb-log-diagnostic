"""Build node reports, compare baselines, and summarize customer-impacting cluster issues."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from . import diffing, offline_cve
from .common import Layout, logger, shift
from .skills import diagnostic_skill_dir

BASELINES = (("n-1", -1, "yesterday"), ("n-8", -7, "one week ago"))


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
        "You are running inside an automated pipeline. Follow the MongoDB Log Diagnostic skill exactly.",
        "Adapter note: the analysis prompt refers to `/home/user/extraction.json`; extraction files are "
        "supplied inline in labelled <document> tags. `current` is extractionOccurence.json; `n-1` and "
        "`n-8` are extractionshort.json baselines. Use exact numbers from `precomputed_diff`. "
        "You have no web access. Use only reviewed offline_driver_cves matches as version-range review "
        "candidates with unverified application exposure; include catalog refreshed_at. If status is "
        "no_match_in_snapshot say so without claiming safety. Never invent CVEs. Output only Markdown.",
        "=== references/analysis-prompt.md ===\n" + _read_skill_text("references/analysis-prompt.md"),
        "=== references/extracted-signal-reference.md ===\n" + _read_skill_text("references/extracted-signal-reference.md"),
        "=== references/analysis-prompt.offline-cve-overlay.md ===\n" + _read_skill_text("references/analysis-prompt.offline-cve-overlay.md"),
    ])


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


def _validate_offline_cves(markdown: str, allowed: set[str]) -> None:
    mentioned = {c.upper() for c in re.findall(r"\bCVE-\d{4}-\d{4,}\b", markdown, re.I)}
    extra = mentioned - {c.upper() for c in allowed}
    if extra:
        raise ValueError("Report contains CVE IDs not in reviewed offline matches: " + ", ".join(sorted(extra)))


def _metrics_appendix(store, layout, log_date: str, host_dir: str, log_name: str) -> tuple[str, dict]:
    """Render this node's stored metrics.json as one A4 page and return (markdown body, uploads).

    Failures never fail the report: the appendix states why the graph is unavailable.
    """
    from . import metrics_graphs

    key = layout.observability(log_date, host_dir, "metrics")
    if not store.exists(key):
        return ("No per-node metrics file was supplied for this node "
                "(enable observability metrics collection to populate this appendix)."), {}
    try:
        payload = json.loads(store.get_text(key))
    except (TypeError, ValueError):
        return "The per-node metrics file could not be parsed; no graph was drawn.", {}
    panels, empty = metrics_graphs.plan_panels(metrics_graphs.load_series(payload))
    if not panels:
        return metrics_graphs.appendix_markdown(None, [], empty, payload,
                                                unavailable_reason="no selected metric group has data"), {}
    png_name, svg_name = "metrics-appendix.png", "metrics-appendix.svg"
    window = payload.get("window", {}) if isinstance(payload, dict) else {}
    subtitle = " · ".join(x for x in (
        f"{str(window.get('since', ''))[:16].replace('T', ' ')} → {str(window.get('until', ''))[:16].replace('T', ' ')} UTC"
        if window else "", f"granularity {window['granularity']}" if window.get("granularity") else "",
        "solid = left axis, dashed = right axis") if x)
    uploads = {}
    try:
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory(prefix="metrics-graph-") as tmp:
            png, svg = str(Path(tmp) / png_name), str(Path(tmp) / svg_name)
            metrics_graphs.render_one_page(panels, f"Node {host_dir} — metrics", png, svg, subtitle=subtitle)
            uploads["metrics_graph"] = store.upload(png, layout.report(log_date, host_dir, log_name, png_name), "image/png")
            uploads["metrics_graph_svg"] = store.upload(svg, layout.report(log_date, host_dir, log_name, svg_name),
                                                        "image/svg+xml")
    except ImportError:
        return metrics_graphs.appendix_markdown(None, panels, empty, payload,
                                                unavailable_reason="matplotlib is not installed in this runtime"), {}
    except Exception as exc:  # never let a graph break the report
        logger.warning("Metrics graph failed node=%s error_type=%s", host_dir, type(exc).__name__)
        return metrics_graphs.appendix_markdown(None, panels, empty, payload,
                                                unavailable_reason="graph rendering or upload failed"), {}
    return metrics_graphs.appendix_markdown(png_name, panels, empty, payload, svg_name), uploads


def report_node(store, layout, llm, system_prompt, log_date, host_dir, log_name, max_chars,
                config: dict | None = None) -> dict:
    current, cur_key = _load_extract(store, layout, log_date, host_dir, log_name, "extractionOccurence.json")
    if current is None:
        raise FileNotFoundError(cur_key)
    current_file = "extractionOccurence.json"
    offline_cves = offline_cve.check(current)
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
        for label, key in (("index_stats", layout.index_stats(log_date, host_dir)),
                           ("query_stats", layout.query_stats(log_date, host_dir))):
            if store.exists(key):
                docs.append(_doc(label, {"node": host_dir}, store.get_text(key)))
        header = (f"Generate the MongoDB Log Diagnostic report for node `{host_dir}` ({log_name} log), "
                  f"log day {log_date} (Atlas project timezone day). Baselines supplied: "
                  f"{', '.join(d['label'] for d in diffs) or 'none -- omit historical-comparison claims'}.")
        return header + "\n\n" + "\n\n".join(docs)

    user = assemble(current, current_file, baselines)
    if len(user) > max_chars:
        short, _ = _load_extract(store, layout, log_date, host_dir, log_name, "extractionshort.json")
        current_file = "extractionshort.json"
        user = assemble(short or current, current_file, baselines)
    if len(user) > max_chars:
        current_file = "extractionshort.json (compacted)"
        user = assemble(_compact(short or current), current_file,
                        [(l, d, s, _compact(p)) for l, d, s, p in baselines])
    if len(user) > max_chars and baselines:
        logger.warning("Report input node=%s still %d chars after compaction (limit %d); dropping baselines",
                       host_dir, len(user), max_chars)
        current_file = "extractionshort.json (compacted, baselines dropped)"
        baselines = []
        user = assemble(_compact(short or current), current_file, [])
    if len(user) > max_chars:
        raise RuntimeError(f"Report input {len(user)} chars exceeds REPORT_MAX_INPUT_CHARS={max_chars} "
                           f"after compaction; reduce inputs for {host_dir}")
    logger.info("Report input node=%s chars=%d current_file=%s baselines=%s",
                host_dir, len(user), current_file, [b[0] for b in baselines])
    report_md = llm.generate(system_prompt, user)
    from .report_shape_mapping import append_reported_shapes
    stats_key = layout.query_stats(log_date, host_dir)
    report_md = append_reported_shapes(
        report_md, current, store.get_text(stats_key) if store.exists(stats_key) else None)
    graph_uploads = {}
    if (config or {}).get("metrics_graphs_enabled", True):
        from .report_shape_mapping import append_metrics_appendix
        metrics_md, graph_uploads = _metrics_appendix(store, layout, log_date, host_dir, log_name)
        report_md = append_metrics_appendix(report_md, metrics_md)
    report_md, removed_internal = _strip_internal_content(report_md)
    if removed_internal:
        logger.info("Removed %d internal-only content block(s) from node report", removed_internal)
    _validate_offline_cves(report_md, {m["cve"] for m in offline_cves["matches"]})
    uploads = {"report": store.put_text(layout.report(log_date, host_dir, log_name, "report.md"),
                                        report_md, "text/markdown; charset=utf-8"), **graph_uploads}
    if diffs:
        uploads["diff"] = store.put_text(layout.report(log_date, host_dir, log_name, "diff.json"),
                                         json.dumps(diff_doc, indent=2), "application/json")
    return {"node": host_dir, "log_name": log_name, "current_file": current_file,
            "baselines": [b[0] for b in baselines], "offline_driver_cves": offline_cves,
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


def summarize_query_stats(store, layout, log_date, nodes, limit=15):
    output = []
    for host in sorted({host for host, _ in nodes}):
        key = layout.query_stats(log_date, host)
        index_available = store.exists(layout.index_stats(log_date, host))
        if not store.exists(key):
            output.append({"node": host, "status": "not_collected", "index_stats_available": index_available})
            continue
        try:
            data = json.loads(store.get_text(key))
            shapes = [_shape(r) for r in _shape_records(data)]
            shapes = [s for s in shapes if s]
            shapes.sort(key=lambda s: (_metric_number(s.get("docsExaminedRatio")),
                                       _metric_number(s.get("avgWorkingMillis")),
                                       _metric_number(s.get("execCount"))), reverse=True)
            output.append({"node": host, "status": "available", "index_stats_available": index_available,
                           "shapes_found": len(shapes), "top_shapes": shapes[:limit]})
        except (ValueError, TypeError) as exc:
            output.append({"node": host, "status": "invalid_query_stats",
                           "index_stats_available": index_available, "error_type": type(exc).__name__})
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
    "present. Do not fill a quota of actions. Never expose credentials, IPs or query literals."
)


def run(config: dict, log_date: str, store=None, llm=None, summary_only: bool = False) -> dict:
    from .providers import get_llm, get_store
    layout = Layout.from_config(config)
    store = store or get_store(config)
    llm = llm or get_llm(config)
    nodes = discover_nodes(store, layout, log_date)
    if not nodes:
        raise RuntimeError(f"No extractions found for {log_date}")
    expected = int(config.get("expected_node_count", 3))
    if len(nodes) != expected:
        logger.warning("Found %d node extracts for %s (expected %d)", len(nodes), log_date, expected)
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
        max_chars = int(config.get("report_max_input_chars", 600_000))
        system_prompt = build_system_prompt()
        for host, name in nodes:
            try:
                results.append(report_node(store, layout, llm, system_prompt, log_date,
                                           host, name, max_chars, config=config))
            except Exception as error:
                logger.exception("Report failed for %s/%s", host, name)
                failed.append({"node": host, "log_name": name, "error": str(error)[:1000]})
    cluster_uri = None
    if results and config.get("cluster_summary", True) and len(results) > 1:
        evidence = summarize_query_stats(store, layout, log_date, nodes)
        logger.info("Cluster summary inputs nodes=%d query_stats=%s summary_only=%s",
                    len(nodes), [(x["node"], x["status"], x.get("shapes_found")) for x in evidence],
                    summary_only)
        body = "\n\n".join(_doc("node_report", {"node": r["node"], "log": r["log_name"]},
                                r["report_md"]) for r in results)
        user = (f"Cluster `{config['cluster_name']}`, day {log_date}.\n\n"
                + _doc("query_shape_insights", {"date": log_date}, json.dumps(evidence))
                + "\n\n" + body)
        summary = llm.generate(CLUSTER_PROMPT, user)
        if not re.search(r"(?im)^#{1,2}\s*Executive summary\b", summary[:1200]):
            summary = llm.generate(
                CLUSTER_PROMPT + " REQUIRE the heading ## Executive summary at the top. "
                "Keep all conclusions grounded in the same supplied evidence.", user)
        if not re.search(r"(?im)^#{1,2}\s*Executive summary\b", summary[:1200]):
            raise ValueError("Cluster summary omitted its required executive summary")
        summary, removed_internal = _strip_internal_content(summary)
        if removed_internal:
            logger.info("Removed %d internal-only content block(s) from cluster summary", removed_internal)
        _validate_customer_summary(summary)
        cluster_uri = store.put_text(layout.cluster_report(log_date, "cluster-summary.md"),
                                     summary, "text/markdown; charset=utf-8")
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
        "cluster_summary": cluster_uri, "failed": failed,
    }
    store.put_text(layout.cluster_report(log_date, "manifest.json"),
                   json.dumps(manifest, indent=2), "application/json")
    if summary_only and not cluster_uri:
        raise RuntimeError("Cluster summary generation disabled or failed")
    if not results:
        raise RuntimeError(json.dumps(manifest))
    return manifest
