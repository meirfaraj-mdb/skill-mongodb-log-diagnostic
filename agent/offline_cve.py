"""Conservative, offline driver/advisory matching. No network calls or app guessing."""
from __future__ import annotations
import json
import os
import re
from pathlib import Path
from typing import Any
from .skills import diagnostic_skill_dir

CATALOG_RELATIVE_PATH = "references/offline-driver-cves.json"
_VERSION = re.compile(r"^v?(\d+(?:\.\d+)*)(?:[-+.]?[A-Za-z~][0-9A-Za-z.\-+~]*)?$")
INCLUDE_UNREVIEWED_ENV = "MONGODB_LOG_DIAG_OFFLINE_CVE_INCLUDE_UNREVIEWED"

# Catalog driver key -> canonical key (older catalogs used mongo-php-driver for the
# Composer mongodb/mongodb library, which is PHPLIB, not the PECL extension).
CATALOG_KEY_ALIASES = {"mongo-php-driver": "mongo-php-library"}
# Extractor canonical driver name (lower case) -> catalog driver key.
OBSERVED_ALIASES = {
    "nodejs": "mongo-nodejs-driver", "node": "mongo-nodejs-driver", "node.js": "mongo-nodejs-driver",
    "mongo-nodejs-driver": "mongo-nodejs-driver",
    "pymongo": "pymongo",
    "mongo-csharp-driver": "mongo-csharp-driver", "mongo-go-driver": "mongo-go-driver",
    "mongo-java-driver": "mongo-java-driver", "mongo-ruby-driver": "mongo-ruby-driver",
    "mongo-rust-driver": "mongo-rust-driver", "mongo-swift-driver": "mongo-swift-driver",
    "mongo-kotlin-driver": "mongo-kotlin-driver", "mongo-scala-driver": "mongo-scala-driver",
}
# Components of a ' / '-joined handshake stack (PHP: "mongoc / ext-mongodb:PHP / PHPLIB").
STACK_COMPONENTS = {"phplib": "mongo-php-library"}


def _version(value: Any) -> tuple[int, ...] | None:
    match = _VERSION.fullmatch(str(value or "").strip())
    return tuple(map(int, match.group(1).split("."))) if match else None


