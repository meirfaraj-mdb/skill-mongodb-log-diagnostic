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
