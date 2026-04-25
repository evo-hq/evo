"""Explorer-cache infrastructure for `evo dispatch`.

The orchestrator never imports this module directly; it goes through the
`evo dispatch` CLI verb (added in a follow-up). This module owns:

* the on-disk schema for cached explorer sessions
  (`.evo/<active-run>/explorers/<parent_id>.json`)
* the predicates that decide when a cached explorer can be reused
* hash helpers used by those predicates
* the EXPLORE-phase user-message template the explorer subprocess sees

Subprocess spawning lives in per-host modules (`hosts/claude_fork.py`, etc.),
which call `_ensure_explorer` and consume its handle.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .core import (
    DISPATCH_HOSTS,
    allocate_experiment,
    atomic_write_json,
    current_commit,
    get_host,
    load_json,
    load_graph,
    workspace_path,
)

# ---------------------------------------------------------------------------
# Layout & constants
# ---------------------------------------------------------------------------

EXPLORERS_DIR = "explorers"

# OpenAI cache TTL maxes at 1 hour; Anthropic exposes a 1h ephemeral option.
# Match that as the default explorer lifespan.
DEFAULT_TTL_SECONDS = 60 * 60

# Path to the worker-protocol skill that the explorer reads first. Relative
# to the plugin root so it works both in dev (editable install) and from the
# Claude Code plugin marketplace cache (where the whole plugin tree ships).
SUBAGENT_SKILL_RELPATH = Path("skills") / "subagent" / "SKILL.md"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def explorers_dir(root: Path) -> Path:
    """Per-run directory for explorer-session metadata. Lives under the
    active run dir so that `evo reset` removes it along with everything
    else for that run; parent_ids are run-scoped (next run renumbers from
    exp_0000), so cross-run reuse would never be valid anyway."""
    return workspace_path(root) / EXPLORERS_DIR


def explorer_record_path(root: Path, parent_id: str) -> Path:
    return explorers_dir(root) / f"{parent_id}.json"


def subagent_skill_path() -> Path:
    """Locate the worker-protocol SKILL.md the explorer should Read.

    Resolution order:
      1. ``EVO_SUBAGENT_SKILL_PATH`` env var (operator override)
      2. plugin-relative path: walk up from this module to the plugin root
         and append `skills/subagent/SKILL.md`. Works when the plugin is
         installed editable (`<plugin>/src/evo/dispatch.py`) or dropped into
         the Claude Code plugin cache (same shape).
    """
    override = os.environ.get("EVO_SUBAGENT_SKILL_PATH")
    if override:
        return Path(override).resolve()
    # __file__ → src/evo/dispatch.py;  parents[2] = plugin root
    plugin_root = Path(__file__).resolve().parents[2]
    return plugin_root / SUBAGENT_SKILL_RELPATH


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def hash_file(path: Path) -> str:
    """SHA-256 of a file's bytes. Returns empty string when the file is
    missing — callers treat empty as "absent" and rebuild the explorer."""
    if not path.exists():
        return ""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def hash_text(text: str | None) -> str:
    """SHA-256 of a string. Empty input returns empty string so a missing
    explore_context can be distinguished from an empty one."""
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def subagent_skill_hash() -> str:
    """Hash of the current worker-protocol skill. Changes here invalidate
    every cached explorer because each one's transcript embeds the SKILL
    text via its first Read tool call."""
    return hash_file(subagent_skill_path())


# ---------------------------------------------------------------------------
# TTL
# ---------------------------------------------------------------------------


def utc_iso_in(seconds: int) -> str:
    """ISO-8601 UTC timestamp `seconds` from now. Used for `ttl_expires_at`."""
    return (
        datetime.now(timezone.utc) + timedelta(seconds=seconds)
    ).isoformat(timespec="seconds")


def _parse_iso(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Cache validation
# ---------------------------------------------------------------------------


def explorer_is_valid(
    record: dict[str, Any],
    *,
    parent_commit: str,
    skill_hash: str,
    explore_context_hash: str,
    current_host: str,
) -> tuple[bool, str]:
    """Decide whether a cached explorer record can be reused.

    Returns ``(valid, reason)`` where ``reason`` names the failure mode
    when ``valid`` is False (used in infra-log entries) and is empty
    otherwise.

    Invalidation matrix (any miss → rebuild):
      * ``host`` mismatch — explorer was created for a different runtime
      * ``worktree_commit`` drift — parent node was amended/rebased
      * ``skill_hash`` change — worker protocol was edited
      * ``explore_context_hash`` change with a non-empty new hint —
        a fresh ``--explore-context`` requires rebuilding the prefix
      * TTL expired or unparseable
    """
    if record.get("host") != current_host:
        return False, f"host_mismatch:{record.get('host')}->{current_host}"
    if record.get("worktree_commit") != parent_commit:
        return False, "parent_commit_drift"
    if record.get("skill_hash") != skill_hash:
        return False, "skill_md_changed"

    # explore_context: empty new hint → reuse regardless of cached value
    # (caller didn't pass one, fall back to whatever was baked in).
    # Non-empty new hint → must match the cached one, else rebuild.
    if explore_context_hash:
        rec_ctx = record.get("explore_context_hash") or ""
        if rec_ctx != explore_context_hash:
            return False, "explore_context_changed"

    expires = _parse_iso(record.get("ttl_expires_at"))
    if expires is None:
        return False, "ttl_unset_or_unparseable"
    if expires < datetime.now(timezone.utc):
        return False, "ttl_expired"

    return True, ""


def load_explorer_record(root: Path, parent_id: str) -> dict[str, Any] | None:
    """Read an explorer record, or None if missing."""
    path = explorer_record_path(root, parent_id)
    if not path.exists():
        return None
    return load_json(path, default=None)


# ---------------------------------------------------------------------------
# EXPLORE-phase user message
# ---------------------------------------------------------------------------

#: Template for the explorer subprocess's first user message. The literal
#: ``{...}`` placeholders are filled by per-host spawn code. The phrasing
#: is deliberate — the agent is told instructions arrive later, so it
#: doesn't try to act on the brief during EXPLORE.
EXPLORE_USER_PROMPT_TEMPLATE = """You are an evo worker in EXPLORE phase. Your detailed edit instructions \
will arrive later as a brief — for now, your only job is to read.