def _compare(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    width = max(len(a), len(b))
    return ((a + (0,) * (width-len(a))) > (b + (0,) * (width-len(b)))) - ((a + (0,) * (width-len(a))) < (b + (0,) * (width-len(b))))


def _affected(version: str, ranges: list[dict], versions: list | None = None) -> bool:
    """OSV semantics: introduced inclusive; fixed exclusive; last_affected inclusive;
    an interval with ``unfixed: true`` has no upper bound. Anything else fails closed."""
    observed = _version(version)
    if observed is None:
        return False
    for listed in versions or []:
        candidate = _version(listed)
        if candidate is not None and _compare(observed, candidate) == 0:
            return True
    for span in ranges:
        introduced = _version(span.get("introduced"))
        if introduced is None or _compare(observed, introduced) < 0:
            continue
        fixed, last = span.get("fixed"), span.get("last_affected")
        if fixed is not None:
            upper = _version(fixed)
            if upper is not None and _compare(introduced, upper) < 0 and _compare(observed, upper) < 0:
                return True
        elif last is not None:
            upper = _version(last)
            if upper is not None and _compare(introduced, upper) <= 0 and _compare(observed, upper) <= 0:
                return True
        elif span.get("unfixed") is True:
            return True
    return False


def load_catalog(path: Path | None = None) -> dict:
    path = path or diagnostic_skill_dir() / CATALOG_RELATIVE_PATH
    with path.open(encoding="utf-8") as handle:
        catalog = json.load(handle)
    if catalog.get("$schema") != "mongodb-log-diagnostic.offline-driver-cves/v1":
        raise ValueError(f"Unsupported offline CVE catalog format: {path}")
    return catalog


def _names(value) -> list[str]:
    if isinstance(value, dict):
        value = list(value)
    if not isinstance(value, list):
        return []
    return sorted({str(x).strip() for x in value if isinstance(x, str) and x.strip() and x.strip().lower() not in ("unknown", "none", "null", "?")})


def observed_drivers(extract: dict) -> list[dict]:
    """Keep observed application names tied to their driver/version, never node-wide.

    ``observed_drivers`` (every application driver, independent of the server/driver
    compatibility matrix) is preferred; the compatibility buckets are kept for older extracts.
    """
    compat = extract.get("driverCompatibility") or {}
    by_pair: dict[tuple[str, str], set[str]] = {}
    raw_by_pair: dict[tuple[str, str], set[str]] = {}
    for field in ("observed_drivers", "distinct_compatible_drivers"):
        versions = compat.get(field) or {}
        if not isinstance(versions, dict):
            continue
        for name, entries in versions.items():
            if isinstance(entries, dict):
                for version, details in entries.items():
                    key = (str(name), str(version))
                    details = details if isinstance(details, dict) else {}
                    by_pair.setdefault(key, set()).update(_names(details.get("distinctAppNames")))
                    raw_by_pair.setdefault(key, set()).update(_names(details.get("rawNames")))
            elif isinstance(entries, list):
                for item in entries:
                    if isinstance(item, dict) and item.get("version"):
                        key = (str(name), str(item["version"]))
                        by_pair.setdefault(key, set()).update(_names(item.get("distinctAppNames")))
    for item in compat.get("incompatible_drivers") or []:
        if isinstance(item, dict) and item.get("driver_name") and item.get("driver_version"):
            key = (str(item["driver_name"]), str(item["driver_version"]))
            by_pair.setdefault(key, set()).update(_names(item.get("distinctAppNames")))
    return [{"driver": k[0], "version": k[1], "observed_app_names": sorted(v),
             **({"raw_names": sorted(raw_by_pair[k])} if raw_by_pair.get(k) else {}),
             "attribution": "observed in driver metadata; ownership and impact not established" if v else "not attributable from extraction"}
            for k, v in sorted(by_pair.items())]


def catalog_targets(driver: dict) -> list[tuple[str, str, str]]:
    """(catalog driver key, version to compare, matched component) for one observed driver."""
    name, version = str(driver.get("driver") or ""), str(driver.get("version") or "")
    if " / " in name:
        names, versions = [p.strip() for p in name.split(" / ")], [p.strip() for p in version.split(" / ")]
        return [(STACK_COMPONENTS[n.lower()], v, n) for n, v in zip(names, versions) if n.lower() in STACK_COMPONENTS]
    base = name.split("|", 1)[0].strip()
    key = OBSERVED_ALIASES.get(base.lower())
    if not key:
        return []
    targets = [(key, version, base)]
    if key == "mongo-java-driver":
        # JVM wrappers report the shared Java core version; add their own advisory keys.
        tails = " ".join("|".join(r.lower().split("|")[1:]) for r in driver.get("raw_names") or [])
        if "kotlin" in tails:
            targets.append(("mongo-kotlin-driver", version, "kotlin"))
        if "scala" in tails:
            targets.append(("mongo-scala-driver", version, "scala"))
    return targets


def include_unreviewed_default() -> bool:
    return os.environ.get(INCLUDE_UNREVIEWED_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def check(extract: dict, catalog: dict | None = None, include_unreviewed: bool | None = None) -> dict:
    catalog = catalog if catalog is not None else load_catalog()
    include_unreviewed = include_unreviewed_default() if include_unreviewed is None else include_unreviewed
    observed = observed_drivers(extract)
    matches, unreviewed_skipped = [], 0
    for driver in observed:
        for key, version, component in catalog_targets(driver):
            for entry in catalog.get("entries", []):
                entry_key = CATALOG_KEY_ALIASES.get(entry.get("driver"), entry.get("driver"))
                if (entry_key != key or not entry.get("source")
                        or not _affected(version, entry.get("affected") or [], entry.get("versions"))):
                    continue
                # Reviewed entries require a driver-specific advisory; unreviewed OSV candidates
                # are only reported when explicitly enabled and are labelled as such.
                if not entry.get("reviewed"):
                    if not include_unreviewed:
                        unreviewed_skipped += 1
                        continue
                match = {**driver, **{k: entry[k] for k in ("cve", "advisory_ids", "severity", "cvss", "summary", "source",
                                                            "affected", "versions", "scope_note") if k in entry},
                         "catalog_driver": key, "matched_component": component, "matched_version": version,
                         "review_status": "reviewed" if entry.get("reviewed") else "unreviewed_candidate",
                         "assessment": "version-range match; component exposure and application ownership unverified"}
                matches.append(match)
    status = "matches_found" if matches else "no_match_in_snapshot"
    return {"mode": "offline_catalog", "catalog": {k: catalog.get(k) for k in ("catalog_name", "catalog_version", "refreshed_at", "refresh_owner", "coverage_note", "sources")},
            "observed_drivers": observed, "matches": matches, "match_count": len(matches),
            "include_unreviewed": include_unreviewed, "unreviewed_candidates_not_reported": unreviewed_skipped,
            "status": status}
