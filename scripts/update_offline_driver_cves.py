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
# Exact OSV package identities. "driver" is the catalog key that agent/offline_cve.py maps
# observed handshake names onto (OBSERVED_ALIASES / STACK_COMPONENTS); a new key needs a
# mapping there, otherwise its entries can never match.
# npm's mongodb package is the official driver for JS *and* TypeScript, not two drivers.
# PHP Composer package is a library layered over the native mongodb extension: partial
# coverage only. C and C++ have no safe direct package identity in this importer.
PACKAGES = (
    ('mongo-csharp-driver', 'NuGet', 'MongoDB.Driver'),
    ('mongo-csharp-driver', 'NuGet', 'MongoDB.Driver.Core'),
    ('mongo-go-driver', 'Go', 'go.mongodb.org/mongo-driver'),
    ('mongo-go-driver', 'Go', 'go.mongodb.org/mongo-driver/v2'),
    ('mongo-java-driver', 'Maven', 'org.mongodb:mongodb-driver-sync'),
    ('mongo-java-driver', 'Maven', 'org.mongodb:mongodb-driver-core'),
    ('mongo-java-driver', 'Maven', 'org.mongodb:mongodb-driver-reactivestreams'),
    ('mongo-java-driver', 'Maven', 'org.mongodb:mongodb-driver-legacy'),
    ('mongo-java-driver', 'Maven', 'org.mongodb:mongodb-driver'),
    ('mongo-java-driver', 'Maven', 'org.mongodb:mongo-java-driver'),
    ('mongo-kotlin-driver', 'Maven', 'org.mongodb:mongodb-driver-kotlin-coroutine'),
    ('mongo-kotlin-driver', 'Maven', 'org.mongodb:mongodb-driver-kotlin-sync'),
    ('mongo-nodejs-driver', 'npm', 'mongodb'),
    ('mongo-php-library', 'Packagist', 'mongodb/mongodb'),
    ('pymongo', 'PyPI', 'pymongo'),
    ('mongo-ruby-driver', 'RubyGems', 'mongo'),
    ('mongo-rust-driver', 'crates.io', 'mongodb'),
    ('mongo-scala-driver', 'Maven', 'org.mongodb.scala:mongo-scala-driver_2.12'),
    ('mongo-scala-driver', 'Maven', 'org.mongodb.scala:mongo-scala-driver_2.13'),
    ('mongo-scala-driver', 'Maven', 'org.mongodb.scala:mongo-scala-driver_3'),
    ('mongo-swift-driver', 'SwiftURL', 'github.com/mongodb/mongo-swift-driver'),
)
# Report unsupported coverage even if OSV returns no entries. No fuzzy package searches.
COVERAGE = {
    'C': ('gap', 'Native libmongoc has no verified direct OSV package mapping here.'),
    'C++': ('gap', 'Native mongocxx has no verified direct OSV package mapping here; do not treat C advisories as C++ advisories.'),
    '.NET/C#': ('candidate', 'NuGet MongoDB.Driver and MongoDB.Driver.Core (pre-3.0 split package).'),
    'Go': ('candidate', 'Go module v1 and v2 are queried separately.'),
    'Java': ('candidate', 'Maven sync, core, reactive-streams, legacy, mongodb-driver and uber-jar mongo-java-driver artifacts; review applicability per artifact.'),
    'Kotlin': ('candidate', 'Kotlin sync and coroutine artifacts; Java dependency CVEs are not automatically Kotlin CVEs.'),
    'Node.js/JS/TS': ('candidate', 'One npm driver package serves all three languages.'),
    'PHP': ('partial', 'Composer mongodb/mongodb library (PHPLIB) only; PECL mongodb native extension is not in OSV and is not queried.'),
    'Python': ('candidate', 'PyPI pymongo package only (Motor reports PyMongo in the handshake and is matched as PyMongo).'),
    'Ruby': ('candidate', 'RubyGems mongo package only.'),
    'Rust': ('candidate', 'crates.io mongodb package only.'),
    'Scala': ('candidate', 'Maven artifacts for Scala 2.12, 2.13 and 3; Java dependency CVEs are not automatically Scala CVEs.'),
    'Swift': ('candidate', 'SwiftURL github.com/mongodb/mongo-swift-driver (driver is end-of-life).'),
}
# '0' (OSV "since the first release") and 1-4 numeric parts. A pre-release/build suffix
# (4.0.0-beta1, 4.0b1) is reduced to its numeric part and flagged approximate.
VERSION = re.compile(r'^v?(\d+(?:\.\d+){0,3})((?:[-+.~]?[A-Za-z][0-9A-Za-z.\-+~]*)?)$')
SEVERITIES = ('CRITICAL', 'HIGH', 'MODERATE', 'MEDIUM', 'LOW')
CVE = re.compile(r'^CVE-\d{4}-\d{4,}$', re.I)


