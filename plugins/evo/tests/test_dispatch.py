"""Tier 1 tests for evo.dispatch — pure logic and orchestration error paths.

No mocks anywhere. Workspaces are real (git init in tmpdir + evo.init_workspace).
Subprocesses for the pid-liveness state machine are real `python -c "..."`
processes, not fake claude binaries — they exist to exercise the lifecycle
code, not simulate an LLM.

Live tests against real claude live in test_dispatch_live_claude.py and are
gated by EVO_LIVE_TEST_CLAUDE=1.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from evo.core import init_workspace, set_host, get_host
from evo.dispatch import (
    DispatchNotSupportedError,
    EXECUTE_USER_PROMPT_TEMPLATE,
    EXPLORE_USER_PROMPT_TEMPLATE,
    ensure_explorer,
    explorer_is_valid,
    explorer_record_path,
    hash_file,
    hash_text,
    render_execute_prompt,
    render_explore_prompt,
    subagent_skill_hash,
    subagent_skill_path,
    utc_iso_in,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Real evo workspace in a real git repo. No fakes."""
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
    return tmp_path


def _baseline_record() -> dict:
    return {
        "parent_id": "exp_0003",
        "session_id": "sid-1",
        "host": "claude-code",
        "worktree_commit": "abc",
        "skill_hash": "sha-skill",
        "explore_context_hash": "sha-ctx",
        "ttl_expires_at": utc_iso_in(60),
    }


# ---------------------------------------------------------------------------
# explorer_is_valid
# ---------------------------------------------------------------------------


def _check(record, **kwargs):
    return explorer_is_valid(
        record,
        parent_commit=kwargs.get("parent_commit", "abc"),
        skill_hash=kwargs.get("skill_hash", "sha-skill"),
        explore_context_hash=kwargs.get("explore_context_hash", "sha-ctx"),
        current_host=kwargs.get("current_host", "claude-code"),
    )


def test_valid_baseline():
    valid, reason = _check(_baseline_record())
    assert valid is True
    assert reason == ""


def test_host_mismatch():
    valid, reason = _check(_baseline_record(), current_host="codex")
    assert valid is False
    assert "host_mismatch" in reason


def test_commit_drift():
    rec = _baseline_record()
    rec["worktree_commit"] = "xyz"
    valid, reason = _check(rec)
    assert valid is False
    assert reason == "parent_commit_drift"


def test_skill_changed():
    rec = _baseline_record()
    rec["skill_hash"] = "sha-skill-new"
    valid, reason = _check(rec)
    assert valid is False
    assert reason == "skill_md_changed"


def test_context_changed():
    valid, reason = _check(_baseline_record(), explore_context_hash="sha-ctx-new")
    assert valid is False
    assert reason == "explore_context_changed"


def test_no_new_context_reuses_record():
    """Empty new context falls back to whatever was baked in -> reuse OK."""
    valid, reason = _check(_baseline_record(), explore_context_hash="")
    assert valid is True
    assert reason == ""


def test_ttl_expired():
    rec = _baseline_record()
    rec["ttl_expires_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=60)
    ).isoformat(timespec="seconds")
    valid, reason = _check(rec)
    assert valid is False
    assert reason == "ttl_expired"


def test_ttl_unparseable():
    rec = _baseline_record()
    rec["ttl_expires_at"] = "not-a-date"
    valid, reason = _check(rec)
    assert valid is False
    assert reason == "ttl_unset_or_unparseable"


def test_ttl_missing():
    rec = _baseline_record()
    rec["ttl_expires_at"] = None
    valid, reason = _check(rec)
    assert valid is False
    assert reason == "ttl_unset_or_unparseable"


# ---------------------------------------------------------------------------
# hashing
# ---------------------------------------------------------------------------


def test_hash_text_deterministic():
    assert hash_text("abc") == hash_text("abc")
    assert hash_text("abc") != hash_text("abd")


def test_hash_text_empty_returns_empty():
    assert hash_text("") == ""
    assert hash_text(None) == ""


def test_hash_file_missing_returns_empty(tmp_path: Path):
    assert hash_file(tmp_path / "nope") == ""


def test_hash_file_changes_with_content(tmp_path: Path):
    p = tmp_path / "x"
    p.write_text("a")
    h1 = hash_file(p)
    p.write_text("b")
    h2 = hash_file(p)
    assert h1 != h2 != ""


