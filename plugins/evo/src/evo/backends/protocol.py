"""Backend protocol types. No imports from `..core` -- safe to import anywhere."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass
class AllocateCtx:
    """Inputs to `Backend.allocate`."""

    root: Path
    exp_id: str
    parent_node: dict[str, Any] | None    # None when parent is the synthetic "root"
    parent_commit: str                    # frozen point-in-time commit hash
    parent_ref: str                       # branch ref used as `git worktree add` start point
    branch: str                           # new branch the workspace will be on
    hypothesis: str


@dataclass
class AllocateResult:
    """What `Backend.allocate` returns."""

    worktree: Path                        # absolute local path; the experiment's filesystem root
    commit: str                           # commit hash now at HEAD of the workspace
    branch: str                           # branch the workspace is on (echoed for symmetry)


@dataclass
class DiscardCtx:
    """Inputs to `Backend.discard` and `Backend.gc`."""

    root: Path
    node: dict[str, Any]


class Backend(Protocol):
    """Workspace lifecycle protocol."""

    name: str

    def allocate(self, ctx: AllocateCtx) -> AllocateResult: ...
    def discard(self, ctx: DiscardCtx) -> None: ...
    def release_lease(self, ctx: DiscardCtx) -> None: ...
    def gc(self, ctx: DiscardCtx) -> None: ...
    def reset_all(self, root: Path) -> None: ...


class BackendError(Exception):
    """Base for backend-specific errors surfaced to the user."""


class PoolExhausted(BackendError):
    """No free slot in the pool; concurrency cap reached."""


class PoolSlotDirty(BackendError):
    """Slot has uncommitted tracked changes; lease refused."""


class PoolSlotMissingCommit(BackendError):
    """Slot's git store does not contain the required parent commit."""


class PoolSlotInvalid(BackendError):
    """Slot path is missing, not a git working tree, or origin mismatch."""