def _json_request(url: str, data: dict | None = None, timeout: int = 20) -> dict:
    if not url.startswith(API + '/'):
        raise ValueError('Unexpected API URL')
    payload = json.dumps(data).encode() if data is not None else None
    request = urllib.request.Request(url, data=payload, method='POST' if payload else 'GET',
                                     headers={'Content-Type': 'application/json', 'User-Agent': 'mongodb-driver-cve-refresh/1'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _norm(value) -> tuple[str, tuple[int, ...], bool] | None:
    match = VERSION.fullmatch(str(value or '').strip())
    if not match:
        return None
    text = match.group(1)
    return text, tuple(map(int, text.split('.'))), bool(match.group(2))


def _lt(a: tuple[int, ...], b: tuple[int, ...]) -> bool:
    n = max(len(a), len(b))
    return a + (0,) * (n-len(a)) < b + (0,) * (n-len(b))


def _intervals(record: dict, ecosystem: str, package: str) -> tuple[list[dict], list[str], list[str]]:
    """Normalize OSV ranges for one package: (intervals, explicit versions, skipped notes).

    introduced is inclusive ("0" = all earlier releases); fixed is exclusive; last_affected is
    inclusive; an introduced event with no later bound is kept as ``unfixed: true`` (no fix
    released). GIT ranges and unparsable versions are reported, never guessed.
    """
    intervals, versions, skipped = set(), set(), []
    for affected in record.get('affected', []):
        p = affected.get('package') or {}
        if p.get('ecosystem') != ecosystem or p.get('name') != package:
            continue
        usable = False
        for span in affected.get('ranges') or []:
            if span.get('type') not in ('ECOSYSTEM', 'SEMVER'):
                skipped.append(f"{span.get('type')} range ignored")
                continue
            introduced = None
            for event in span.get('events') or []:
                if 'introduced' in event:
                    if introduced is not None:
                        intervals.add((introduced[0], 'unfixed', '', introduced[2]))
                        usable = True
                    introduced = _norm(event['introduced'])
                    if introduced is None:
                        skipped.append(f"unparsable introduced {event['introduced']!r}")
                elif 'fixed' in event or 'last_affected' in event:
                    kind = 'fixed' if 'fixed' in event else 'last_affected'
                    upper = _norm(event[kind])
                    if introduced is None or upper is None:
                        skipped.append(f"unparsable {kind} {event[kind]!r}")
                    elif (_lt(introduced[1], upper[1]) if kind == 'fixed' else not _lt(upper[1], introduced[1])):
                        intervals.add((introduced[0], kind, upper[0], introduced[2] or upper[2]))
                        usable = True
                    introduced = None
                elif 'limit' in event:
                    skipped.append('limit event ignored')
                    introduced = None
            if introduced is not None:
                intervals.add((introduced[0], 'unfixed', '', introduced[2]))
                usable = True
        if not usable:
            for v in affected.get('versions') or []:
                n = _norm(v)
                if n:
                    versions.add(n[0])
    out = []
    for intro, kind, upper, approx in sorted(intervals, key=lambda i: (_norm(i[0])[1], i[1], i[2])):
        item = {'introduced': intro}
        if kind == 'unfixed':
            item['unfixed'] = True
        else:
            item[kind] = upper
        if approx:
            item['approximate'] = True
        out.append(item)
    return out, sorted(versions, key=lambda v: _norm(v)[1]), skipped


def _severity(record: dict) -> tuple[str, str | None]:
    db = record.get('database_specific') or {}
    label = str(db.get('severity') or '').upper()
    for affected in record.get('affected') or []:
        label = label or str(((affected.get('ecosystem_specific') or {}).get('severity')) or '').upper()
        label = label or str(((affected.get('database_specific') or {}).get('severity')) or '').upper()
    cvss = next((s.get('score') for s in record.get('severity') or [] if str(s.get('type', '')).startswith('CVSS')), None)
    return (label if label in SEVERITIES else 'review advisory'), cvss


def _entry_id(cve: str | None, ids) -> str | None:
    ids = sorted(i for i in ids or [] if isinstance(i, str))
    ghsa = [i for i in ids if i.startswith('GHSA-')]
    return cve or (ghsa[0] if ghsa else (ids[0] if ids else None))


def _advisory_key(record: dict) -> tuple[str | None, list[str]]:
    ids = sorted({x for x in [record.get('id'), *(record.get('aliases') or [])] if isinstance(x, str)})
    cves = sorted({c.upper() for c in ids if CVE.fullmatch(c)})
    return (cves[0] if cves else None), ids


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
    legacy = {'mongo-php-driver': 'mongo-php-library'}
    previous = {(legacy.get(x['driver'], x['driver']), _entry_id(x.get('cve'), x.get('advisory_ids'))): x
                for x in catalog['entries'] if x.get('driver') and _entry_id(x.get('cve'), x.get('advisory_ids'))}
    grouped: dict[tuple[str, str], dict] = {}
    per_package, withdrawn, unrepresentable = {}, set(), []
    details_cache = {}
    for driver, ecosystem, package in PACKAGES:
        found = _fetch_package(ecosystem, package, fetch)
        per_package[f'{ecosystem}:{package}'] = len(found)
        for osv_id in found:
            if osv_id not in details_cache:
                details_cache[osv_id] = fetch(API + '/vulns/' + urllib.parse.quote(osv_id, safe=''))
            record = details_cache[osv_id]
            if record.get('withdrawn'):
                withdrawn.add(osv_id)
                continue
            cve, ids = _advisory_key(record)
            ranges, versions, notes = _intervals(record, ecosystem, package)
            severity, cvss = _severity(record)
            # One entry per (driver, vulnerability). Advisories without a CVE keep their
            # GHSA/OSV id so they are not silently dropped.
            vuln = _entry_id(cve, ids)
            key = driver, vuln
            item = grouped.setdefault(key, {'cve': cve, 'driver': driver, 'affected': [], 'versions': [],
                'advisory_ids': [], 'severity': severity, 'cvss': cvss, 'summary': '',
                'scope_note': 'Version-range candidate only. Verify advisory, feature use, and application ownership.',
                'source': API + '/vulns/' + urllib.parse.quote(osv_id, safe=''), 'reviewed': False,
                'packages': []})
            item['advisory_ids'] = sorted(set(item['advisory_ids']) | set(ids))
            if item['severity'] == 'review advisory' and severity != 'review advisory':
                item['severity'] = severity
            item['cvss'] = item['cvss'] or cvss
            if ranges or versions:
                item['affected'].extend(ranges)
                item['versions'].extend(versions)
                item['packages'].append({'ecosystem': ecosystem, 'name': package, 'osv_id': osv_id})
            elif notes:
                unrepresentable.append({'id': vuln, 'osv_id': osv_id, 'package': f'{ecosystem}:{package}', 'notes': sorted(set(notes))})
            # Prefer the GitHub advisory text, then the first non-empty summary.
            text = str(record.get('summary') or record.get('details') or '').strip()[:400]
            if text and (not item['summary'] or osv_id.startswith('GHSA-')):
                item['summary'] = text
    entries = []
    for key, item in sorted(grouped.items()):
        uniq = {json.dumps(r, sort_keys=True): r for r in item['affected']}
        item['affected'] = sorted(uniq.values(), key=lambda r: (_norm(r['introduced'])[1], json.dumps(r, sort_keys=True)))
        item['versions'] = sorted(set(item['versions']), key=lambda v: _norm(v)[1])
        item['packages'] = sorted(item['packages'], key=lambda x: (x['ecosystem'], x['name'], x['osv_id']))
        if not item['versions']:
            del item['versions']
        if not item['cvss']:
            del item['cvss']
        if not item['affected'] and not item.get('versions'):
            continue  # cannot represent; reported in stats['unrepresentable']
        old = previous.get(key, {})
        # Preserve human review ONLY if every normalized interval remains identical.
        if old.get('reviewed') is True and old.get('affected') == item['affected'] and old.get('versions') == item.get('versions'):
            item['reviewed'] = True
            item['source'] = old['source']
            item['summary'] = old.get('summary', item['summary'])
            item['scope_note'] = old.get('scope_note', item['scope_note'])
        entries.append(item)
    day = today or dt.datetime.now(dt.timezone.utc).date().isoformat()
    updated = {**catalog, 'catalog_version': day + '-osv-candidates', 'refreshed_at': day,
               'refresh_owner': 'OSV automated import; human review required for new/changed ranges',
               'coverage_note': ('Incomplete OSV snapshot for exact allowlisted packages; C/C++ and the PHP native extension '
                                 'are not covered. New or changed matches are unreviewed and excluded from reports unless '
                                 'MONGODB_LOG_DIAG_OFFLINE_CVE_INCLUDE_UNREVIEWED=true (then labelled unreviewed_candidate). '
                                 'Absence of a match is not evidence of safety; a match does not establish application exposure.'),
               'sources': [API], 'entries': entries}
    stats = {'coverage': {language: {'status': status, 'note': note} for language, (status, note) in COVERAGE.items()},
             'packages_queried': len(PACKAGES), 'package_result_counts': per_package, 'entries': len(entries), 'reviewed_unchanged': sum(e['reviewed'] for e in entries),
             'needs_review': sum(not e['reviewed'] for e in entries),
             'unfixed_entries': sorted(_entry_id(e['cve'], e['advisory_ids']) for e in entries if any(r.get('unfixed') for r in e['affected'])),
             'withdrawn_skipped': sorted(withdrawn), 'unrepresentable': unrepresentable,
             'previously_reviewed_not_retained': sorted(k[1] for k, e in previous.items()
                                                       if e.get('reviewed') and not any((n['driver'], _entry_id(n['cve'], n['advisory_ids'])) == k and n['reviewed'] for n in entries))}
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
