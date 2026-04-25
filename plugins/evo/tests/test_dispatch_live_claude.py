"""Tier 2 live test — exercises real `claude -p` against real Anthropic API.

Skipped unless EVO_LIVE_TEST_CLAUDE=1. Real subprocess, real LLM calls.
Costs ~$0.10 per run with the default haiku-class model. No mocks.

What we verify:
  1. ensure_explorer spawns a claude -p subprocess, captures session_id,
     and persists .evo/explorers/<parent_id>.json with all required fields.
  2. A second ensure_explorer call within TTL reuses the record without
     spawning a new subprocess (created_at unchanged).
  3. Editing the subagent SKILL.md invalidates the cache and rebuilds.

We deliberately do NOT exercise dispatch_child here — that requires a real
benchmark, real worktree allocation, and the child running its iteration
loop, which inflates the test surface significantly. The explorer test
proves the fork-cache mechanism works end-to-end at the explorer layer;
fork-from-explorer cache hits are verified separately in the manual
/tmp/fork-test/ scripts in the dispatch-fork branch history.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from evo.core import init_workspace, set_host
from evo.dispatch import (
    ensure_explorer,
    explorer_record_path,
    subagent_skill_path,
)


pytestmark = pytest.mark.skipif(
    os.environ.get("EVO_LIVE_TEST_CLAUDE") != "1",
    reason="set EVO_LIVE_TEST_CLAUDE=1 to enable real claude -p calls",
)


def _claude_available() -> bool:
    return shutil.which(os.environ.get("EVO_CLAUDE_BIN", "claude")) is not None


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    if not _claude_available():
        pytest.skip("claude CLI not on PATH")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "initial"],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "bench.sh").write_text("echo score:1.0\n")
    init_workspace(
        tmp_path,
        target="bench.sh",
        benchmark="./bench.sh",
        metric="max",
        gate=None,
    )
    set_host(tmp_path, "claude-code")
    return tmp_path


def test_ensure_explorer_spawns_and_persists(workspace: Path):
    record = ensure_explorer(workspace, parent_id="root")
    assert record["host"] == "claude-code"
    assert record["session_id"]
    assert record["worktree_commit"]
    assert record["skill_hash"]
    assert record["ttl_expires_at"]

    # Persisted to disk
    rec_path = explorer_record_path(workspace, "root")
    assert rec_path.exists()
    on_disk = json.loads(rec_path.read_text())
    assert on_disk["session_id"] == record["session_id"]


def test_ensure_explorer_reuses_within_ttl(workspace: Path):
    rec1 = ensure_explorer(workspace, parent_id="root")
    rec2 = ensure_explorer(workspace, parent_id="root")
    # Same record returned -> same session_id and created_at
    assert rec2["session_id"] == rec1["session_id"]
    assert rec2["created_at"] == rec1["created_at"]


def test_ensure_explorer_rebuilds_when_skill_changes(workspace: Path):
    """Editing subagent/SKILL.md must invalidate every cached explorer."""
    rec1 = ensure_explorer(workspace, parent_id="root")

    # Mutate the skill file in place; restore in finally.
    skill = subagent_skill_path()
    original = skill.read_text(encoding="utf-8")
    try:
        skill.write_text(original + "\n<!-- test invalidation marker -->\n", encoding="utf-8")
        rec2 = ensure_explorer(workspace, parent_id="root")
        assert rec2["session_id"] != rec1["session_id"]
        assert rec2["skill_hash"] != rec1["skill_hash"]
    finally:
        skill.write_text(original, encoding="utf-8")
