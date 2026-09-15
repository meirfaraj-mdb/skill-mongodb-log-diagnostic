#!/usr/bin/env python3
"""Hatchet-derived MongoDB application-driver compatibility extraction."""
from __future__ import annotations

import json
import re
from collections import OrderedDict
from typing import Any, Iterable

# Hatchet's drivers.json compatibility matrix, compared at major.minor level.
DRIVER_MATRIX: dict[str, dict[str, list[str]]] = {
    "4.4": {"mongoc": ["1.17", "1.18", "1.19", "1.20", "1.21", "1.22", "1.23", "1.24"], "mongo-csharp-driver": ["2.11", "2.12", "2.13", "2.14", "2.15", "2.16", "2.17", "2.18", "2.19", "2.20", "2.21"], "mongo-go-driver": ["1.4", "1.5", "1.6", "1.7", "1.8", "1.9", "1.10", "1.11", "1.12"], "mongo-java-driver": ["4.1", "4.2", "4.3", "4.4", "4.5", "4.6", "4.7", "4.8", "4.9", "4.10"], "nodejs": ["3.6", "3.7", "4.0", "4.1", "4.2", "4.3", "4.4", "4.5", "4.6", "4.7", "4.8", "4.9", "4.10", "4.11", "4.12", "4.13", "4.14", "5.0", "5.1", "5.2", "5.3", "5.4", "5.5", "5.6", "5.7"], "PyMongo": ["3.11", "3.12", "3.13", "4.0", "4.1", "4.2", "4.3", "4.4"]},
    "5.0": {"mongoc": ["1.18", "1.19", "1.20", "1.21", "1.22", "1.23", "1.24"], "mongo-csharp-driver": ["2.13", "2.14", "2.15", "2.16", "2.17", "2.18", "2.19", "2.20", "2.21"], "mongo-go-driver": ["1.6", "1.7", "1.8", "1.9", "1.10", "1.11", "1.12"], "mongo-java-driver": ["4.3", "4.4", "4.5", "4.6", "4.7", "4.8", "4.9", "4.10"], "nodejs": ["3.7", "4.0", "4.1", "4.2", "4.3", "4.4", "4.5", "4.6", "4.7", "4.8", "4.9", "4.10", "4.11", "4.12", "4.13", "4.14", "5.0", "5.1", "5.2", "5.3", "5.4", "5.5", "5.6", "5.7"], "PyMongo": ["3.12", "3.13", "4.0", "4.1", "4.2", "4.3", "4.4"]},
    "6.0": {"mongoc": ["1.22", "1.23", "1.24"], "mongo-csharp-driver": ["2.16", "2.17", "2.18", "2.19", "2.20", "2.21"], "mongo-go-driver": ["1.10", "1.11", "1.12"], "mongo-java-driver": ["4.7", "4.8", "4.9", "4.10"], "nodejs": ["4.8", "4.9", "4.10", "4.11", "4.12", "4.13", "4.14", "5.0", "5.1", "5.2", "5.3", "5.4", "5.5", "5.6", "5.7"], "PyMongo": ["4.2", "4.3", "4.4"]},
    "7.0": {"mongoc": ["1.24"], "mongo-csharp-driver": ["2.20", "2.21"], "mongo-go-driver": ["1.12"], "mongo-java-driver": ["4.10"], "nodejs": ["5.7"], "PyMongo": ["4.4"]},
    "8.0": {"mongoc": ["1.28"], "mongo-csharp-driver": ["2.29"], "mongo-go-driver": ["2.1"], "mongo-java-driver": ["5.2", "5.5"], "nodejs": ["6.9", "6.10", "6.17"], "PyMongo": ["4.9"]},
}

INTERNAL_DRIVER_PREFIX = "NetworkInterfaceTL"
INTERNAL_DRIVER_NAME = "MongoDB Internal Client"
IGNORED_WRAPPER_DRIVERS = {"mongoose", "motor", "odm"}
VERSION_RE = re.compile(r"(?<!\d)(\d+\.\d+)(?:\.\d+)?(?:[-+][0-9A-Za-z.-]+)?(?!\d)")


def _text(value: Any) -> str | None:
    value = str(value).strip() if value is not None else ""
    return value or None


def _version_part(value: Any) -> str | None:
    text = _text(value)
    return text.split("|", 1)[0].strip() if text else None


def _major_minor(value: Any) -> str | None:
    match = VERSION_RE.search(_version_part(value) or "")
    return match.group(1) if match else None


