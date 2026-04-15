#!/usr/bin/env python3
"""Assert evo-hq-cli, claude-plugin, and codex-plugin versions all match.

Runs in CI (and locally) to prevent a release where the CLI was bumped
but the plugin manifests were not (or vice versa). Claude Code and
Codex use the plugin manifest version to decide whether to refetch the
plugin -- if only pyproject.toml bumps, installed hosts never see the
new CLI.

Exit 0 on match, non-zero with a diagnostic on mismatch.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SOURCES = [
    ("plugins/evo/pyproject.toml", "pyproject.toml (evo-hq-cli)"),
    ("plugins/evo/src/evo/__init__.py", "__version__ (read by `evo --version`)"),
    ("plugins/evo/.claude-plugin/plugin.json", "Claude Code plugin manifest"),
    ("plugins/evo/.codex-plugin/plugin.json", "Codex plugin manifest"),
]


def read_pyproject_version(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    # Top-level [project] version. Use a regex to avoid a tomllib dep so
    # the script runs on any Python 3.8+.
    match = re.search(
        r'^\[project\].*?^version\s*=\s*"([^"]+)"',
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    if not match:
        raise RuntimeError(f"no [project] version in {path}")
    return match.group(1)


def read_json_version(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    if "version" not in data:
        raise RuntimeError(f"no version field in {path}")
    return data["version"]


def read_python_version(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
    if not match:
        raise RuntimeError(f"no __version__ assignment in {path}")
    return match.group(1)


def main() -> int:
    versions: list[tuple[str, str, str]] = []  # (relpath, label, version)
    for relpath, label in SOURCES:
        path = REPO_ROOT / relpath
        if not path.exists():
            print(f"ERROR: missing {relpath}", file=sys.stderr)
            return 2
        if path.suffix == ".toml":
            version = read_pyproject_version(path)
        elif path.suffix == ".json":
            version = read_json_version(path)
        elif path.suffix == ".py":
            version = read_python_version(path)
        else:
            raise RuntimeError(f"unknown file type for {path}")
        versions.append((relpath, label, version))

    distinct = {v for _, _, v in versions}
    if len(distinct) == 1:
        (only,) = distinct
        print(f"OK: all {len(versions)} sources report version {only}")
        return 0

    print("ERROR: version mismatch between plugin and CLI:", file=sys.stderr)
    width = max(len(rp) for rp, _, _ in versions)
    for relpath, label, version in versions:
        print(f"  {relpath:<{width}}  {version}  ({label})", file=sys.stderr)
    print(
        "\nBump all three together. Claude Code / Codex key off the plugin "
        "manifest version to decide whether to refetch the plugin -- bumping "
        "pyproject alone leaves installed hosts stuck on the old CLI.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
