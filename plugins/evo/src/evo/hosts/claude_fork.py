"""Claude Code host module: explorer + fork-session children via `claude -p`.

This module owns the actual subprocess invocations. Cache validation,
record schema, and prompt rendering live in `evo.dispatch`. The orchestrator
in `evo.dispatch.dispatch_child` calls `spawn_explorer` / `spawn_child`
through the registry in `evo.hosts`.

Verified mechanism (see /tmp/fork-test/ in the dispatch-fork branch history):
``claude -p --resume <SID> --fork-session`` cache-reads the entire forked
transcript on Anthropic's server, ~99% prefix reuse on transcripts ≥30k
tokens.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from ..core import utc_now
from ..dispatch import (
    DEFAULT_TTL_SECONDS,
    hash_text,
    render_execute_prompt,
    render_explore_prompt,
    subagent_skill_hash,
    subagent_skill_path,
    utc_iso_in,
)

CLAUDE_BIN = os.environ.get("EVO_CLAUDE_BIN", "claude")
DEFAULT_MODEL = os.environ.get("EVO_DISPATCH_MODEL", "")  # empty → claude default
EXPLORER_TIMEOUT_SECONDS = int(os.environ.get("EVO_EXPLORER_TIMEOUT", "900"))
HOST_NAME = "claude-code"


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------


def _parse_events(stdout: str) -> list[dict[str, Any]]:
    """`claude -p --output-format json` emits a JSON array of events. Older
    versions emit JSONL. Try both."""
    stdout = stdout.strip()
    if not stdout:
        return []
    # Single JSON value (array or object)
    try:
        parsed = json.loads(stdout)
        if isinstance(parsed, list):
            return [e for e in parsed if isinstance(e, dict)]
        if isinstance(parsed, dict):
            return [parsed]
    except json.JSONDecodeError:
        pass
    # JSONL
    out: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
            if isinstance(ev, dict):
                out.append(ev)
        except json.JSONDecodeError:
            pass
    return out


def _extract_session_id(events: list[dict[str, Any]]) -> str | None:
    """The init event carries session_id. The result event also carries it
    as a fallback. Walk both."""
    for ev in events:
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            sid = ev.get("session_id")
            if sid:
                return sid
    for ev in events:
        if ev.get("type") == "result":
            sid = ev.get("session_id")
            if sid:
                return sid
    return None


def _extract_usage(events: list[dict[str, Any]]) -> dict[str, int]:
    """Pull cache_read / cache_write / input / output token counts from the
    result event. Used for telemetry; not load-bearing."""
    for ev in events:
        if ev.get("type") == "result":
            u = ev.get("usage", {})
            return {
                "input_tokens": int(u.get("input_tokens", 0)),
                "output_tokens": int(u.get("output_tokens", 0)),
                "cache_creation_input_tokens": int(u.get("cache_creation_input_tokens", 0)),
                "cache_read_input_tokens": int(u.get("cache_read_input_tokens", 0)),
                "total_cost_usd": float(ev.get("total_cost_usd", 0.0)),
                "duration_ms": int(ev.get("duration_ms", 0)),
            }
    return {}


# ---------------------------------------------------------------------------
# Explorer
# ---------------------------------------------------------------------------


def spawn_explorer(
    root: Path,
    *,
    parent_id: str,
    parent_worktree: Path,
    parent_commit: str,
    explore_context: str | None,
) -> dict[str, Any]:
    """Run a fresh `claude -p` to build the EXPLORE-phase session for
    parent_id. Returns the record dict; caller is responsible for writing
    it to .evo/explorers/<parent_id>.json.

    The cwd is the **repo root** (not the worktree) because evo's worker
    protocol asks subagents to call `evo ...` from the main repo. The
    EXPLORE prompt carries the worktree path as text so the agent can
    Read files via absolute paths.
    """
    skill_p = subagent_skill_path()
    if not skill_p.exists():
        raise RuntimeError(
            f"subagent SKILL.md not found at {skill_p}. "
            "Set EVO_SUBAGENT_SKILL_PATH or check plugin install layout."
        )

    prompt = render_explore_prompt(
        skill_path=skill_p,
        worktree_path=parent_worktree,
        parent_id=parent_id,
        explore_context=explore_context,
    )

    cmd = [CLAUDE_BIN, "-p", "--output-format", "json"]
    if DEFAULT_MODEL:
        cmd.extend(["--model", DEFAULT_MODEL])
    cmd.append(prompt)

    proc = subprocess.run(
        cmd,
        cwd=root,
        capture_output=True,
        text=True,
        timeout=EXPLORER_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        # Truncate stderr to avoid blasting walls of output into infra log.
        stderr_tail = (proc.stderr or "")[-500:]
        raise RuntimeError(
            f"claude explorer failed (exit={proc.returncode}): {stderr_tail}"
        )

    events = _parse_events(proc.stdout)
    sid = _extract_session_id(events)
    if not sid:
        raise RuntimeError(
            "could not parse session_id from claude explorer output; "
            "check that `claude -p --output-format json` is supported"
        )
    usage = _extract_usage(events)

    return {
        "parent_id": parent_id,
        "session_id": sid,
        "host": HOST_NAME,
        "worktree_commit": parent_commit,
        "skill_hash": subagent_skill_hash(),
        "explore_context_hash": hash_text(explore_context or ""),
        "cache_write_tokens": usage.get("cache_creation_input_tokens", 0),
        "explorer_input_tokens": usage.get("input_tokens", 0),
        "explorer_cost_usd": usage.get("total_cost_usd", 0.0),
        "created_at": utc_now(),
        "ttl_expires_at": utc_iso_in(DEFAULT_TTL_SECONDS),
    }


# ---------------------------------------------------------------------------
# Child fork
# ---------------------------------------------------------------------------


def spawn_child(
    root: Path,
    *,
    explorer_record: dict[str, Any],
    exp_id: str,
    worktree_path: Path,
    parent_id: str,
    brief: str,
    budget: int,
    job_dir: Path,
    background: bool = False,
) -> dict[str, Any]:
    """Run one child fork via `claude -p --resume <SID> --fork-session`.

    On ``background=False`` (default), this blocks until the child exits and
    returns a result dict containing exit_code, session_id, usage, and the
    paths to the captured stdout/stderr in `job_dir`.

    On ``background=True``, this returns immediately with the spawned
    Popen's pid; the caller (Phase 3 dispatch CLI) handles status tracking.
    """
    sid = explorer_record["session_id"]
    prompt = render_execute_prompt(
        exp_id=exp_id,
        worktree_path=worktree_path,
        parent_id=parent_id,
        brief=brief,
        budget=budget,
    )

    job_dir.mkdir(parents=True, exist_ok=True)
    stdout_log = job_dir / "stdout.log"
    stderr_log = job_dir / "stderr.log"

    cmd = [
        CLAUDE_BIN, "-p",
        "--resume", sid,
        "--fork-session",
        "--output-format", "json",
    ]
    if DEFAULT_MODEL:
        cmd.extend(["--model", DEFAULT_MODEL])
    cmd.append(prompt)

    if background:
        out_f = stdout_log.open("w")
        err_f = stderr_log.open("w")
        popen = subprocess.Popen(
            cmd,
            cwd=root,
            stdout=out_f,
            stderr=err_f,
            start_new_session=True,
        )
        # Don't close file handles here — they need to stay open for the
        # OS-level redirect. The Popen object owns them; subprocess closes
        # on exit.
        return {
            "background": True,
            "pid": popen.pid,
            "stdout_log": str(stdout_log),
            "stderr_log": str(stderr_log),
            "exp_id": exp_id,
            "started_at": utc_now(),
        }

    # Foreground
    proc = subprocess.run(
        cmd,
        cwd=root,
        capture_output=True,
        text=True,
    )
    stdout_log.write_text(proc.stdout or "", encoding="utf-8")
    stderr_log.write_text(proc.stderr or "", encoding="utf-8")

    events = _parse_events(proc.stdout)
    child_sid = _extract_session_id(events)
    usage = _extract_usage(events)

    return {
        "background": False,
        "exit_code": proc.returncode,
        "session_id": child_sid,
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "usage": usage,
        "exp_id": exp_id,
        "started_at": utc_now(),
    }