def _version_key(value: Any) -> tuple[int, int] | None:
    version = _major_minor(value)
    if not version:
        return None
    major, minor = version.split(".")
    return int(major), int(minor)


def _canonical_driver(value: Any) -> str | None:
    name = _text(value)
    if not name:
        return None
    # Hatchet driver strings may contain details after '|'; the name ends before it.
    name = name.split("|", 1)[0].strip()
    aliases = {"pymongo": "PyMongo", "node": "nodejs", "node.js": "nodejs", "libmongoc": "mongoc", "c#": "mongo-csharp-driver", ".net": "mongo-csharp-driver", "java": "mongo-java-driver"}
    return aliases.get(name.lower(), name)


def _is_internal(name: str, version: str | None) -> bool:
    return name.startswith(INTERNAL_DRIVER_PREFIX) or name == INTERNAL_DRIVER_NAME or (name == "mongo-go-driver" and bool(version) and version.endswith("-cloud"))


def _is_wrapper(name: str) -> bool:
    return name.lower() in IGNORED_WRAPPER_DRIVERS


def _ip(value: Any) -> str | None:
    value = _text(value)
    if not value:
        return None
    if value.startswith("[") and "]" in value:
        return value[1:value.index("]")]
    if value.count(":") == 1 and value.rsplit(":", 1)[1].isdigit():
        return value.rsplit(":", 1)[0]
    return value


