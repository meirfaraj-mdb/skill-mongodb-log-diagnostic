"""Stage 3 -- build per-node diagnostic reports (with n-1 / n-8 diff) and upload.

Inputs per node (host + log_name):
  current  = D-1 extraction   (extractionOccurence.json, authoritative)
  n-1      = D-2 extraction   (extractionshort.json)   -- only if it exists
  n-8      = D-8 extraction   (extractionshort.json)   -- only if it exists
Prompt   = references/analysis-prompt.md + references/extracted-signal-reference.md
           (+ SKILL.md report rules), exactly as the skill requires.
Output   = <prefix>/<D-1>/<host>/reports/<log_name>/report.md
           <prefix>/<D-1>/<host>/reports/<log_name>/diff.json
           <prefix>/<D-1>/cluster/reports/cluster-summary.md   (optional)
           <prefix>/<D-1>/cluster/reports/manifest.json
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import diffing, offline_cve
from .common import Layout, logger, shift
from .skills import diagnostic_skill_dir

BASELINES = (("n-1", -1, "yesterday"), ("n-8", -7, "one week ago"))


def _read_skill_text(rel: str) -> str:
    return (diagnostic_skill_dir() / rel).read_text(encoding="utf-8")


def build_system_prompt() -> str:
    return "\n\n".join([
        "You are running inside an automated pipeline. Follow the MongoDB Log Diagnostic skill exactly.",
        "Adapter note: the analysis prompt refers to `/home/user/extraction.json`; in this pipeline the "
        "extraction files are supplied inline in the user message, each inside a labelled <document> tag. "
        "The document labelled `current` is the authoritative extractionOccurence.json. Baselines are labelled "
        "`n-1` (yesterday) and `n-8` (one week ago) and are extractionshort.json files. A `precomputed_diff` "
        "document gives deterministic comparisons; use its exact numbers for the historical comparison table. "
        "You have no web access in this run. The `offline_driver_cves` document is the only permitted CVE source: "
        "use matching entries as confirmed offline-snapshot findings and include catalog `refreshed_at`; if its status "
        "is `no_match_in_snapshot`, say that exactly, without claiming the driver is safe or a live lookup occurred. "
        "Do not browse or invent CVEs. Output ONLY the final Markdown report.",
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
        for k in path[:-1]:
            node = node.get(k, {})
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


def report_node(store, layout, llm, system_prompt, log_date, host_dir, log_name, max_chars) -> dict:
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
            docs.append(_doc(label, {"date": bdate, "meaning": desc, "node": host_dir, "file": "extractionshort.json"},
                             json.dumps(payload, separators=(",", ":"))))
        if diffs:
            docs.append(_doc("precomputed_diff", {}, json.dumps(diff_doc, separators=(",", ":"))))
        docs.append(_doc("offline_driver_cves", {}, json.dumps(offline_cves, separators=(",", ":"))))
        for label, key in (("index_stats", layout.index_stats(log_date, host_dir)), ("query_stats", layout.query_stats(log_date, host_dir))):
            if store.exists(key):
                docs.append(_doc(label, {"node": host_dir}, store.get_text(key)))
        header = (f"Generate the MongoDB Log Diagnostic report for node `{host_dir}` ({log_name} log), "
                  f"log day {log_date} (Atlas project timezone day). Baselines supplied: "
                  f"{', '.join(d['label'] for d in diffs) or 'none -- omit historical-comparison claims'}.")
        return header + "\n\n" + "\n\n".join(docs)

    # Size ladder: full occurrence file -> short file -> compacted short + compacted baselines.
    user = assemble(current, current_file, baselines)
    if len(user) > max_chars:
        short, _ = _load_extract(store, layout, log_date, host_dir, log_name, "extractionshort.json")
        current_file = "extractionshort.json"
        user = assemble(short or current, current_file, baselines)
        if len(user) > max_chars:
            current_file = "extractionshort.json (compacted)"
            user = assemble(_compact(short or current), current_file,
                            [(l, d, s, _compact(p)) for l, d, s, p in baselines])
    logger.info("Report input node=%s chars=%d current_file=%s baselines=%s",
                host_dir, len(user), current_file, [b[0] for b in baselines])

    report_md = llm.generate(system_prompt, user)
    uploads = {"report": store.put_text(layout.report(log_date, host_dir, log_name, "report.md"), report_md, "text/markdown; charset=utf-8")}
    if diffs:
        uploads["diff"] = store.put_text(layout.report(log_date, host_dir, log_name, "diff.json"),
                                         json.dumps(diff_doc, indent=2), "application/json")
    return {"node": host_dir, "log_name": log_name, "current_file": current_file,
            "baselines": [b[0] for b in baselines], "offline_driver_cves": offline_cves,
            "uploads": uploads, "report_md": report_md}


CLUSTER_PROMPT = (
    "You are given per-node MongoDB Log Diagnostic reports for the nodes of one Atlas cluster for the same day. "
    "Write a concise cluster-level Markdown summary: overall health, cross-node patterns (same fingerprint or "
    "query shape on several nodes, primary vs secondaries differences, elections/stepdowns), the n-1/n-8 trend "
    "per node when present, and the top 5 prioritized actions with the node(s) they apply to. Use only facts "
    "stated in the node reports; do not invent numbers. Never expose credentials, IPs, or query literals."
)


def run(config: dict, log_date: str, store=None, llm=None) -> dict:
    from .providers import get_llm, get_store
    layout = Layout.from_config(config)
    store = store or get_store(config)
    llm = llm or get_llm(config)
    max_chars = int(config.get("report_max_input_chars", 600_000))
    system_prompt = build_system_prompt()

    nodes = discover_nodes(store, layout, log_date)
    if not nodes:
        raise RuntimeError(f"No extractions found for {log_date}")
    expected = int(config.get("expected_node_count", 3))
    if len(nodes) != expected:
        logger.warning("Found %d node extracts for %s (expected %d)", len(nodes), log_date, expected)

    results, failed = [], []
    for host_dir, log_name in nodes:
        try:
            results.append(report_node(store, layout, llm, system_prompt, log_date, host_dir, log_name, max_chars))
        except Exception as error:
            logger.exception("Report failed for %s/%s", host_dir, log_name)
            failed.append({"node": host_dir, "log_name": log_name, "error": str(error)[:1000]})

    cluster_uri = None
    if results and config.get("cluster_summary", True) and len(results) > 1:
        body = "\n\n".join(_doc("node_report", {"node": r["node"], "log": r["log_name"]}, r["report_md"]) for r in results)
        summary = llm.generate(CLUSTER_PROMPT, f"Cluster `{config['cluster_name']}`, day {log_date}.\n\n{body}")
        cluster_uri = store.put_text(layout.cluster_report(log_date, "cluster-summary.md"), summary, "text/markdown; charset=utf-8")

    catalog = offline_cve.load_catalog()
    manifest = {
        "cluster": config["cluster_name"], "log_date": log_date, "cloud": config.get("cloud"),
        "storage": store.uri(layout.day(log_date)), "cluster_reports": store.uri(layout.cluster_report(log_date, "")), "llm_provider": config.get("llm_provider"),
        "offline_driver_cve_catalog": {k: catalog.get(k) for k in ("catalog_name", "catalog_version", "refreshed_at", "coverage_note")},
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "node_count": len(nodes), "expected_node_count": expected,
        "reports": [{k: v for k, v in r.items() if k != "report_md"} for r in results],
        "cluster_summary": cluster_uri, "failed": failed,
    }
    store.put_text(layout.cluster_report(log_date, "manifest.json"), json.dumps(manifest, indent=2), "application/json")
    if not results:
        raise RuntimeError(json.dumps(manifest))
    return manifest
