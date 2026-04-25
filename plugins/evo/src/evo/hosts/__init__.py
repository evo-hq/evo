"""Per-host spawn handlers for `evo dispatch`.

Each handler module exposes:
  * ``spawn_explorer(root, parent_id, parent_worktree, parent_commit,
    explore_context) -> dict`` — a fresh explorer record (NOT yet written
    to disk; the orchestrator persists it after validating).
  * ``spawn_child(root, explorer_record, exp_id, worktree_path, parent_id,
    brief, budget, job_dir) -> dict`` — runs one fork and returns a
    summary; per-host details encapsulated.

Only hosts with a usable fork primitive get a handler. ``codex``,
``opencode`` (1.0.207), ``openclaw``, ``hermes``, and ``generic`` use
their host's native parallel-Task primitive instead of evo dispatch — see
``plugins/evo/skills/optimize/SKILL.md``.
"""

from __future__ import annotations

from . import claude_fork

HOST_HANDLERS = {
    "claude-code": claude_fork,
}

__all__ = ["HOST_HANDLERS", "claude_fork"]
