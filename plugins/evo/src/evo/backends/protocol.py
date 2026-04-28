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
    def gc(self, ctx: DiscardCtx) -> bool:
        """Returns True if this call freed disk-side state, False otherwise.

        Worktree backend returns True (worktree dir was removed); pool
        backend returns False (slots are user-owned, gc is a no-op). The
        CLI layer uses this to avoid reporting pool nodes as 'removed'
        when nothing was cleaned up.
        """
        ...
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


class RemoteBackendUnavailable(BackendError):
    """Requested remote provider's SDK is not installed or its API is unreachable."""


# ---------------------------------------------------------------------------
# Remote sandbox provider types (alpha.3+)
# ---------------------------------------------------------------------------


@dataclass
class SandboxSpec:
    """What `SandboxProvider.provision` needs to spin up a remote sandbox."""

    image_ref: str                      # provider-native image reference (e.g. Modal image hash)
    env: dict[str, str]                 # env vars for the in-sandbox benchmark process
    bearer_token: str                   # generated per-sandbox; passed to sandbox-agent
    exposed_port: int = 8080            # sandbox-agent listen port inside the container
    timeout_seconds: int = 3600         # provider-native lifetime cap


@dataclass
class SandboxHandle:
    """What `SandboxProvider.provision` returns; opaque to the orchestrator
    above the backend layer. The base_url + bearer_token is what the
    sandbox-agent HTTP client speaks to."""

    provider: str                       # echoed for diagnostics
    base_url: str                       # https://<provider-tunnel>/...
    bearer_token: str                   # what the orchestrator sends as Authorization
    native_id: str                      # provider-native ID (e.g. modal sandbox.id)
    metadata: dict[str, Any]            # opaque, provider-internal


class SandboxProvider(Protocol):
    """Pluggable adapter for a remote container provider (Modal, E2B, ...).

    Each implementation lives in `evo.backends.sandbox_providers.<name>` as
    a Python module that lazy-imports its provider SDK. `RemoteSandboxBackend`
    is provider-agnostic; everything provider-specific is here.
    """

    name: str

    def provision(self, spec: SandboxSpec) -> SandboxHandle: ...
    def tear_down(self, handle: SandboxHandle) -> None: ...
    def is_alive(self, handle: SandboxHandle) -> bool: ...
