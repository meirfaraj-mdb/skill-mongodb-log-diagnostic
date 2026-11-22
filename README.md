# Offline MongoDB driver CVE candidate refresh

Copy `scripts/update_offline_driver_cves.py` and `tests/test_update_offline_driver_cves.py` into the matching repository paths. Run from the repository root:

```bash
python3 scripts/update_offline_driver_cves.py --dry-run
python3 scripts/update_offline_driver_cves.py
python3 -m unittest discover -s tests -p 'test_update_offline_driver_cves.py'
```

Requires outbound access to `https://api.osv.dev`. The script queries **16 exact OSV packages**: NuGet (.NET); Go modules v1/v2; Maven Java sync/core/reactive, Kotlin sync/coroutine and Scala 2.12/2.13/3; npm Node.js/JS/TS; Packagist PHP library; PyPI PyMongo; RubyGems Ruby; crates.io Rust. It prints package query counts and a coverage ledger. C/libmongoc and C++/mongocxx are **explicit gaps**, not implicitly covered through unrelated OS packages; PHP coverage excludes the native PECL extension. Driver names in the catalog must match driver names extracted from logs for any candidate to be matched. Package query counts, including zero, are not evidence a driver is safe or fully covered.

Only OSV records with exact package identities, CVE aliases and representable closed version intervals are imported. New/changed candidates remain `reviewed: false` and are excluded by the offline report matcher. Previously reviewed entries retain approval only with identical affected intervals; review package applicability as well. The script makes a `.bak` backup then atomically replaces the JSON; failures or no representable entries leave the catalog untouched. Review the `previously_reviewed_not_retained` list and backup after each run. Neither a candidate version match nor an observed app name proves exploitability.

This is an **incomplete OSV snapshot**, not a complete live driver security feed. For C/C++ and PHP extension, integrate a verified vendor-advisory source and an independently reviewed version matcher before claiming coverage; do not map OS-distribution package CVEs to driver versions. The refresh doesn't update prompts, extraction driver-name normalization, or GitHub automatically.

# Offline MongoDB server CVE refresh

The node report also checks the **MongoDB server version** against an offline NVD snapshot,
`skills/mongodb-log-diagnostic/references/offline-mongodb-server-cves.json`
(schema `mongodb-log-diagnostic.offline-mongodb-server-cves/v1`). The result is the
`offline_mongodb_server_cves` document and the report section "MongoDB server version and CVE review",
placed immediately before the driver compatibility section.

```bash
python3 scripts/update_offline_mongodb_server_cves.py --dry-run
python3 scripts/update_offline_mongodb_server_cves.py
# air-gapped: build from saved NVD API 2.0 responses
python3 scripts/update_offline_mongodb_server_cves.py --input-json nvd-page-0.json
python3 -m unittest discover -s tests -p 'test_update_offline_mongodb_server_cves.py'
```

Requires outbound access to `https://services.nvd.nist.gov` (set `NVD_API_KEY` for a higher rate limit). The
query is the exact server CPE (`virtualMatchString=cpe:2.3:a:mongodb:mongodb`), paginated; only
`vulnerable: true` matches for `cpe:2.3:a:mongodb:mongodb:*` are imported. NVD bounds map to the same
interval semantics as the driver catalog (`introduced` inclusive, `fixed` exclusive, `last_affected`
inclusive); exact pre-release CPEs (`4.0.0:rc1`) only match that build; CPEs ANDed with an OS keep a
`platform_note`; a wildcard CPE without bounds is listed under `unrepresentable` and never matched. CVEs
that NVD has not analysed yet fall back to the CNA `affected` data for "MongoDB Server"
(`range_source: "cna"`). New/changed entries are `reviewed: false` (reported only with
`MONGODB_LOG_DIAG_OFFLINE_CVE_INCLUDE_UNREVIEWED=true` / `offline_cve_include_unreviewed`, labelled
`unreviewed_candidate`); review is preserved only when the intervals are identical. The script writes a
`.bak` backup and replaces the JSON atomically, and refuses an empty result or a result with fewer than half
of the previous entries (override with `--allow-shrink`).

The server version comes from the log `buildInfo` record (extractor 1.8 `mongodbServer.version`). A log
that does not include the process start has no version: set `mongodb_server_version` in the pipeline config
as a fallback, otherwise the section states that the version was not available.
