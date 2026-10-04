"""Conservative, offline driver/advisory matching. No network calls or app guessing."""
from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Any
from .skills import diagnostic_skill_dir

CATALOG_RELATIVE_PATH = "references/offline-driver-cves.json"
_VERSION = re.compile(r"^v?(\d+(?:\.\d+)+)(?:[-+].*)?$")


def _version(value: Any) -> tuple[int, ...] | None:
    match = _VERSION.fullmatch(str(value or "").strip())
    return tuple(map(int, match.group(1).split("."))) if match else None


def _compare(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    width = max(len(a), len(b))
    return ((a + (0,) * (width-len(a))) > (b + (0,) * (width-len(b)))) - ((a + (0,) * (width-len(a))) < (b + (0,) * (width-len(b))))


def _affected(version: str, ranges: list[dict]) -> bool:
    observed = _version(version)
    if observed is None:
        return False
    for span in ranges:
        introduced = _version(span.get("introduced"))
        fixed = _version(span.get("fixed"))
        # Fail closed on an incomplete or malformed affected interval.
        if introduced is None or fixed is None or _compare(introduced, fixed) >= 0:
            continue
        if _compare(observed, introduced) >= 0 and _compare(observed, fixed) < 0:
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
    """Keep observed application names tied to their driver/version, never node-wide."""
    compat = extract.get("driverCompatibility") or {}
    by_pair: dict[tuple[str, str], set[str]] = {}
    versions = compat.get("distinct_compatible_drivers") or {}
    if isinstance(versions, dict):
        for name, entries in versions.items():
            if isinstance(entries, dict):
                for version, details in entries.items():
                    key = (str(name), str(version))
                    by_pair.setdefault(key, set()).update(_names((details or {}).get("distinctAppNames")) if isinstance(details, dict) else [])
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
             "attribution": "observed in driver metadata; ownership and impact not established" if v else "not attributable from extraction"}
            for k, v in sorted(by_pair.items())]


def check(extract: dict, catalog: dict | None = None) -> dict:
    catalog = catalog if catalog is not None else load_catalog()
    observed = observed_drivers(extract)
    matches = []
    for driver in observed:
        for entry in catalog.get("entries", []):
            # Reviewed entries require a driver-specific advisory; generic product indexes
            # are insufficient evidence for a security claim.
            if (entry.get("driver") != driver["driver"] or not entry.get("source")
                    or not entry.get("reviewed") or not _affected(driver["version"], entry.get("affected") or [])):
                continue
            matches.append({**driver, **{k: entry[k] for k in ("cve", "severity", "summary", "source", "affected", "scope_note") if k in entry},
                            "assessment": "version-range match; component exposure and application ownership unverified"})
    return {"mode": "offline_catalog", "catalog": {k: catalog.get(k) for k in ("catalog_name", "catalog_version", "refreshed_at", "refresh_owner", "coverage_note", "sources")},
            "observed_drivers": observed, "matches": matches, "match_count": len(matches),
            "status": "matches_found" if matches else "no_match_in_snapshot"}
