"""RemoteSandboxBackend: workspace lifecycle backed by a remote sandbox.

The provider (Modal, E2B, SSH, ...) provisions a container; sandbox-agent
runs inside it on a known port; this backend talks to sandbox-agent over
HTTP for everything else (file ops, process exec, git ops).

This module is lifecycle-only -- it owns provisioning, leasing, and
tear-down. The HTTP client lives in `evo.sandbox_client` (commit 2/5);
artifact streaming and `cmd_run` integration live in commit 4/5.

State persists in `<run>/remote_state.json` (see `remote_state.py`). The
on-disk schema deliberately omits bearer tokens; tokens live only in this
process's memory and are regenerated on workspace re-init or when a
sandbox is re-provisioned.
"""
from __future__ import annotations

import os
import secrets
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from ..core import utc_now, workspace_path
from . import remote_state
from .protocol import (
    AllocateCtx,
    AllocateResult,
    Backend,
    DiscardCtx,
    SandboxHandle,
    SandboxProvider,
    SandboxSpec,
)


class RemoteSandboxBackend:
    """Lease lifecycle for remote sandboxes. Provider-agnostic.

    Lifecycle parallels PoolBackend (pool.py:37-310): an `allocate()` call
    leases a sandbox (provisioning lazily on first use), `release_lease()`
    returns it to the free pool, `discard()` tears it down.

    POC scope: concurrency=1 (one active sandbox per workspace),
    tear-down on release. A `keep_warm` provider_config flag will gate
    warm-reuse in alpha.4.
    """

    name = "remote"

    def __init__(self, provider: SandboxProvider) -> None:
        self.provider = provider
        # Tokens live in memory only; never persisted to remote_state.json.
        # Keyed by sandbox `id` (the local index, not the provider native_id).
        self._tokens: dict[int, str] = {}
        # SandboxHandle objects live in memory too -- the provider needs them
        # for tear_down(), but they include opaque metadata (e.g. modal app
        # references) that aren't safe to serialize. Re-hydrated lazily from
        # remote_state.json's native_id on cold-start.
        self._handles: dict[int, SandboxHandle] = {}

    # ---------------------------------------------------------------- allocate

    def allocate(self, ctx: AllocateCtx) -> AllocateResult:
        """Lease a sandbox for the experiment, provisioning if needed.

        Mirrors PoolBackend.allocate (pool.py:37-72): reconcile orphaned
        leases first, then claim a slot atomically under the state lock,
        then perform slow operations (provision, parent_commit shipping)
        outside the lock with explicit unwind on failure.

        For the POC the parent-commit-shipping + checkout step is stubbed
        out -- commit 4/5 wires it through the sandbox-agent client.
        """
        from ..backends import pool  # for orphan-reconciliation pattern

        # Step 1: reconcile orphaned leases. Same shape as
        # `pool._reconcile_orphaned_leases`; see pool.py:249-270.
        self._reconcile_orphaned(ctx.root)

        # Step 2: under the state lock, find or create a free sandbox slot
        # and stamp the lease atomically. Slow operations (provision call,
        # network IO) happen outside the lock.
        slot_id, needs_provision, handle = self._claim_slot(ctx)

        try:
            if needs_provision:
                handle = self._provision_sandbox(slot_id)
                self._handles[slot_id] = handle
                # Persist provider-side metadata that survives orchestrator
                # restart (the bearer_token is intentionally NOT persisted).
                with remote_state.locked_state(ctx.root) as state:
                    sandbox = state["sandboxes"][slot_id]
                    sandbox["native_id"] = handle.native_id
                    sandbox["base_url"] = handle.base_url
                    sandbox["provisioned_at"] = utc_now()

            # Step 3: ship parent commit into the sandbox + check out the
            # experiment's branch. Stubbed for commit 1/5; real impl in 4/5.
            worktree_path = self._setup_workspace(ctx, handle)
        except Exception:
            # Unwind: release the lease atomically. Provider-side handle
            # stays warm (transient failures shouldn't burn a sandbox).
            self._release_if_matches(ctx.root, slot_id, ctx.exp_id)
            raise

        return AllocateResult(
            worktree=worktree_path,
            commit=ctx.parent_commit,
            branch=ctx.branch,
        )

    # ---------------------------------------------------------------- discard

    def discard(self, ctx: DiscardCtx) -> None:
        """Tear down the sandbox the experiment was running on."""
        node = ctx.node
        slot_id = self._slot_for_exp(ctx.root, node["id"])
        if slot_id is None:
            return  # nothing to do
        handle = self._handles.get(slot_id)
        if handle is not None:
            try:
                self.provider.tear_down(handle)
            except Exception:
                # Best-effort; sandbox may already be gone (network blip,
                # provider-side timeout). State cleanup proceeds regardless.
                pass
        with remote_state.locked_state(ctx.root) as state:
            # Drop the slot entirely on discard. Re-allocate gets a fresh
            # provision; no half-states left around.
            state["sandboxes"] = [
                s for s in state["sandboxes"] if s["id"] != slot_id
            ]
        self._handles.pop(slot_id, None)
        self._tokens.pop(slot_id, None)

    # ---------------------------------------------------------------- release_lease

    def release_lease(self, ctx: DiscardCtx) -> None:
        """Clear the lease without tearing down the sandbox.

        POC behavior: ALSO tears down (concurrency=1, no warm-reuse). When
        we add `keep_warm` config in alpha.4, this becomes the path that
        retains the sandbox.
        """
        # POC: same as discard.
        self.discard(ctx)

    # ---------------------------------------------------------------- gc

    def gc(self, ctx: DiscardCtx) -> bool:
        """Best-effort cleanup of stale sandboxes whose holders are gone.

        Returns True if anything got cleaned up so cli.cmd_gc reports it.
        """
        cleaned = False
        with remote_state.locked_state(ctx.root) as state:
            keep: list[dict[str, Any]] = []
            for sandbox in state["sandboxes"]:
                if sandbox.get("leased_by") is None:
                    handle = self._handles.get(sandbox["id"])
                    if handle is not None:
                        try:
                            self.provider.tear_down(handle)
                            cleaned = True
                        except Exception:
                            pass
                        self._handles.pop(sandbox["id"], None)
                        self._tokens.pop(sandbox["id"], None)
                else:
                    keep.append(sandbox)
            state["sandboxes"] = keep
        return cleaned

    # ---------------------------------------------------------------- reset_all

    def reset_all(self, root: Path) -> None:
        """Tear down every recorded sandbox and wipe the workspace dir."""
        try:
            state = remote_state.read_state(root)
        except FileNotFoundError:
            state = {"sandboxes": []}
        for sandbox in state.get("sandboxes", []):
            handle = self._handles.get(sandbox["id"])
            if handle is None:
                # Reconstitute a minimal handle so tear_down has something
                # to work with. Provider-specific tear_down should be
                # idempotent; we don't have the metadata dict on cold start.
                continue
            try:
                self.provider.tear_down(handle)
            except Exception:
                pass
        self._handles.clear()
        self._tokens.clear()
        shutil.rmtree(workspace_path(root), ignore_errors=True)

    # ---------------------------------------------------------------- internal

    def _claim_slot(
        self, ctx: AllocateCtx
    ) -> tuple[int, bool, SandboxHandle | None]:
        """Atomically claim or create a sandbox slot.

        POC concurrency=1: first allocate provisions slot 0; subsequent
        allocates either re-use slot 0 (if free) or raise PoolExhausted.

        Returns (slot_id, needs_provision, existing_handle_or_None). If
        needs_provision is True, the caller must call _provision_sandbox
        OUTSIDE the state lock and then update the state with the handle.
        """
        from ..backends.protocol import PoolExhausted

        with remote_state.locked_state(ctx.root) as state:
            free = [s for s in state["sandboxes"] if s.get("leased_by") is None]
            if free:
                sandbox = free[0]
                slot_id = sandbox["id"]
                sandbox["leased_by"] = {
                    "exp_id": ctx.exp_id,
                    "pid": os.getpid(),
                    "leased_at": utc_now(),
                }
                sandbox["last_branch"] = ctx.branch
                handle = self._handles.get(slot_id)
                return slot_id, handle is None, handle

            # No free slot. POC concurrency=1: only one slot total.
            if len(state["sandboxes"]) >= 1:
                raise PoolExhausted(
                    "remote backend has no free sandbox; concurrency=1 in "
                    "POC. Wait for the active experiment to finish, or run "
                    "evo workspace status to inspect lease state."
                )

            # Reserve a new slot id.
            slot_id = len(state["sandboxes"])
            state["sandboxes"].append({
                "id": slot_id,
                "native_id": None,           # filled in after provision
                "base_url": None,
                "leased_by": {
                    "exp_id": ctx.exp_id,
                    "pid": os.getpid(),
                    "leased_at": utc_now(),
                },
                "last_branch": ctx.branch,
                "provisioned_at": None,
            })
            return slot_id, True, None

    def _provision_sandbox(self, slot_id: int) -> SandboxHandle:
        """Call the provider to spin up a new container.

        The bearer token is generated here and held in process memory only.
        The image_ref + env are POC defaults; alpha.4 will plumb these
        through provider_config.
        """
        token = secrets.token_urlsafe(32)
        self._tokens[slot_id] = token
        spec = SandboxSpec(
            image_ref="evo-sandbox-base",   # provider resolves to its own image system
            env={},                          # alpha.4: forwarded user secrets
            bearer_token=token,
        )
        handle = self.provider.provision(spec)
        return handle

    def _setup_workspace(
        self, ctx: AllocateCtx, handle: SandboxHandle | None
    ) -> Path:
        """Ship parent commit into the sandbox + checkout the experiment branch.

        STUB for commit 1/5. The real implementation lands in commit 4/5
        once the sandbox-agent HTTP client and git-bundle helpers exist.
        Returns the in-sandbox worktree path encoded as a Path so callers
        that interpolate it into shell commands continue to work.
        """
        # POC sandbox-internal layout: /workspace/exp_NNNN
        return Path(f"/workspace/{ctx.exp_id}")

    def _slot_for_exp(self, root: Path, exp_id: str) -> int | None:
        """Return the slot id currently leased by `exp_id`, or None."""
        try:
            state = remote_state.read_state(root)
        except FileNotFoundError:
            return None
        for sandbox in state["sandboxes"]:
            lease = sandbox.get("leased_by")
            if lease and lease.get("exp_id") == exp_id:
                return sandbox["id"]
        return None

    def _release_if_matches(self, root: Path, slot_id: int, exp_id: str) -> None:
        """Atomically release the lease on `slot_id` only if it's currently
        held by `exp_id`. Mirror of pool._release_if_matches (pool.py:240-246).
        """
        with remote_state.locked_state(root) as state:
            for sandbox in state["sandboxes"]:
                if sandbox["id"] == slot_id:
                    lease = sandbox.get("leased_by")
                    if lease and lease.get("exp_id") == exp_id:
                        sandbox["leased_by"] = None
                    break

    def _reconcile_orphaned(self, root: Path) -> None:
        """Clear leases whose owning experiments are now in a terminal state.

        Mirror of pool._reconcile_orphaned_leases (pool.py:249-270). Defends
        the crash window between `_mark_committed` and `release_lease` in
        `cli.cmd_run`: if the process dies after the graph update but before
        the lease release, the slot would otherwise be pinned forever.

        Only acts when the graph has an explicit terminal status. A missing
        node is NOT treated as terminal -- masks real bugs (e.g. a partial
        graph write would otherwise look like a leaked lease).
        """
        from ..core import load_graph

        try:
            graph = load_graph(root)
        except FileNotFoundError:
            return

        terminal = {"committed", "discarded"}
        with remote_state.locked_state(root) as state_locked:
            for sandbox in state_locked["sandboxes"]:
                lease = sandbox.get("leased_by")
                if not lease:
                    continue
                exp_id = lease.get("exp_id")
                node = graph["nodes"].get(exp_id)
                if node is not None and node.get("status") in terminal:
                    sandbox["leased_by"] = None
