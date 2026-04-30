"""Execution backend protocol and registry.

Backends abstract workspace allocation and lifecycle:
- WorktreeBackend (default): fresh `git worktree` per experiment
- PoolBackend (alpha.1+): leases user-provided pre-built directories
- RemoteSandboxBackend (alpha.3+): provisions a remote container and runs
  experiments inside it via a provider-owned sandbox client
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

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
    SandboxClient,
    SandboxHandle,
    SandboxProvider,
    SandboxSpec,
)
from .remote import RemoteSandboxBackend
from .state_keys import backend_state_key
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
    "SandboxClient",
    "SandboxHandle",
    "SandboxProvider",
    "SandboxSpec",
    "WorktreeBackend",
    "backend_spec_for_node",
    "backend_spec_from_config",
    "load_backend",
    "backend_state_key",
]


def backend_spec_from_config(config: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return `(name, config)` from workspace config.json shape."""
    name = config.get("execution_backend", "worktree")
    cfg = config.get("execution_backend_config", {}) or {}
    return name, dict(cfg)


def backend_spec_for_node(
    root: Path,
    node: dict[str, Any],
    *,
    workspace_config: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return the backend spec for a graph node.

    Alpha.4 nodes persist their backend choice directly. Alpha.3 nodes do
    not, so they fall back to the workspace default from config.json.
    """
    if node.get("backend"):
        return node["backend"], dict(node.get("backend_config", {}) or {})

    from ..core import load_config  # lazy: avoid circular import

    config = workspace_config if workspace_config is not None else load_config(root)
    return backend_spec_from_config(config)



def _construct_backend(name: str, config: dict[str, Any]) -> Backend:
    if name == "worktree":
        return WorktreeBackend()
    if name == "pool":
        return PoolBackend(slot_paths=list(config.get("slots", []) or []))
    if name == "remote":
        from .sandbox_providers import load_provider
        provider_name = config.get("provider")
        if not provider_name:
            raise RemoteBackendUnavailable(
                "execution_backend=remote requires "
                "execution_backend_config.provider in config.json. "
                "Set it with `evo config backend remote --provider <name> ...` "
                "or pass a per-experiment override to `evo new`."
            )
        provider_config = config.get("provider_config", {}) or {}
        provider = load_provider(provider_name, provider_config)
        return RemoteSandboxBackend(
            provider,
            provider_name=provider_name,
            provider_config=provider_config,
        )
    raise ValueError(f"Unknown execution_backend: {name!r}")


def load_backend(
    root: Path,
    *,
    explicit_name: str | None = None,
    explicit_config: dict[str, Any] | None = None,
    node: dict[str, Any] | None = None,
    workspace_config: dict[str, Any] | None = None,
) -> Backend:
    """Resolve a backend from an explicit override, node state, or config."""
    if explicit_name is not None:
        return _construct_backend(explicit_name, dict(explicit_config or {}))
    if node is not None:
        name, cfg = backend_spec_for_node(root, node, workspace_config=workspace_config)
        return _construct_backend(name, cfg)

    from ..core import load_config  # lazy: avoid circular import

    config = workspace_config if workspace_config is not None else load_config(root)
    name, cfg = backend_spec_from_config(config)
    return _construct_backend(name, cfg)