def test_subagent_skill_hash_resolves():
    """Skill file should exist when running from the dev install layout."""
    assert subagent_skill_path().exists()
    assert subagent_skill_hash() != ""


# ---------------------------------------------------------------------------
# prompt rendering
# ---------------------------------------------------------------------------


def test_render_explore_prompt_no_context():
    out = render_explore_prompt(
        skill_path=Path("/path/to/SKILL.md"),
        worktree_path=Path("/tmp/wt"),
        parent_id="exp_0003",
        explore_context=None,
    )
    # Must mention the skill path verbatim (agent will Read it)
    assert "/path/to/SKILL.md" in out
    assert "/tmp/wt" in out
    assert "exp_0003" in out
    # The orchestrator-focus block should NOT appear
    assert "Orchestrator focus" not in out
    # Stop signal
    assert "ready" in out


def test_render_explore_prompt_with_context():
    out = render_explore_prompt(
        skill_path=Path("/path/to/SKILL.md"),
        worktree_path=Path("/tmp/wt"),
        parent_id="exp_0003",
        explore_context="Round focus: retry behavior.\nFiles to skip: utils/.",
    )
    assert "Orchestrator focus" in out
    assert "retry behavior" in out
    assert "Files to skip" in out
    # Multiline hint must be indented to read cleanly
    assert "\n  Round focus" in out
    assert "\n  Files to skip" in out


def test_render_execute_prompt():
    out = render_execute_prompt(
        exp_id="exp_0007",
        worktree_path=Path("/tmp/wt"),
        parent_id="exp_0003",
        brief="try retry-loop, cap 2",
        budget=3,
    )
    assert "EXECUTE phase" in out
    assert "exp_0007" in out
    assert "exp_0003" in out
    assert "try retry-loop, cap 2" in out
    assert "Budget: 3" in out
    assert "Follow the protocol you loaded earlier" in out


# ---------------------------------------------------------------------------
# Orchestration error paths — real workspace, no subprocess to claude
# ---------------------------------------------------------------------------


def test_dispatch_errors_when_no_host(workspace: Path):
    with pytest.raises(DispatchNotSupportedError) as exc:
        ensure_explorer(workspace, parent_id="root")
    assert "no host recorded" in str(exc.value).lower()


def test_dispatch_errors_when_host_unsupported(workspace: Path):
    set_host(workspace, "codex")
    with pytest.raises(DispatchNotSupportedError) as exc:
        ensure_explorer(workspace, parent_id="root")
    assert "host=codex" in str(exc.value)
    # Guidance must point at the alternative
    assert "parallel-Task" in str(exc.value)


def test_dispatch_errors_on_unknown_parent(workspace: Path):
    set_host(workspace, "claude-code")
    with pytest.raises(RuntimeError) as exc:
        ensure_explorer(workspace, parent_id="exp_9999")
    # Should NOT be DispatchNotSupportedError — different error class
    assert not isinstance(exc.value, DispatchNotSupportedError)
    assert "unknown parent" in str(exc.value)


def test_explorer_record_path_and_dir(workspace: Path):
    """Path helpers point at the right place; dir is created lazily by writers."""
    p = explorer_record_path(workspace, "exp_0003")
    assert p == workspace / ".evo" / "explorers" / "exp_0003.json"
    # Dir doesn't exist until something writes — that's fine.
    assert p.parent.parent.exists()  # .evo/


# ---------------------------------------------------------------------------
# pid liveness state machine — uses a real Python subprocess, NOT a mock
# ---------------------------------------------------------------------------


def test_is_pid_alive_with_real_subprocess():
    """Spawn a real cheap subprocess; verify _is_pid_alive flips correctly."""
    from evo.cli import _is_pid_alive

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.5)"],
    )
    try:
        assert _is_pid_alive(proc.pid) is True
        proc.wait(timeout=5)
        # On macOS/Linux a wait()'d zombie may briefly still answer kill 0;
        # poll a short while to allow the kernel to reap.
        for _ in range(20):
            if not _is_pid_alive(proc.pid):
                break
            time.sleep(0.05)
        assert _is_pid_alive(proc.pid) is False
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_is_pid_alive_zero_and_negative():
    from evo.cli import _is_pid_alive
    assert _is_pid_alive(0) is False
    assert _is_pid_alive(-1) is False
