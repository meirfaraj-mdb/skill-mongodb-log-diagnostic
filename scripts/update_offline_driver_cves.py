#!/usr/bin/env python3
"""Refresh the offline driver-CVE catalog from OSV; never auto-approve a finding.

Usage (from any directory): python scripts/update_offline_driver_cves.py [--dry-run]
Requires outbound access to api.osv.dev at runtime; no third-party Python modules.
The package allowlist below is intentionally exact, not a fuzzy driver-name search.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.request
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG = REPO_ROOT / 'skills/mongodb-log-diagnostic/references/offline-driver-cves.json'
SCHEMA = 'mongodb-log-diagnostic.offline-driver-cves/v1'
API = 'https://api.osv.dev/v1'
# Exact OSV package identities. "driver" MUST match agent/offline_cve.py's
# observed driver key; adding a package here does not make the extractor recognize it.
# npm's mongodb package is the official driver for JS *and* TypeScript, not two drivers.
# PHP Composer package is a library layered over the native mongodb extension: partial
# coverage only. C and C++ have no safe direct package identity in this importer.
PACKAGES = (
    ('mongo-csharp-driver', 'NuGet', 'MongoDB.Driver'),
    ('mongo-go-driver', 'Go', 'go.mongodb.org/mongo-driver'),
    ('mongo-go-driver', 'Go', 'go.mongodb.org/mongo-driver/v2'),
    ('mongo-java-driver', 'Maven', 'org.mongodb:mongodb-driver-sync'),
    ('mongo-java-driver', 'Maven', 'org.mongodb:mongodb-driver-core'),
    ('mongo-java-driver', 'Maven', 'org.mongodb:mongodb-driver-reactivestreams'),
    ('mongo-kotlin-driver', 'Maven', 'org.mongodb:mongodb-driver-kotlin-coroutine'),
    ('mongo-kotlin-driver', 'Maven', 'org.mongodb:mongodb-driver-kotlin-sync'),
    ('mongo-nodejs-driver', 'npm', 'mongodb'),
    ('mongo-php-driver', 'Packagist', 'mongodb/mongodb'),
    ('pymongo', 'PyPI', 'pymongo'),
    ('mongo-ruby-driver', 'RubyGems', 'mongo'),
    ('mongo-rust-driver', 'crates.io', 'mongodb'),
    ('mongo-scala-driver', 'Maven', 'org.mongodb.scala:mongo-scala-driver_2.12'),
    ('mongo-scala-driver', 'Maven', 'org.mongodb.scala:mongo-scala-driver_2.13'),
    ('mongo-scala-driver', 'Maven', 'org.mongodb.scala:mongo-scala-driver_3'),
)
# Report unsupported coverage even if OSV returns no entries. No fuzzy package searches.
COVERAGE = {
    'C': ('gap', 'Native libmongoc has no verified direct OSV package mapping here.'),
    'C++': ('gap', 'Native mongocxx has no verified direct OSV package mapping here; do not treat C advisories as C++ advisories.'),
    '.NET/C#': ('candidate', 'NuGet MongoDB.Driver only; subpackages are not separately queried.'),
    'Go': ('candidate', 'Go module v1 and v2 are queried separately.'),
    'Java': ('candidate', 'Maven sync, core, and reactive-streams artifacts; review applicability per artifact.'),
    'Kotlin': ('candidate', 'Kotlin sync and coroutine artifacts; Java dependency CVEs are not automatically Kotlin CVEs.'),
    'Node.js/JS/TS': ('candidate', 'One npm driver package serves all three languages.'),
    'PHP': ('partial', 'Composer mongodb/mongodb library only; PECL mongodb native extension is not queried.'),
    'Python': ('candidate', 'PyPI pymongo package only.'),
    'Ruby': ('candidate', 'RubyGems mongo package only.'),
    'Rust': ('candidate', 'crates.io mongodb package only.'),
    'Scala': ('candidate', 'Maven artifacts for Scala 2.12, 2.13 and 3; Java dependency CVEs are not automatically Scala CVEs.'),
}
VERSION = re.compile(r'^\d+(?:\.\d+){1,3}$')
CVE = re.compile(r'^CVE-\d{4}-\d{4,}$', re.I)


def _json_request(url: str, data: dict | None = None, timeout: int = 20) -> dict:
    if not url.startswith(API + '/'):
        raise ValueError('Unexpected API URL')
    payload = json.dumps(data).encode() if data is not None else None
    request = urllib.request.Request(url, data=payload, method='POST' if payload else 'GET',
                                     headers={'Content-Type': 'application/json', 'User-Agent': 'mongodb-driver-cve-refresh/1'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _intervals(record: dict, ecosystem: str, package: str) -> list[dict]:
    """Only explicit introduced/fixed pairs: never extrapolate unfixed or complex ranges."""
    intervals = set()
    for affected in record.get('affected', []):
        p = affected.get('package') or {}
        if p.get('ecosystem') != ecosystem or p.get('name') != package:
            continue
        for span in affected.get('ranges') or []:
            if span.get('type') not in ('ECOSYSTEM', 'SEMVER'):
                continue
            introduced = None
            for event in span.get('events') or []:
                if 'introduced' in event:
                    introduced = event['introduced']
                elif 'fixed' in event:
                    fixed = event['fixed']
                    if introduced and VERSION.fullmatch(str(introduced)) and VERSION.fullmatch(str(fixed)):
                        a, b = tuple(map(int, introduced.split('.'))), tuple(map(int, fixed.split('.')))
                        n = max(len(a), len(b))
                        if a + (0,) * (n-len(a)) < b + (0,) * (n-len(b)):
                            intervals.add((introduced, fixed))
                    introduced = None
                else:
                    introduced = None  # last_affected/limit cannot be expressed by matcher
    return [{'introduced': a, 'fixed': b} for a, b in sorted(intervals)]


def _fetch_package(ecosystem: str, package: str, fetch) -> list[str]:
    ids, token, seen = set(), None, set()
    while True:
        body = {'package': {'ecosystem': ecosystem, 'name': package}}
        if token:
            body['page_token'] = token
        result = fetch(API + '/query', body)
        ids.update(v['id'] for v in result.get('vulns', []) if isinstance(v.get('id'), str))
        token = result.get('next_page_token')
        if not token:
            break
        if token in seen:
            raise ValueError('OSV repeated a pagination token; refusing partial catalog')
        seen.add(token)
    return sorted(ids)


def refresh(catalog: dict, fetch=_json_request, today: str | None = None) -> tuple[dict, dict]:
    if catalog.get('$schema') != SCHEMA or not isinstance(catalog.get('entries'), list):
        raise ValueError('Not a compatible offline-driver-cves.json')
    previous = {(x['driver'], x['cve']): x for x in catalog['entries'] if x.get('driver') and x.get('cve')}
    grouped: dict[tuple[str, str], dict] = {}
    per_package = {}
    details_cache = {}
    for driver, ecosystem, package in PACKAGES:
        found = _fetch_package(ecosystem, package, fetch)
        per_package[f'{ecosystem}:{package}'] = len(found)
        for osv_id in found:
            if osv_id not in details_cache:
                details_cache[osv_id] = fetch(API + '/vulns/' + urllib.parse.quote(osv_id, safe=''))
            record = details_cache[osv_id]
            if record.get('withdrawn'):
                continue
            cves = sorted({c.upper() for c in [record.get('id'), *(record.get('aliases') or [])]
                           if isinstance(c, str) and CVE.fullmatch(c)})
            ranges = _intervals(record, ecosystem, package)
            for cve in cves:
                key = driver, cve
                item = grouped.setdefault(key, {'cve': cve, 'driver': driver, 'affected': [],
                    'severity': 'review advisory', 'summary': '',
                    'scope_note': 'Version-range candidate only. Verify advisory, feature use, and application ownership.',
                    'source': API + '/vulns/' + urllib.parse.quote(osv_id, safe=''), 'reviewed': False,
                    'packages': []})
                if ranges:
                    item['affected'].extend(ranges)
                    item['packages'].append({'ecosystem': ecosystem, 'name': package, 'osv_id': osv_id})
                item['summary'] = str(record.get('summary') or '')[:400] or item['summary']
    entries = []
    for key, item in sorted(grouped.items()):
        item['affected'] = sorted({(r['introduced'], r['fixed']) for r in item['affected']})
        item['affected'] = [{'introduced': a, 'fixed': b} for a, b in item['affected']]
        item['packages'] = sorted(item['packages'], key=lambda x: (x['ecosystem'], x['name'], x['osv_id']))
        if not item['affected']:
            continue  # cannot represent; no false negatives claimed
        old = previous.get(key, {})
        # Preserve human review ONLY if every normalized interval remains identical.
        if old.get('reviewed') is True and old.get('affected') == item['affected']:
            item['reviewed'] = True
            item['source'] = old['source']
            item['summary'] = old.get('summary', item['summary'])
            item['scope_note'] = old.get('scope_note', item['scope_note'])
        entries.append(item)
    day = today or dt.datetime.now(dt.timezone.utc).date().isoformat()
    updated = {**catalog, 'catalog_version': day + '-osv-candidates', 'refreshed_at': day,
               'refresh_owner': 'OSV automated import; human review required for new/changed ranges',
               'coverage_note': ('Incomplete OSV snapshot for exact allowlisted packages; C/C++ and the PHP native extension '
                                 'are not covered. New or changed matches are unreviewed and excluded from reports. '
                                 'Absence of a match is not evidence of safety; a match does not establish application exposure.'),
               'sources': [API], 'entries': entries}
    stats = {'coverage': {language: {'status': status, 'note': note} for language, (status, note) in COVERAGE.items()},
             'packages_queried': len(PACKAGES), 'package_result_counts': per_package, 'entries': len(entries), 'reviewed_unchanged': sum(e['reviewed'] for e in entries),
             'needs_review': sum(not e['reviewed'] for e in entries),
             'previously_reviewed_not_retained': sorted(k[1] for k, e in previous.items()
                                                       if e.get('reviewed') and not any((n['driver'], n['cve']) == k and n['reviewed'] for n in entries))}
    if not entries:
        raise ValueError('No representable CVE ranges returned: refusing to replace catalog')
    return updated, stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, default=CATALOG)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--timeout', type=int, default=20)
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error('--timeout must be positive')
    path = args.catalog
    if not path.is_file():
        parser.error(f'Catalog not found: {path} (use --catalog PATH if the catalog is elsewhere)')
    original = json.loads(path.read_text(encoding='utf-8'))
    updated, stats = refresh(original, fetch=lambda url, body=None: _json_request(url, body, args.timeout))
    print(json.dumps(stats, indent=2))
    if args.dry_run:
        print('Dry run: catalog unchanged')
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + '.bak')
    shutil.copy2(path, backup)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.offline-driver-cves-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(updated, output, indent=2, ensure_ascii=False)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    print(f'Updated {path} (backup: {backup}). Review unapproved entries before using them in reports.')


if __name__ == '__main__':
    main()
