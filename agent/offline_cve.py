"""Offline driver-CVE matching; performs no network I/O."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .skills import diagnostic_skill_dir

CATALOG_RELATIVE_PATH = "references/offline-driver-cves.json"
_VERSION = re.compile(r"\d+(?:\.\d+)+")


def _version(value: Any) -> tuple[int, ...] | None:
    match = _VERSION.search(str(value or ""))
    return tuple(int(part) for part in match.group(0).split(".")) if match else None


def _compare(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    width = max(len(left), len(right))
    l, r = left + (0,) * (width - len(left)), right + (0,) * (width - len(right))
    return (l > r) - (l < r)


def _affected(version: str, ranges: list[dict]) -> bool:
    observed = _version(version)
    if not observed:
        return False
    for span in ranges:
        introduced, fixed = _version(span.get("introduced", "0")), _version(span.get("fixed"))
        if introduced and _compare(observed, introduced) < 0:
            continue
        if fixed and _compare(observed, fixed) >= 0:
            continue
        return True
    return False


def load_catalog(path: Path | None = None) -> dict:
    path = path or diagnostic_skill_dir() / CATALOG_RELATIVE_PATH
    with path.open(encoding="utf-8") as handle:
        catalog = json.load(handle)
    if catalog.get("$schema") != "mongodb-log-diagnostic.offline-driver-cves/v1":
        raise ValueError(f"Unsupported offline CVE catalog format: {path}")
    return catalog


def observed_drivers(extract: dict) -> list[dict[str, str]]:
    """Normalize the extractor's compatibility output into driver/version pairs."""
    drivers = (extract.get("driverCompatibility") or {}).get("distinct_compatible_drivers") or {}
    found: set[tuple[str, str]] = set()
    if isinstance(drivers, dict):
        for name, versions in drivers.items():
            if isinstance(versions, dict):
                found.update((str(name), str(version)) for version in versions)
            elif isinstance(versions, list):
                found.update((str(name), str(item.get("version"))) for item in versions if isinstance(item, dict) and item.get("version"))
    for item in (extract.get("driverCompatibility") or {}).get("incompatible_drivers") or []:
        if isinstance(item, dict) and item.get("driver_name") and item.get("driver_version"):
            found.add((str(item["driver_name"]), str(item["driver_version"])))
    return [{"driver": driver, "version": version} for driver, version in sorted(found)]


def check(extract: dict, catalog: dict | None = None) -> dict:
    catalog = catalog or load_catalog()
    matches = []
    for observed in observed_drivers(extract):
        for entry in catalog.get("entries", []):
            if entry.get("driver") == observed["driver"] and _affected(observed["version"], entry.get("affected", [])):
                matches.append({"driver": observed["driver"], "version": observed["version"], **{k: entry[k] for k in ("cve", "severity", "cvss_v3", "summary", "source", "affected") if k in entry}})
    return {
        "mode": "offline_catalog",
        "catalog": {k: catalog.get(k) for k in ("catalog_name", "catalog_version", "refreshed_at", "refresh_owner", "coverage_note", "sources")},
        "observed_drivers": observed_drivers(extract),
        "matches": matches,
        "match_count": len(matches),
        "status": "matches_found" if matches else "no_match_in_snapshot"
    }
