"""Locate and import bundled skills.

Layout (container):  /opt/skills/<skill-name>/{SKILL.md,scripts/,references/}
Layout (repo/dev):   <project>/skills/<skill-name>/...
The diagnostic skill is vendored next to the other skills.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

_PROJECT_SKILLS = Path(__file__).resolve().parents[1] / "skills"


def skills_dir() -> Path:
    env = os.environ.get("SKILLS_DIR")
    if env:
        return Path(env)
    return Path("/opt/skills") if Path("/opt/skills").is_dir() else _PROJECT_SKILLS


def diagnostic_skill_dir() -> Path:
    env = os.environ.get("DIAG_SKILL_DIR") or os.environ.get("SKILL_DIR")
    return Path(env) if env else skills_dir() / "mongodb-log-diagnostic"


def load_skill_module(skill: str, module: str):
    """Import scripts/<module>.py from a skill without installing it."""
    name = f"skill_{skill.replace('-', '_')}_{module}"
    if name in sys.modules:
        return sys.modules[name]
    path = skills_dir() / skill / "scripts" / f"{module}.py"
    if not path.is_file():
        raise FileNotFoundError(f"Skill script not found: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def atlas_logs():
    return load_skill_module("mongodb-atlas-logs", "atlas_logs")


def aws_storage():
    return load_skill_module("aws-storage", "aws_storage")


def gcp_storage():
    return load_skill_module("gcp-storage", "gcp_storage")


def observability():
    return load_skill_module("mongodb-observability", "observability")
