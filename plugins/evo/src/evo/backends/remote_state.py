"""File-locked reader/writer for `remote_state.json`.

Schema:
{
  "provider": "modal",
  "provider_config": {...},
  "sandboxes": [
    {
      "id": 0,
      "native_id": "sb-abc123",
      "base_url": "https://...",
      "leased_by": null | {"exp_id": "exp_NNNN", "pid": 12345, "leased_at": "..."},
      "last_branch": "evo/run_NNNN/exp_NNNN" | null,
      "provisioned_at": "..."
    }
  ]
}

Note: bearer_token is intentionally NOT persisted on disk -- it lives
only in the orchestrator process's memory, regenerated on workspace
re-init. See SPEC.md "Roadmap > Open: secrets redaction policy".
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..locking import advisory_lock


def remote_state_path(root: Path) -> Path:
    """Path to the active run's remote_state.json. Resolves the run dir lazily."""
    from ..core import workspace_path

    return workspace_path(root) / "remote_state.json"


def init_state(root: Path, provider: str, provider_config: dict[str, Any]) -> None:
    """Create a fresh remote_state.json with no sandboxes provisioned yet.
    Sandboxes are spun up lazily on first `evo new`.
    """
    state_path = remote_state_path(root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "provider": provider,
        "provider_config": provider_config,
        "sandboxes": [],
    }
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _lock_path(state_path: Path) -> Path:
    return state_path.with_suffix(state_path.suffix + ".lock")


@contextmanager
def locked_state(root: Path) -> Iterator[dict[str, Any]]:
    """Open remote_state.json under a file lock for read-modify-write.

    Mirrors `pool_state.locked_state`. The caller mutates the dict in place;
    on exit the state is written via tmp-and-rename.
    """
    from ..core import atomic_write_json

    state_path = remote_state_path(root)
    if not state_path.exists():
        raise FileNotFoundError(f"remote_state.json missing at {state_path}")
    with advisory_lock(_lock_path(state_path)):
        state = _load_validated(state_path)
        yield state
        atomic_write_json(state_path, state)


def read_state(root: Path) -> dict[str, Any]:
    """Read-only snapshot of remote_state.json."""
    state_path = remote_state_path(root)
    if not state_path.exists():
        raise FileNotFoundError(f"remote_state.json missing at {state_path}")
    with advisory_lock(_lock_path(state_path)):
        return _load_validated(state_path)


def _load_validated(state_path: Path) -> dict[str, Any]:
    """Read + minimally validate remote_state.json. Surface a recovery error
    rather than letting JSON / KeyError percolate up to the user."""
    try:
        raw = state_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"remote_state.json at {state_path} is corrupted ({exc}). "
            f"This usually indicates an interrupted write. Inspect the file; "
            f"if recovery is impossible, restore from a backup or re-init."
        ) from exc
    if not isinstance(data, dict) or "sandboxes" not in data or "provider" not in data:
        raise RuntimeError(
            f"remote_state.json at {state_path} has unexpected shape "
            f"(missing 'provider' or 'sandboxes' key). File may have been "
            f"hand-edited or corrupted."
        )
    return data
