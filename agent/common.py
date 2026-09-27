"""Shared configuration, S3/GCS layout and date helpers (cloud-neutral)."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

logger = logging.getLogger("mongodb_log_agent")
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger.setLevel(logging.INFO)

WORK_DIR = Path(os.environ.get("WORK_DIR", "/tmp"))
EXTRACT_FILES = ("extractionOccurence.json", "extractionshort.json", "handoff.md")


@dataclass(frozen=True)
class Layout:
    """Object key layout -- identical on S3 and GCS, and identical to the original lambda for raw logs."""
    prefix: str

    @classmethod
    def from_config(cls, config: dict) -> "Layout":
        return cls(config["prefix"].strip("/"))

    def day(self, log_date: str) -> str:
        return f"{self.prefix}/{log_date}"

    def raw_log(self, log_date: str, host_dir: str, log_name: str) -> str:          # stage 1
        return f"{self.day(log_date)}/{host_dir}/{log_name}.gz"

    def extract(self, log_date: str, host_dir: str, log_name: str, filename: str) -> str:  # stage 2
        return f"{self.day(log_date)}/{host_dir}/extract/{log_name}/{filename}"

    def report(self, log_date: str, filename: str) -> str:                          # stage 3
        return f"{self.day(log_date)}/reports/{filename}"


def shift(log_date: str, days: int) -> str:
    return (date.fromisoformat(log_date) + timedelta(days=days)).isoformat()
