"""Execution backend protocol and registry.

Backends abstract workspace allocation and lifecycle. WorktreeBackend
(default) creates a fresh `git worktree` per experiment; PoolBackend
leases user-provided pre-built directories.
"""
from __future__ import annotations

from pathlib import Path

from .pool import PoolBackend
from .protocol import (
    AllocateCtx,
    AllocateResult,
    Backend,
    BackendError,
    DiscardCtx,
    PoolExhausted,
    PoolSlotDirty,
    PoolSlotInvalid,
    PoolSlotMissingCommit,
)
from .worktree import WorktreeBackend

__all__ = [
    "AllocateCtx",
    "AllocateResult",
    "Backend",
    "BackendError",
    "DiscardCtx",
    "PoolBackend",
    "PoolExhausted",
    "PoolSlotDirty",
    "PoolSlotInvalid",
    "PoolSlotMissingCommit",
    "WorktreeBackend",
    "load_backend",
]


def load_backend(root: Path) -> Backend:
    """Return the configured backend for this workspace.

    Reads `execution_backend` from `.evo/config.json`; defaults to `worktree`
    when absent.
    """
    from ..core import load_config  # lazy: avoid circular import

    name = load_config(root).get("execution_backend", "worktree")
    if name == "worktree":
        return WorktreeBackend()
    if name == "pool":
        return PoolBackend()
    raise ValueError(f"Unknown execution_backend: {name!r}")
