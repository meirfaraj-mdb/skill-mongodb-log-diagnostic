#!/usr/bin/env python3
"""Overlay this wrapper and the shared pipeline onto an Atlas CLI scaffold."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tomllib
from pathlib import Path

DEPENDENCIES = [
    "agent-engine-sdk-langgraph==0.11.8",
    "langgraph>=1.0,<2.0",
    "langchain-aws>=0.2",
    "langchain-anthropic>=0.3",
    "boto3>=1.34",
    "pymongo>=4.6",
    "requests>=2.31",
    "google-cloud-storage>=2.14",
    "google-cloud-secret-manager>=2.18",
    "google-auth>=2.27",
    "google-genai>=1.0",
    "anthropic>=0.40",
]


def _find_toml_array_end(text: str, opening: int) -> int:
    depth = 0
    quote = None
    escaped = False
    comment = False
    for index in range(opening, len(text)):
        char = text[index]
        if comment:
            if char == "\n":
                comment = False
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char == "#":
            comment = True
        elif char in ('"', "'"):
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return index + 1
    raise ValueError("Unclosed dependencies array in pyproject.toml")


def add_dependencies(pyproject: Path) -> None:
    text = pyproject.read_text(encoding="utf-8")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid pyproject.toml: {exc}") from exc
    project = data.get("project")
    if not isinstance(project, dict):
        raise ValueError("Atlas scaffold pyproject.toml must contain a [project] table")
    existing = project.get("dependencies", [])
    if not isinstance(existing, list) or not all(isinstance(item, str) for item in existing):
        raise ValueError("[project].dependencies must be a TOML list of strings")
    merged = list(existing)
    for dependency in DEPENDENCIES:
        if dependency not in merged:
            merged.append(dependency)

    header = re.search(r"(?m)^\[project\]\s*$", text)
    if not header:
        raise ValueError("Could not locate [project] in pyproject.toml")
    next_section = re.search(r"(?m)^\[", text[header.end():])
    section_end = header.end() + next_section.start() if next_section else len(text)
    section = text[header.end():section_end]
    dependency_line = re.search(r"(?m)^dependencies\s*=\s*\[", section)
    rendered = "dependencies = [\n" + "".join(
        f"    {json.dumps(item)},\n" for item in merged
    ) + "]"
    if dependency_line:
        start = header.end() + dependency_line.start()
        opening = text.find("[", header.end() + dependency_line.start())
        end = _find_toml_array_end(text, opening)
        text = text[:start] + rendered + text[end:]
    else:
        insertion = header.end() + len(section)
        prefix = "" if text[:insertion].endswith("\n") else "\n"
        text = text[:insertion] + prefix + rendered + "\n" + text[insertion:]

    # Validate before writing, including duplicate/invalid TOML introduced above.
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Dependency update produced invalid TOML: {exc}") from exc
    pyproject.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True,
                        help="Existing diagnostic repository root containing agent/ and skills/")
    parser.add_argument("--workspace", type=Path, required=True,
                        help="Existing Python LangGraph workspace created by agentengine create")
    parser.add_argument("--entrypoint-file", required=True,
                        help="Relative path to the Python entrypoint file referenced by agent.yaml")
    args = parser.parse_args()

    wrapper_root = Path(__file__).resolve().parents[1]
    repo_root = args.repo_root.resolve()
    workspace = args.workspace.resolve()
    for required in (repo_root / "agent", repo_root / "skills"):
        if not required.is_dir():
            parser.error(f"Missing shared source directory: {required}")
    for required in (workspace / "agent.yaml", workspace / "pyproject.toml"):
        if not required.is_file():
            parser.error(f"Workspace is not an initialized Atlas agent workspace: {required}")

    entrypoint = Path(args.entrypoint_file)
    if entrypoint.is_absolute() or ".." in entrypoint.parts:
        parser.error("--entrypoint-file must be a path relative to --workspace")
    target = workspace / entrypoint
    target.parent.mkdir(parents=True, exist_ok=True)

    src = wrapper_root / "src" / "mongodb_log_diagnostic_atlas"
    if not (src / "main.py").is_file() or not (src / "runtime.py").is_file():
        parser.error(f"Wrapper source missing under {src}")

    # Do not copy .env, credentials, venvs, .git, raw logs, or build outputs.
    ignore = shutil.ignore_patterns(
        ".env", ".env.*", "*.env", ".git", ".venv", "venv", "__pycache__",
        "*.pyc", ".DS_Store", "*.log", "*.gz", "*.jsonl", "*.bson",
    )
    for dirname in ("agent", "skills"):
        shutil.copytree(repo_root / dirname, workspace / dirname,
                        dirs_exist_ok=True, ignore=ignore)

    package_dir = target.parent
    (package_dir / "__init__.py").touch(exist_ok=True)
    shutil.copy2(src / "main.py", target)
    shutil.copy2(src / "runtime.py", package_dir / "runtime.py")
    shutil.copy2(wrapper_root / "env.atlas.example", workspace / "env.atlas.example")
    add_dependencies(workspace / "pyproject.toml")

    print(f"Installed Atlas LangGraph wrapper into: {workspace}")
    print(f"Entrypoint source: {target.relative_to(workspace)}")
    print("Shared source copied: agent/ and skills/ (credential/env/log files excluded)")
    print("Next: run agentengine dev up, configure platform secrets/env, then agentengine deploy --auto")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
