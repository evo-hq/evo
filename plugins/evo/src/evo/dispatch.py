"""Explorer-cache infrastructure for `evo dispatch`.

The orchestrator never imports this module directly; it goes through the
`evo dispatch` CLI verb (added in a follow-up). This module owns:

* the on-disk schema for cached explorer sessions
  (`.evo/explorers/<parent_id>.json`)
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

from .core import evo_dir, load_json

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
    """Top-level directory for explorer-session metadata. Workspace-scoped,
    not per-run, because parent_id is workspace-stable once committed."""
    return evo_dir(root) / EXPLORERS_DIR


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
