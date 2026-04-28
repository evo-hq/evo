"""Execution backend protocol and registry.

Backends abstract workspace allocation and lifecycle:
- WorktreeBackend (default): fresh `git worktree` per experiment
- PoolBackend (alpha.1+): leases user-provided pre-built directories
- RemoteSandboxBackend (alpha.3+): provisions a remote container and runs
  experiments inside it via sandbox-agent's HTTP API
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
    RemoteBackendUnavailable,
    SandboxHandle,
    SandboxProvider,
    SandboxSpec,
)
from .remote import RemoteSandboxBackend
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
    "RemoteBackendUnavailable",
    "RemoteSandboxBackend",
    "SandboxHandle",
    "SandboxProvider",
    "SandboxSpec",
    "WorktreeBackend",
    "load_backend",
]


def load_backend(root: Path) -> Backend:
    """Return the configured backend for this workspace.

    Reads `execution_backend` from `.evo/config.json`; defaults to `worktree`
    when absent.
    """
    from ..core import load_config  # lazy: avoid circular import

    config = load_config(root)
    name = config.get("execution_backend", "worktree")
    if name == "worktree":
        return WorktreeBackend()
    if name == "pool":
        return PoolBackend()
    if name == "remote":
        from .sandbox_providers import load_provider
        cfg = config.get("execution_backend_config", {}) or {}
        provider_name = cfg.get("provider")
        if not provider_name:
            raise RemoteBackendUnavailable(
                "execution_backend=remote requires "
                "execution_backend_config.provider in config.json. "
                "Re-init with `evo init --backend remote --provider <name> ...`."
            )
        provider = load_provider(provider_name, cfg.get("provider_config", {}) or {})
        return RemoteSandboxBackend(provider)
    raise ValueError(f"Unknown execution_backend: {name!r}")
