#!/usr/bin/env python3
"""Refresh the offline MongoDB *server* CVE catalog from NVD.

Usage (from any directory):
    python scripts/update_offline_mongodb_server_cves.py [--dry-run]
    python scripts/update_offline_mongodb_server_cves.py --input-json nvd-page.json [--input-json ...]

Online mode needs outbound HTTPS to services.nvd.nist.gov (no third-party Python modules). Set
NVD_API_KEY to raise the NVD rate limit. ``--input-json`` builds the catalog from saved NVD API 2.0
responses (air-gapped refresh). Source query (exact product, no keyword search):

    https://services.nvd.nist.gov/rest/json/cves/2.0?virtualMatchString=cpe:2.3:a:mongodb:mongodb

Only ``cpe:2.3:a:mongodb:mongodb:*`` matches marked ``vulnerable: true`` are used (MongoDB Server).
NVD ranges map to OSV-style intervals so agent/offline_cve.py can reuse one matcher:
versionStartIncluding -> introduced (inclusive), versionStartExcluding -> introduced +
``introduced_exclusive``, versionEndExcluding -> fixed (exclusive), versionEndIncluding ->
last_affected (inclusive). A CPE with a fixed version is an exact ``versions`` entry; a pre-release
update field (rc1, alpha) is kept as "4.0.0-rc1" and only matches that exact build. A wildcard CPE
with no bound (all versions) is not representable and is reported, never matched. A CPE ANDed with
an OS platform (e.g. Windows only) keeps a ``platform_note``. When NVD has not analysed a CVE yet
(no CPE configuration), the CNA ``affected`` block for "MongoDB Server" is used and the entry
records ``range_source: "cna"``. The catalog is rebuilt from the source on every run.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG = REPO_ROOT / 'skills/mongodb-log-diagnostic/references/offline-mongodb-server-cves.json'
SCHEMA = 'mongodb-log-diagnostic.offline-mongodb-server-cves/v1'
API = 'https://services.nvd.nist.gov/rest/json/cves/2.0'
CPE_PREFIX = 'cpe:2.3:a:mongodb:mongodb:'
VIRTUAL_MATCH = 'cpe:2.3:a:mongodb:mongodb'
PAGE_SIZE = 2000                       # NVD API 2.0 maximum
SLEEP_WITHOUT_KEY_S = 6.5              # NVD public limit: 5 requests / 30 s
SLEEP_WITH_KEY_S = 0.7
VERSION = re.compile(r'^v?(\d+(?:\.\d+){0,3})((?:[-+.~]?[A-Za-z][0-9A-Za-z.\-+~]*)?)$')
CNA_PRODUCT = re.compile(r'^\s*mongodb[ _]server\s*$', re.I)
SEVERITY_ORDER = ('cvssMetricV31', 'cvssMetricV40', 'cvssMetricV30', 'cvssMetricV2')
SCOPE_NOTE = ('Version-range candidate only. Verify the advisory, the affected component/feature and the '
              'deployment (edition, platform, configuration) before calling the node vulnerable.')
COVERAGE_NOTE = ('Offline NVD snapshot for the MongoDB Server CPE (cpe:2.3:a:mongodb:mongodb). '
                 'Version-range matches are reported. '
                 'Absence of a match is not evidence of safety; a match does not establish exploitability. '
                 'Atlas applies server patch releases on its maintenance schedule: verify the node version.')


def _json_request(url: str, timeout: int = 30) -> dict:
    if not url.startswith(API + '?'):
        raise ValueError('Unexpected API URL')
    headers = {'User-Agent': 'mongodb-server-cve-refresh/1', 'Accept': 'application/json'}
    if os.environ.get('NVD_API_KEY'):
        headers['apiKey'] = os.environ['NVD_API_KEY']
    request = urllib.request.Request(url, headers=headers, method='GET')
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _norm(value) -> tuple[str, tuple[int, ...], str] | None:
    """('4.0.0-rc1', (4, 0, 0), 'rc1') / ('7.0.12', (7, 0, 12), ''); None when unparsable."""
    match = VERSION.fullmatch(str(value or '').strip())
    if not match:
        return None
    numeric = match.group(1)
    suffix = match.group(2).lstrip('-+.~')
    return (numeric + ('-' + suffix if suffix else '')), tuple(map(int, numeric.split('.'))), suffix


def _lt(a: tuple[int, ...], b: tuple[int, ...]) -> bool:
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) < b + (0,) * (n - len(b))


def _cpe_parts(criteria: str) -> tuple[str, str]:
    """(version, update) components of a CPE 2.3 string."""
    parts = criteria.split(':')
    return (parts[5] if len(parts) > 5 else '*'), (parts[6] if len(parts) > 6 else '*')


def _platform_note(configuration: dict) -> str | None:
    """An AND configuration ties the server CPE to a platform (OS/hardware) node."""
    if str(configuration.get('operator') or '').upper() != 'AND':
        return None
    platforms = sorted({m.get('criteria', '').split(':')[3] + ':' + m.get('criteria', '').split(':')[4]
                        for node in configuration.get('nodes') or [] for m in node.get('cpeMatch') or []
                        if not m.get('criteria', '').startswith(CPE_PREFIX) and m.get('criteria', '').count(':') > 4})
    return ('Only when running on: ' + ', '.join(platforms)) if platforms else None


def _cpe_intervals(cve: dict) -> tuple[list[dict], list[str], list[str], list[str]]:
    """(intervals, exact versions, platform notes, unrepresentable notes) from NVD CPE configurations."""
    intervals, versions, platforms, skipped = {}, set(), set(), []
    for configuration in cve.get('configurations') or []:
        note = _platform_note(configuration)
        for node in configuration.get('nodes') or []:
            if node.get('negate'):
                continue
            for match in node.get('cpeMatch') or []:
                criteria = str(match.get('criteria') or '')
                if not criteria.startswith(CPE_PREFIX) or match.get('vulnerable') is not True:
                    continue
                if note:
                    platforms.add(note)
                start_in, start_ex = match.get('versionStartIncluding'), match.get('versionStartExcluding')
                end_ex, end_in = match.get('versionEndExcluding'), match.get('versionEndIncluding')
                version, update = _cpe_parts(criteria)
                if start_in or start_ex or end_ex or end_in:
                    low = _norm(start_in or start_ex) if (start_in or start_ex) else ('0', (0,), '')
                    high = _norm(end_ex or end_in) if (end_ex or end_in) else None
                    if low is None or ((end_ex or end_in) and high is None):
                        skipped.append(f'unparsable range {start_in or start_ex!r}..{end_ex or end_in!r}')
                        continue
                    item = {'introduced': low[0]}
                    if start_ex:
                        item['introduced_exclusive'] = True
                    if high is None:
                        item['unfixed'] = True
                    elif end_ex:
                        if not _lt(low[1], high[1]):
                            skipped.append(f'empty range {low[0]}..{high[0]}')
                            continue
                        item['fixed'] = high[0]
                    else:
                        if _lt(high[1], low[1]):
                            skipped.append(f'empty range {low[0]}..{high[0]}')
                            continue
                        item['last_affected'] = high[0]
                    if low[2] or (high and high[2]):
                        item['approximate'] = True
                    intervals[json.dumps(item, sort_keys=True)] = item
                elif version in ('*', '-', ''):
                    skipped.append(f'wildcard CPE without a version bound ({criteria})')
                else:
                    exact = _norm(version if update in ('*', '-', '') else f'{version}-{update}')
                    if exact is None:
                        skipped.append(f'unparsable version {version!r}/{update!r}')
                    else:
                        versions.add(exact[0])
    ordered = sorted(intervals.values(), key=lambda r: (_norm(r['introduced'])[1], json.dumps(r, sort_keys=True)))
    return ordered, sorted(versions, key=lambda v: (_norm(v)[1], v)), sorted(platforms), skipped


def _cna_intervals(cve: dict) -> tuple[list[dict], list[str], list[str]]:
    """Fallback for CVEs NVD has not analysed: CNA ``affected`` data for the MongoDB Server product."""
    intervals, versions, skipped = {}, set(), []
    for block in cve.get('affected') or []:
        for data in block.get('affectedData') or []:
            if not CNA_PRODUCT.match(str(data.get('product') or '')):
                continue
            for item in data.get('versions') or []:
                if str(item.get('status') or '').lower() != 'affected':
                    continue
                low = _norm(item.get('version'))
                upper_key = 'lessThan' if item.get('lessThan') else ('lessThanOrEqual' if item.get('lessThanOrEqual') else None)
                if low is None:
                    skipped.append(f"unparsable CNA version {item.get('version')!r}")
                    continue
                if upper_key is None:
                    versions.add(low[0])
                    continue
                high = _norm(item[upper_key])
                if high is None or (upper_key == 'lessThan' and not _lt(low[1], high[1])):
                    skipped.append(f'unusable CNA range {item.get("version")!r}..{item.get(upper_key)!r}')
                    continue
                span = {'introduced': low[0], ('fixed' if upper_key == 'lessThan' else 'last_affected'): high[0]}
                intervals[json.dumps(span, sort_keys=True)] = span
    ordered = sorted(intervals.values(), key=lambda r: (_norm(r['introduced'])[1], json.dumps(r, sort_keys=True)))
    return ordered, sorted(versions, key=lambda v: (_norm(v)[1], v)), skipped


def _severity(cve: dict) -> tuple[str, str | None, float | None]:
    metrics = cve.get('metrics') or {}
    for key in SEVERITY_ORDER:
        rows = metrics.get(key) or []
        rows = sorted(rows, key=lambda r: r.get('type') != 'Primary')  # NVD primary score first
        for row in rows:
            data = row.get('cvssData') or {}
            label = str(data.get('baseSeverity') or row.get('baseSeverity') or '').upper()
            if label:
                return label, data.get('vectorString'), data.get('baseScore')
    return 'review advisory', None, None


def _summary(cve: dict) -> str:
    for item in cve.get('descriptions') or []:
        if item.get('lang') == 'en' and item.get('value'):
            return re.sub(r'\s+', ' ', str(item['value'])).strip()[:400]
    return ''


def _page_url(start: int, extra: dict | None = None) -> str:
    query = {'virtualMatchString': VIRTUAL_MATCH, 'resultsPerPage': PAGE_SIZE, 'startIndex': start, **(extra or {})}
    return API + '?' + urllib.parse.urlencode(query)


def fetch_all(fetch=_json_request, sleep=time.sleep) -> list[dict]:
    """Every NVD page for the server CPE; refuses a partial result."""
    records, start, total = [], 0, None
    pause = SLEEP_WITH_KEY_S if os.environ.get('NVD_API_KEY') else SLEEP_WITHOUT_KEY_S
    while True:
        page = fetch(_page_url(start))
        total = int(page.get('totalResults', 0)) if total is None else total
        batch = page.get('vulnerabilities') or []
        records.extend(batch)
        start += int(page.get('resultsPerPage') or len(batch) or PAGE_SIZE)
        if start >= total or not batch:
            break
        sleep(pause)
    if total is not None and len(records) < total:
        raise ValueError(f'NVD returned {len(records)} of {total} CVEs; refusing partial catalog')
    return records


def refresh(catalog: dict, records: list[dict], today: str | None = None, source: str = API) -> tuple[dict, dict]:
    if catalog.get('$schema') != SCHEMA or not isinstance(catalog.get('entries'), list):
        raise ValueError('Not a compatible offline-mongodb-server-cves.json')
    previous = {x.get('cve'): x for x in catalog['entries'] if x.get('cve')}
    entries, unrepresentable, rejected, seen = [], [], [], set()
    sources = {'nvd_cpe': 0, 'cna': 0}
    for record in records:
        cve = record.get('cve') if isinstance(record.get('cve'), dict) else record
        cve_id = str(cve.get('id') or '').upper()
        if not re.fullmatch(r'CVE-\d{4}-\d{4,}', cve_id) or cve_id in seen:
            continue
        seen.add(cve_id)
        if str(cve.get('vulnStatus') or '').lower() == 'rejected':
            rejected.append(cve_id)
            continue
        ranges, versions, platforms, notes = _cpe_intervals(cve)
        range_source = 'nvd_cpe'
        if not ranges and not versions:
            cna_ranges, cna_versions, cna_notes = _cna_intervals(cve)
            if cna_ranges or cna_versions:
                ranges, versions, range_source = cna_ranges, cna_versions, 'cna'
            notes += cna_notes
        if not ranges and not versions:
            unrepresentable.append({'cve': cve_id, 'notes': sorted(set(notes)) or ['no MongoDB Server version data']})
            continue
        sources[range_source] += 1
        severity, vector, score = _severity(cve)
        item = {'cve': cve_id, 'product': 'mongodb-server', 'affected': ranges}
        if versions:
            item['versions'] = versions
        item.update({'severity': severity, 'summary': _summary(cve), 'scope_note': SCOPE_NOTE,
                     'source': 'https://nvd.nist.gov/vuln/detail/' + cve_id, 'range_source': range_source,
                     'nvd_status': cve.get('vulnStatus'), 'published': str(cve.get('published') or '')[:10] or None,
                     'last_modified': str(cve.get('lastModified') or '')[:10] or None})
        if vector:
            item['cvss'] = vector
        if score is not None:
            item['cvss_score'] = score
        if platforms:
            item['platform_note'] = '; '.join(platforms)
        if notes:
            item['range_notes'] = sorted(set(notes))
        entries.append(item)
    entries.sort(key=lambda e: (e.get('published') or '', e['cve']), reverse=True)
    if not entries:
        raise ValueError('No representable MongoDB Server CVE ranges: refusing to replace catalog')
    day = today or dt.datetime.now(dt.timezone.utc).date().isoformat()
    updated = {**catalog, 'catalog_name': catalog.get('catalog_name') or 'MongoDB Server offline CVE catalog',
               'catalog_version': day + '-nvd', 'refreshed_at': day,
               'refresh_owner': 'NVD automated import (scripts/update_offline_mongodb_server_cves.py)',
               'coverage_note': COVERAGE_NOTE, 'sources': [source], 'entries': entries}
    stats = {'nvd_records': len(records), 'entries': len(entries), 'range_sources': sources,
             'platform_specific': sorted(e['cve'] for e in entries if e.get('platform_note')),
             'unfixed_entries': sorted(e['cve'] for e in entries if any(r.get('unfixed') for r in e['affected'])),
             'rejected_skipped': sorted(rejected), 'unrepresentable': unrepresentable,
             **_diff(previous, {e['cve']: e for e in entries}),
             'previous_entries': len(previous)}
    return updated, stats


def _diff(previous: dict, current: dict) -> dict:
    """What this refresh changed versus the previous catalog (ranges or exact versions)."""
    changed = sorted(k for k in current.keys() & previous.keys()
                     if (previous[k].get('affected'), previous[k].get('versions'))
                     != (current[k].get('affected'), current[k].get('versions')))
    return {'added': sorted(current.keys() - previous.keys()), 'changed_ranges': changed,
            'removed': sorted(previous.keys() - current.keys())}

def _write_atomic(path: Path, data: dict) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + '.bak') if path.exists() else None
    if backup is not None:
        shutil.copy2(path, backup)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.offline-mongodb-server-cves-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(data, output, indent=2, ensure_ascii=False)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return backup


def empty_catalog() -> dict:
    return {'$schema': SCHEMA, 'catalog_name': 'MongoDB Server offline CVE catalog', 'entries': []}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--catalog', type=Path, default=CATALOG)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--timeout', type=int, default=30)
    parser.add_argument('--input-json', type=Path, action='append', default=[],
                        help='saved NVD API 2.0 response (repeatable); no network access')
    parser.add_argument('--allow-shrink', action='store_true',
                        help='accept a refresh with fewer than half of the previous entries')
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error('--timeout must be positive')
    path = args.catalog
    original = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else empty_catalog()
    if args.input_json:
        records = [r for p in args.input_json for r in json.loads(p.read_text(encoding='utf-8')).get('vulnerabilities') or []]
        source = API + '?virtualMatchString=' + VIRTUAL_MATCH + ' (saved responses)'
    else:
        records = fetch_all(fetch=lambda url: _json_request(url, args.timeout))
        source = API + '?virtualMatchString=' + VIRTUAL_MATCH
    updated, stats = refresh(original, records, source=source)
    print(json.dumps(stats, indent=2))
    if stats['previous_entries'] and stats['entries'] < stats['previous_entries'] / 2 and not args.allow_shrink:
        raise SystemExit(f"Refusing to replace {stats['previous_entries']} entries with {stats['entries']} "
                         '(use --allow-shrink after checking the NVD response)')
    if args.dry_run:
        print('Dry run: catalog unchanged')
        return
    backup = _write_atomic(path, updated)
    print(f"Updated {path} ({'backup: ' + str(backup) if backup else 'new catalog'}).")


if __name__ == '__main__':
    main()