First, load the worker protocol that will apply once you receive a brief:
  Read: {skill_path}

Then explore the target:
  Worktree: {worktree_path}
  Parent node: {parent_id}
{explore_context_block}
  Read the files that matter for the optimization target. Build a structural
  understanding by reading. Cover the surface that will be relevant for any
  edit downstream.

In this phase: do NOT propose edits, do NOT run evo commands, do NOT
summarize. When you've finished reading the relevant code, reply with the
single word: ready

The actual hypothesis to attempt arrives in your next user message.
"""


def render_explore_prompt(
    *,
    skill_path: Path,
    worktree_path: Path,
    parent_id: str,
    explore_context: str | None,
) -> str:
    """Concrete EXPLORE-phase user message for a given parent + worktree.
    Caller passes the rendered string as the explorer subprocess's prompt."""
    if explore_context:
        block = (
            "\n  Orchestrator focus for this round:\n"
            "  " + explore_context.replace("\n", "\n  ").rstrip() + "\n"
        )
    else:
        block = "\n"
    return EXPLORE_USER_PROMPT_TEMPLATE.format(
        skill_path=str(skill_path),
        worktree_path=str(worktree_path),
        parent_id=parent_id,
        explore_context_block=block,
    )


# ---------------------------------------------------------------------------
# EXECUTE-phase user message
# ---------------------------------------------------------------------------

#: Template for the child fork's first user message. The child inherits
#: the explorer's transcript (skill + code reads) via fork-session, so
#: this prompt only needs to deliver the per-child handle: the experiment
#: it has been allocated, where to edit, the brief, and the budget.
EXECUTE_USER_PROMPT_TEMPLATE = """EXECUTE phase begins now.
Your experiment: {exp_id}
Worktree: {worktree_path}
Parent: {parent_id}

Brief:
{brief}

Budget: {budget} iterations. Follow the protocol you loaded earlier. Begin.
"""


def render_execute_prompt(
    *,
    exp_id: str,
    worktree_path: Path,
    parent_id: str,
    brief: str,
    budget: int,
) -> str:
    """Concrete EXECUTE-phase user message for one child fork."""
    return EXECUTE_USER_PROMPT_TEMPLATE.format(
        exp_id=exp_id,
        worktree_path=str(worktree_path),
        parent_id=parent_id,
        brief=brief.strip(),
        budget=budget,
    )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DispatchNotSupportedError(RuntimeError):
    """Raised when `evo dispatch` is invoked on a host without a fork
    handler. The caller (CLI) translates this into guidance that points
    the orchestrator at its host's native parallel-Task primitive."""


class ExplorerSpawnError(RuntimeError):
    """Explorer subprocess failed; orchestrator cannot proceed for this
    parent until the underlying issue is fixed (or `--no-fork` is added
    in a future revision)."""


# ---------------------------------------------------------------------------
# Orchestration: ensure_explorer + dispatch_child
# ---------------------------------------------------------------------------


def _require_dispatch_host(root: Path) -> str:
    """Resolve and validate the current dispatch host. Raises
    DispatchNotSupportedError if the workspace's host doesn't support
    fork-cache. Callers translate this to user-facing guidance."""
    host = get_host(root)
    if host is None:
        raise DispatchNotSupportedError(
            "no host recorded for this workspace. "
            "Run `evo host set <claude-code|codex|opencode|...>` first."
        )
    if host not in DISPATCH_HOSTS:
        raise DispatchNotSupportedError(
            f"`evo dispatch` is not supported on host={host}. "
            "Use your host's parallel-Task primitive instead — see "
            "plugins/evo/skills/optimize/SKILL.md. "
            f"Currently dispatchable: {sorted(DISPATCH_HOSTS)}."
        )
    return host


