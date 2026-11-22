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


def _status(matches: list) -> str:
    return "matches_found" if matches else "no_match_in_snapshot"


def check(extract: dict, catalog: dict | None = None) -> dict:
    catalog = catalog if catalog is not None else load_catalog()
    observed = observed_drivers(extract)
    matches, uncovered = [], []
    for driver in observed:
        targets = catalog_targets(driver)
        if not targets:
            uncovered.append(f"{driver.get('driver')} {driver.get('version')}")
        for key, version, component in targets:
            for entry in catalog.get("entries", []):
                entry_key = CATALOG_KEY_ALIASES.get(entry.get("driver"), entry.get("driver"))
                if (entry_key != key or not entry.get("source")
                        or not _affected(version, entry.get("affected") or [], entry.get("versions"))):
                    continue
                match = {**driver, **{k: entry[k] for k in ("cve", "advisory_ids", "severity", "cvss", "summary", "source",
                                                            "affected", "versions", "scope_note") if k in entry},
                         "catalog_driver": key, "matched_component": component, "matched_version": version,
                         "assessment": "version-range match; component exposure and application ownership unverified"}
                matches.append(match)
    status = _status(matches)
    if not observed:
        status = "no_application_driver_observed"
    elif status == "no_match_in_snapshot" and len(uncovered) == len(observed):
        status = "not_covered_by_snapshot"
    result = {"mode": "offline_catalog", "catalog": {k: catalog.get(k) for k in ("catalog_name", "catalog_version", "refreshed_at", "refresh_owner", "coverage_note", "sources")},
              "observed_drivers": observed, "matches": matches, "match_count": len(matches),
              "drivers_not_covered_by_snapshot": uncovered, "status": status}
    return result


# --- MongoDB server version (offline NVD snapshot) -------------------------------------------------
SERVER_CATALOG_RELATIVE_PATH = "references/offline-mongodb-server-cves.json"
SERVER_SCHEMA = "mongodb-log-diagnostic.offline-mongodb-server-cves/v1"
_SERVER_VERSION = re.compile(r"^v?(\d+\.\d+\.\d+)(?:-([0-9A-Za-z.\-]+))?$")
_MAJOR_MINOR = re.compile(r"^v?(\d+)\.(\d+)$")


def load_server_catalog(path: Path | None = None) -> dict:
    path = path or diagnostic_skill_dir() / SERVER_CATALOG_RELATIVE_PATH
    with path.open(encoding="utf-8") as handle:
        catalog = json.load(handle)
    if catalog.get("$schema") != SERVER_SCHEMA:
        raise ValueError(f"Unsupported offline MongoDB server CVE catalog format: {path}")
    return catalog


def server_version(extract: dict, fallback_version: str | None = None) -> tuple[str | None, str | None]:
    """(version, source): log buildInfo (parser >= 1.8) first, then the configured fallback."""
    server = (extract or {}).get("mongodbServer") or {}
    compat = (extract or {}).get("driverCompatibility") or {}
    for value, source in ((server.get("version"), server.get("source") or "log buildInfo"),
                          (compat.get("mongodb_full_version"), "log buildInfo"),
                          (fallback_version, "configuration (mongodb_server_version)"),
                          (server.get("major_minor") or compat.get("mongodb_version"), "log buildInfo (major.minor only)")):
        if value:
            return str(value).strip(), source
    return None, None


def _server_affected(version: str, entry: dict) -> bool:
    """Exact pre-release builds (``4.0.0-rc1``) only match that build; GA versions compare numerically.

    A pre-release observed version (``8.0.0-rc9``) is compared on its numeric part, so it matches a range
    that starts at the GA release: an approximation in the safe direction, flagged by the caller.
    """
    match = _SERVER_VERSION.fullmatch(version)
    if not match:
        return False
    numeric, suffix = match.group(1), (match.group(2) or "").lower()
    for listed in entry.get("versions") or []:
        text = str(listed).strip().lower()
        if "-" in text:
            if suffix and text == f"{numeric}-{suffix}":
                return True
        elif not suffix and _version(text) is not None and _compare(_version(text), _version(numeric)) == 0:
            return True
    spans = []
    for span in entry.get("affected") or []:
        if span.get("introduced_exclusive") and _version(span.get("introduced")) is not None \
                and _compare(_version(numeric), _version(span["introduced"])) == 0:
            continue  # versionStartExcluding: the start release itself is not affected
        spans.append(span)
    return _affected(numeric, spans)


def check_server(extract: dict, catalog: dict | None = None, fallback_version: str | None = None) -> dict:
    """Offline MongoDB server-version CVE review for one node (no network): every catalog entry whose
    NVD range contains the observed version is reported."""
    try:
        catalog = catalog if catalog is not None else load_server_catalog()
    except (OSError, ValueError) as exc:
        return {"mode": "offline_catalog", "status": "catalog_unavailable", "error": str(exc)[:200],
                "observed_server": None, "matches": [], "match_count": 0}
    version, source = server_version(extract, fallback_version)
    meta = {k: catalog.get(k) for k in ("catalog_name", "catalog_version", "refreshed_at", "refresh_owner",
                                        "coverage_note", "sources")}
    observed = {"version": version, "source": source}
    base = {"mode": "offline_catalog", "catalog": meta, "observed_server": observed,
            "matches": [], "match_count": 0}
    if not version:
        return {**base, "status": "server_version_unknown",
                "note": "No server version in this log (buildInfo is logged at start-up) and none configured."}
    if _MAJOR_MINOR.fullmatch(version):
        return {**base, "status": "server_version_incomplete",
                "note": f"Only the release line {version} is known; patch-level CVE ranges cannot be evaluated."}
    if not _SERVER_VERSION.fullmatch(version):
        return {**base, "status": "server_version_unparsable", "note": f"Unrecognized server version {version!r}."}
    matches = []
    prerelease = "-" in version
    for entry in catalog.get("entries") or []:
        if not entry.get("cve") or not entry.get("source") or not _server_affected(version, entry):
            continue
        match = {k: entry[k] for k in ("cve", "severity", "cvss", "cvss_score", "summary", "source", "affected",
                                       "versions", "platform_note", "scope_note", "published", "range_source")
                 if k in entry}
        fixed_in = sorted({s["fixed"] for s in entry.get("affected") or [] if s.get("fixed")
                           and _compare(_version(s["fixed"]), _version(version.split("-")[0])) > 0
                           and _version(s.get("introduced")) is not None
                           and _compare(_version(s["introduced"]), _version(version.split("-")[0])) <= 0},
                          key=lambda v: _version(v))
        match.update({"matched_version": version, "fixed_in": fixed_in[0] if fixed_in else None,
                      "assessment": "version-range match; feature use, edition/platform and exploitability unverified"
                                    + ("; pre-release build compared on its numeric version" if prerelease else "")})
        matches.append(match)
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "MODERATE": 2, "LOW": 3}
    matches.sort(key=lambda m: (order.get(str(m.get("severity")).upper(), 4), -(m.get("cvss_score") or 0), m["cve"]))
    upgrade = sorted({m["fixed_in"] for m in matches if m.get("fixed_in")}, key=lambda v: _version(v))
    return {**base, "status": _status(matches), "matches": matches, "match_count": len(matches),
            "highest_fixed_in": upgrade[-1] if upgrade else None,
            "highest_fixed_in_note": "Highest fix release among the matched ranges; confirm against the advisories (a later CVE may affect it)."}