def _walk(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _remote(obj: dict[str, Any]) -> str | None:
    for mapping in _walk(obj):
        for key in ("remote", "client", "ip", "clientIp", "clientIP", "address"):
            if key in mapping:
                found = _ip(mapping[key])
                if found:
                    return found
    return None


def _connection_details(obj: dict[str, Any]) -> tuple[str | None, set[str], set[str]]:
    """Return remote IP, application names, and platform names from client metadata."""
    remote = _remote(obj)
    application_names: set[str] = set()
    platforms: set[str] = set()
    for mapping in _walk(obj):
        application = mapping.get("application")
        if isinstance(application, dict):
            name = _text(application.get("name"))
            if name:
                application_names.add(name)
        for key in ("appName", "applicationName"):
            name = _text(mapping.get(key))
            if name:
                application_names.add(name)
        platform = _text(mapping.get("platform"))
        if platform:
            platforms.add(platform)
    return remote, application_names, platforms


def _driver_records(obj: dict[str, Any]) -> list[tuple[str, str | None]]:
    found: list[tuple[str, str | None]] = []
    for mapping in _walk(obj):
        value = mapping.get("driver") or mapping.get("clientDriver")
        if isinstance(value, dict):
            raw_name = _text(value.get("name") or value.get("driverName"))
            parts = raw_name.split("|") if raw_name else []
            name = _canonical_driver(parts[0] if parts else raw_name)
            version = _version_part(value.get("version") or value.get("driverVersion")) or (_version_part(parts[1]) if len(parts) > 1 else None)
            if name: found.append((name, version))
        elif value is not None:
            parts = _text(value).split("|")
            name = _canonical_driver(parts[0])
            version = _version_part(mapping.get("version") or mapping.get("driverVersion")) or (_version_part(parts[1]) if len(parts) > 1 else None)
            if name: found.append((name, version))
        else:
            name = _canonical_driver(mapping.get("driverName") or mapping.get("clientDriverName"))
            if name: found.append((name, _version_part(mapping.get("driverVersion") or mapping.get("clientDriverVersion") or mapping.get("version"))))
    unique: OrderedDict[tuple[str, str | None], None] = OrderedDict()
    for item in found:
        unique[item] = None
    return list(unique)


def _server_version(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        raw = event.get("raw_line")
        if not isinstance(raw, str):
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for mapping in _walk(obj):
            build = mapping.get("buildInfo")
            if isinstance(build, dict):
                version = _major_minor(build.get("version"))
                if version:
                    return version
    return None


class DriverCompatibilityAccumulator:
    """Stream driver observations without retaining raw log events."""

    def __init__(self) -> None:
        self.mongodb_version: str | None = None
        self.observations: dict[tuple[str, str | None], dict[str, Any]] = {}
        self.excluded_internal = 0
        self.observed_application = 0
        self.driver_log_count = 0

    def add(self, event: dict[str, Any]) -> None:
        obj = event.get("_driver_obj")
        if not isinstance(obj, dict):
            raw = event.get("raw_line")
            if not isinstance(raw, str):
                return
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                return
        if not self.mongodb_version:
            for mapping in _walk(obj):
                build = mapping.get("buildInfo")
                if isinstance(build, dict):
                    self.mongodb_version = _major_minor(build.get("version"))
                    if self.mongodb_version:
                        break
        remote, application_names, platforms = _connection_details(obj)
        driver_records = _driver_records(obj)
        if driver_records:
            # Count source log records, not incompatible version groups.
            self.driver_log_count += 1
        for driver_name, raw_version in driver_records:
            if _is_wrapper(driver_name):
                continue
            if _is_internal(driver_name, raw_version):
                self.excluded_internal += 1
                continue
            self.observed_application += 1
            key = (driver_name, _version_part(raw_version))
            item = self.observations.setdefault(key, {
                "occurrences": 0,
                "ips": set(),
                "application_names": set(),
                "platforms": set(),
                "first_seen": event.get("timestamp"),
                "last_seen": event.get("timestamp"),
                "source_files": set(),
            })
            item["occurrences"] += 1
            if remote:
                item["ips"].add(remote)
            item["application_names"].update(application_names)
            item["platforms"].update(platforms)
            timestamp = event.get("timestamp")
            if timestamp and (not item["first_seen"] or timestamp < item["first_seen"]):
                item["first_seen"] = timestamp
            if timestamp and (not item["last_seen"] or timestamp > item["last_seen"]):
                item["last_seen"] = timestamp
            source = _text(event.get("source_file"))
            if source:
                item["source_files"].add(source)

    def finish(self) -> dict[str, Any]:
        grouped: OrderedDict[tuple[str, str | None, str], dict[str, Any]] = OrderedDict()
        compatible_versions: dict[str, dict[str, dict[str, set[str]]]] = {}
        for (driver_name, normalized_version), observation in sorted(self.observations.items(), key=lambda item: (item[0][0], str(item[0][1]))):
            driver_version = _major_minor(normalized_version)
            minimum_version = (DRIVER_MATRIX.get(self.mongodb_version or "", {}).get(driver_name) or [None])[0]
            actual_key = _version_key(normalized_version)
            minimum_key = _version_key(minimum_version)
            if not self.mongodb_version or not driver_version or not normalized_version or minimum_key is None or actual_key is None:
                continue
            metadata = {
                "distinctIps": sorted(observation["ips"]),
                "distinctAppNames": sorted(observation["application_names"]),
                "distinctPlatforms": sorted(observation["platforms"]),
            }
            if actual_key >= minimum_key:
                compatible_versions.setdefault(driver_name, {})[normalized_version] = metadata
                continue
            key = (driver_name, normalized_version, self.mongodb_version)
            grouped[key] = {
                "driver_name": driver_name,
                "driver_version": normalized_version,
                "mongodb_version": self.mongodb_version,
                "ips": metadata["distinctIps"],
                "ip_count": len(metadata["distinctIps"]),
                **metadata,
                "occurrences": observation["occurrences"],
                "first_seen": observation["first_seen"],
                "last_seen": observation["last_seen"],
                "source_files": sorted(observation["source_files"]),
                "reason": f"MongoDB {self.mongodb_version} requires driver {driver_name} major.minor >= {minimum_version}; observed {driver_version}",
            }
        ips_by_version: dict[str, dict[str, list[str]]] = {}
        for item in grouped.values():
            ips_by_version.setdefault(item["driver_name"], {})[item["driver_version"] or "unknown"] = item["ips"]
        return {
            "status": "found" if grouped else ("unknown_server_version" if not self.mongodb_version else "none_detected"),
            "mongodb_version": self.mongodb_version,
            "incompatible_drivers": list(grouped.values()),
            "count": self.driver_log_count,
            "driver_log_count": self.driver_log_count,
            "incompatible_count": len(grouped),
            "distinct_incompatible_ips": ips_by_version,
            "distinct_incompatible_ip_count": len({ip for item in grouped.values() for ip in item["ips"]}),
            "distinct_compatible_drivers": {
                name: {version: details for version, details in sorted(versions.items())}
                for name, versions in sorted(compatible_versions.items())
            },
            "observed_application_connections": self.observed_application,
            "excluded_internal_connections": self.excluded_internal,
            "compatibility_manifest_versions": sorted(DRIVER_MATRIX),
        }


def extract_driver_compatibility(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    accumulator = DriverCompatibilityAccumulator()
    for event in events:
        accumulator.add(event)
    return accumulator.finish()
