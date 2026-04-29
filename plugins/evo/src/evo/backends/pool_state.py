"""File-locked reader/writer for keyed pool state files.

Schema:
{
  "slots": [
    {
      "id": 0,
      "path": "/abs/path/to/ws-N",
      "leased_by": null | {"exp_id": "exp_NNNN", "pid": 12345, "leased_at": "..."},
      "last_branch": "evo/run_NNNN/exp_NNNN" | null
    },
    ...
  ]
}
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..locking import advisory_lock


def _state_dir(root: Path) -> Path:
    from ..core import workspace_path

    return workspace_path(root) / "backend_state"


def pool_state_path(root: Path, state_key: str | None = None) -> Path:
    """Path to this pool config's state file."""
    if state_key is None:
        from ..core import workspace_path

        return workspace_path(root) / "pool_state.json"
    return _state_dir(root) / f"pool-{state_key}.json"


def _migrate_legacy_if_needed(root: Path, state_key: str) -> Path:
    keyed = pool_state_path(root, state_key)
    if keyed.exists():
        return keyed
    legacy = pool_state_path(root, None)
    if legacy.exists():
        keyed.parent.mkdir(parents=True, exist_ok=True)
        legacy.replace(keyed)
    return keyed


def _resolve_state_path(root: Path, state_key: str | None) -> Path:
    if state_key is not None:
        return _migrate_legacy_if_needed(root, state_key)
    legacy = pool_state_path(root, None)
    if legacy.exists():
        return legacy
    state_dir = _state_dir(root)
    matches = sorted(state_dir.glob("pool-*.json")) if state_dir.exists() else []
    if len(matches) == 1:
        return matches[0]
    if not matches:
        return legacy
    raise RuntimeError(
        "multiple pool state files exist for this run; pass an explicit state key"
    )


def init_state(root: Path, slot_paths: list[str], state_key: str) -> None:
    """Create a fresh keyed pool-state file with all slots free."""
    state_path = pool_state_path(root, state_key)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "slots": [
            {"id": i, "path": p, "leased_by": None, "last_branch": None}
            for i, p in enumerate(slot_paths)
        ]
    }
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _lock_path(state_path: Path) -> Path:
    return state_path.with_suffix(state_path.suffix + ".lock")


@contextmanager
def locked_state(root: Path, state_key: str) -> Iterator[dict[str, Any]]:
    """Open this pool config's state file under a file lock for RMW.

    Yields the parsed state; the caller mutates it in place. On exit, the
    state is written via tmp-and-rename so a process killed mid-write leaves
    the original file intact.
    """
    from ..core import atomic_write_json

    state_path = _migrate_legacy_if_needed(root, state_key)
    if not state_path.exists():
        raise FileNotFoundError(f"pool_state.json missing at {state_path}")
    with advisory_lock(_lock_path(state_path)):
        state = _load_validated(state_path)
        yield state
        atomic_write_json(state_path, state)


def read_state(root: Path, state_key: str | None = None) -> dict[str, Any]:
    """Read-only snapshot of this pool config's state file.

    Acquires the lock briefly to
    avoid reading a partial file mid-rewrite (atomic_write_json should make
    this redundant, but the lock costs us nothing and rules out edge cases
    on filesystems with weak rename semantics)."""
    state_path = _resolve_state_path(root, state_key)
    if not state_path.exists():
        raise FileNotFoundError(f"pool_state.json missing at {state_path}")
    with advisory_lock(_lock_path(state_path)):
        return _load_validated(state_path)


def _load_validated(state_path: Path) -> dict[str, Any]:
    """Read + minimally validate pool_state.json. Surface a recovery error
    rather than letting JSON / KeyError percolate up to the user."""
    try:
        raw = state_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"pool_state.json at {state_path} is corrupted ({exc}). "
            f"This usually indicates an interrupted write. Inspect the file; "
            f"if recovery is impossible, restore from a backup or re-init."
        ) from exc
    if not isinstance(data, dict) or "slots" not in data:
        raise RuntimeError(
            f"pool_state.json at {state_path} has unexpected shape "
            f"(missing top-level 'slots' key). File may have been hand-edited "
            f"or corrupted."
        )
    return data
