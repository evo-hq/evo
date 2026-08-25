"""Lineage-forked children must carry a Compact Instructions block (#27).

A lineage fork inherits the parent experiment's session, so the worker
protocol reaches the child only through the cached transcript. On long
ancestor chains Claude Code auto-compacts the older portion and can
summarize the protocol away. The existing lineage prompt only *hints*
("re-read it if you can't see it"); this pins the load-bearing context as
preserve-on-summary so compaction keeps it.

Fresh (non-lineage) children get their protocol in the current turn and do
not inherit a long transcript, so they must NOT carry the block -- keeping
the contrast is what these tests guard.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))

from evo.dispatch import render_execute_prompt


def _render(lineage: bool) -> str:
    return render_execute_prompt(
        exp_id="exp_0009",
        worktree_path=Path("/tmp/wt"),
        parent_id="exp_0004",
        brief="widen search beam",
        budget=3,
        lineage=lineage,
    )


def test_lineage_prompt_includes_compact_instructions_header():
    out = _render(lineage=True)
    assert "Compact Instructions" in out
    assert "Preserve when summarizing" in out


def test_lineage_prompt_pins_load_bearing_context():
    """The block must name the specific things that must survive a summary:
    the experiment identity, that the worker protocol / `evo run` is
    terminal, and gate semantics."""
    out = _render(lineage=True).lower()
    assert "exp_id" in out
    assert "hypothesis" in out
    assert "parent_commit" in out
    assert "worker protocol" in out
    assert "evo run" in out and "terminal" in out
    assert "gate" in out


def test_non_lineage_prompt_omits_compact_instructions():
    out = _render(lineage=False)
    assert "Compact Instructions" not in out
