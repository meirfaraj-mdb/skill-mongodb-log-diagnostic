#!/usr/bin/env python3
"""Regression tests for per-version MongoDB driver compatibility metadata."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from driver_compatibility import extract_driver_compatibility  # noqa: E402


def event(client: str, app: str, platform: str, driver: str, version: str) -> dict:
    raw = {
        "t": {"$date": "2026-09-01T10:06:07.092+00:00"},
        "s": "I",
        "c": "ACCESS",
        "id": 5286306,
        "ctx": "conn-test",
        "msg": "Successfully authenticated",
        "attr": {
            "client": client,
            "doc": {
                "application": {"name": app},
                "driver": {"name": driver, "version": version},
                "os": {"type": "Linux", "name": "Ubuntu", "architecture": "arm64", "version": "24.04"},
                "platform": platform,
            },
        },
    }
    return {"raw_line": json.dumps(raw), "timestamp": "2026-09-01T10:06:07.092Z", "source_file": "test.json"}


def server_event() -> dict:
    raw = {"msg": "buildInfo", "buildInfo": {"version": "8.0.0"}}
    return {"raw_line": json.dumps(raw), "timestamp": "2026-09-01T10:00:00Z", "source_file": "test.json"}


def multi_driver_event() -> dict:
    raw = {
        "t": {"$date": "2026-09-01T10:06:08.092+00:00"},
        "s": "I", "c": "ACCESS", "msg": "client metadata",
        "attr": {"client": "192.0.2.10:27017", "clients": [
            {"application": {"name": "multi-csharp"}, "driver": {"name": "mongo-csharp-driver", "version": "3.10.0"}, "platform": ".NET 10"},
            {"application": {"name": "multi-go"}, "driver": {"name": "mongo-go-driver", "version": "2.1.0"}, "platform": "go1.24"},
        ]},
    }
    return {"raw_line": json.dumps(raw), "timestamp": "2026-09-01T10:06:08.092Z", "source_file": "test.json"}


def main() -> None:
    # count is source-log records with driver metadata, not driver observations or incompatibility groups.
    count_check = extract_driver_compatibility([
        server_event(),
        multi_driver_event(),
        event("192.0.2.11:27017", "single", ".NET 10", "mongo-csharp-driver", "3.10.0"),
        {"raw_line": json.dumps({"msg": "ordinary log line"}), "timestamp": "2026-09-01T10:06:09Z"},
    ])
    assert count_check["count"] == 2, count_check
    assert count_check["driver_log_count"] == 2, count_check
    assert count_check["observed_application_connections"] == 3, count_check
    assert count_check["incompatible_count"] == 0, count_check

    compatible = extract_driver_compatibility([
        server_event(),
        event("45.167.153.189:17216", "myApplication", ".NET 10.0.3", "mongo-csharp-driver", "3.10.0"),
        event("45.167.153.190:17216", "anotherApplication", ".NET 10.0.4", "mongo-csharp-driver", "3.10.0"),
    ])
    assert compatible["count"] == 2, compatible
    assert compatible["incompatible_count"] == 0, compatible
    version = compatible["distinct_compatible_drivers"]["mongo-csharp-driver"]["3.10.0"]
    assert version == {
        "distinctIps": ["45.167.153.189", "45.167.153.190"],
        "distinctAppNames": ["anotherApplication", "myApplication"],
        "distinctPlatforms": [".NET 10.0.3", ".NET 10.0.4"],
    }, version

    incompatible = extract_driver_compatibility([
        server_event(),
        event("10.0.0.1:27017", "legacyApp", ".NET 6.0", "mongo-csharp-driver", "2.20.0"),
        event("10.0.0.2:27017", "legacyApp2", ".NET 7.0", "mongo-csharp-driver", "2.20.0"),
    ])
    assert incompatible["count"] == 2, incompatible
    assert incompatible["incompatible_count"] == 1, incompatible
    item = incompatible["incompatible_drivers"][0]
    assert item["driver_version"] == "2.20.0"
    assert item["distinctIps"] == ["10.0.0.1", "10.0.0.2"]
    assert item["distinctAppNames"] == ["legacyApp", "legacyApp2"]
    assert item["distinctPlatforms"] == [".NET 6.0", ".NET 7.0"]
    print("driver compatibility metadata tests: PASS")


if __name__ == "__main__":
    main()
