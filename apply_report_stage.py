"""Apply conservative report policy to the existing agent/report_stage.py; safe to re-run."""
from pathlib import Path
import ast
p = Path('agent/report_stage.py')
s = p.read_text(encoding='utf-8')
needle = '    return "\\n\\n".join([\n'
policy = '''        "CVE and application-attribution safety rules OVERRIDE conflicting text in the base skill: "
        "The offline_driver_cves document is a limited reviewed snapshot, not a web lookup. A matching "
        "driver/version is a review candidate, never proof of exploitation or vulnerable component use. "
        "Only name applications tied to the same driver/version in observed_app_names, or the same slow "
        "group in app_names; distinguish internal Atlas services from customer apps. For topology and "
        "TLS errors with no app mapping state attribution unknown. Show an issue-to-application matrix and "
        "include app attribution in driver warnings. Never fabricate CVEs, upgrades, application impact, "
        "or a universal server-driver compatibility requirement. Do not advise customer upgrades of mongot. "
        "The reviewed offline overlay has precedence over base analysis-prompt.md. "
        "An offline range match is not a confirmed vulnerability in the connected application.",
'''
if 'CVE and application-attribution safety rules OVERRIDE' not in s:
    if needle not in s:
        raise SystemExit('Expected build_system_prompt list not found; no changes made')
    s = s.replace(needle, needle + policy, 1)
cluster_needle = 'CLUSTER_PROMPT = (\n'
cluster_policy = '''    "For each prioritized finding include observed app names ONLY if linked to that issue in "
    "the node evidence; otherwise write Not attributable from logs. Do not label internal Atlas "
    "clients as customer applications. Offline CVE version matches require component-use validation; "
    "do not promote a node report's speculative CVE or compatibility claim to a confirmed "
    "cluster-wide fact. Include catalog date and source when discussing CVEs. "
'''
if 'For each prioritized finding include observed app names ONLY' not in s:
    if cluster_needle not in s:
        raise SystemExit('Expected CLUSTER_PROMPT not found; no changes made')
    s = s.replace(cluster_needle, cluster_needle + cluster_policy, 1)
# A model can ignore instructions: reject an unreviewed CVE before uploading.
validator = 'def _validate_offline_cves(markdown: str, allowed: set[str]) -> None:\n    import re\n    mentioned = set(re.findall(r"\\bCVE-\\d{4}-\\d{4,}\\b", markdown, re.I))\n    extra = {c.upper() for c in mentioned} - {c.upper() for c in allowed}\n    if extra:\n        raise ValueError("Report contains CVE IDs not in reviewed offline matches: " + ", ".join(sorted(extra)))\n\n'
if 'def _validate_offline_cves(' not in s:
    anchor = 'def report_node('
    if anchor not in s:
        raise SystemExit('Expected report_node not found; no changes made')
    s = s.replace(anchor, validator + anchor, 1)
if '_validate_offline_cves(report_md,' not in s:
    anchor = '    report_md = llm.generate(system_prompt, user)'
    if anchor not in s:
        raise SystemExit('Expected node report generation not found; no changes made')
    s = s.replace(anchor, anchor + '\n    _validate_offline_cves(report_md, {m["cve"] for m in offline_cves["matches"]})', 1)
if '_validate_offline_cves(summary,' not in s:
    anchor = '        cluster_uri = store.put_text('
    if anchor not in s:
        raise SystemExit('Expected cluster summary upload not found; no changes made')
    s = s.replace(anchor, '        _validate_offline_cves(summary, {m["cve"] for r in results for m in r["offline_driver_cves"]["matches"]})\n' + anchor, 1)
s = s.replace('use matching entries as confirmed offline-snapshot findings', 'use matching entries only as version-range review candidates with unverified application exposure')
ast.parse(s, filename=str(p))
p.write_text(s, encoding='utf-8')
print('Updated', p)