def ensure_explorer(
    root: Path,
    *,
    parent_id: str,
    explore_context: str | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Return a valid explorer record for ``parent_id``, creating one if
    none exists or the cached one is stale.

    Reuse predicate matches ``explorer_is_valid``. ``refresh=True`` forces
    a rebuild even when the cache would otherwise be valid (operator
    override; orchestrator passes this when it wants new
    ``--explore-context`` to take effect mid-run).

    Raises ``DispatchNotSupportedError`` if the host lacks fork support
    and ``ExplorerSpawnError`` if the spawn subprocess fails.
    """
    host = _require_dispatch_host(root)

    # Resolve parent worktree + commit. ``parent_id`` must be a real node.
    graph = load_graph(root)
    if parent_id not in graph["nodes"]:
        raise RuntimeError(f"unknown parent: {parent_id}")
    node = graph["nodes"][parent_id]
    parent_worktree = Path(node["worktree"]) if node.get("worktree") else root
    parent_commit_value = node.get("commit") or _resolve_parent_commit(root, parent_worktree)

    skill_h = subagent_skill_hash()
    ctx_h = hash_text(explore_context or "")

    record = load_explorer_record(root, parent_id) if not refresh else None
    if record is not None:
        valid, reason = explorer_is_valid(
            record,
            parent_commit=parent_commit_value,
            skill_hash=skill_h,
            explore_context_hash=ctx_h,
            current_host=host,
        )
        if valid:
            return record
        # Fall through to rebuild; reason is logged by the CLI layer.

    # Rebuild via the host handler.
    from .hosts import HOST_HANDLERS  # local import to avoid cycle
    handler = HOST_HANDLERS.get(host)
    if handler is None:
        raise DispatchNotSupportedError(
            f"no host handler registered for host={host}. "
            "This is a bug — DISPATCH_HOSTS and HOST_HANDLERS drifted."
        )
    try:
        new_record = handler.spawn_explorer(
            root,
            parent_id=parent_id,
            parent_worktree=parent_worktree,
            parent_commit=parent_commit_value,
            explore_context=explore_context,
        )
    except Exception as exc:
        raise ExplorerSpawnError(str(exc)) from exc

    explorers_dir(root).mkdir(parents=True, exist_ok=True)
    atomic_write_json(explorer_record_path(root, parent_id), new_record)
    return new_record


def _resolve_parent_commit(root: Path, parent_worktree: Path) -> str:
    """Best-effort parent commit resolution. For non-root nodes the graph
    already has the commit; this is a fallback that asks git directly."""
    try:
        return current_commit(parent_worktree)
    except Exception:  # noqa: BLE001
        return current_commit(root)


def dispatch_child(
    root: Path,
    *,
    parent_id: str,
    brief: str,
    budget: int,
    explore_context: str | None = None,
    refresh_explorer: bool = False,
    background: bool = False,
    job_dir_factory=None,
) -> dict[str, Any]:
    """Allocate one new experiment under ``parent_id``, ensure the
    explorer is warm, then spawn a fork via the host handler.

    Returns a dict with at minimum ``exp_id``, plus the host handler's
    output (background pid + log paths, or foreground exit_code + usage).

    ``job_dir_factory(exp_id) -> Path`` lets the CLI pick its own
    ``forks/<job_id>/`` layout. Defaults to ``<active-run>/forks/<exp_id>/``.
    """
    host = _require_dispatch_host(root)
    record = ensure_explorer(
        root,
        parent_id=parent_id,
        explore_context=explore_context,
        refresh=refresh_explorer,
    )

    node = allocate_experiment(root, parent_id=parent_id, hypothesis=brief)
    exp_id = node["id"]
    worktree_path = Path(node["worktree"])

    if job_dir_factory is None:
        job_dir = workspace_path(root) / "forks" / exp_id
    else:
        job_dir = job_dir_factory(exp_id)

    from .hosts import HOST_HANDLERS  # local import to avoid cycle
    handler = HOST_HANDLERS[host]
    spawn_result = handler.spawn_child(
        root,
        explorer_record=record,
        exp_id=exp_id,
        worktree_path=worktree_path,
        parent_id=parent_id,
        brief=brief,
        budget=budget,
        job_dir=job_dir,
        background=background,
    )

    return {
        "exp_id": exp_id,
        "parent_id": parent_id,
        "worktree": str(worktree_path),
        "job_dir": str(job_dir),
        "explorer_session_id": record["session_id"],
        **spawn_result,
    }
