"""RemoteSandboxBackend: workspace lifecycle backed by a remote sandbox.

The provider (Modal, E2B, SSH, ...) provisions the
container and owns the corresponding process/filesystem client object
for file ops, process exec, git ops, and teardown.

This module is lifecycle-only -- it owns provisioning, leasing, and
tear-down. State persists in `<run>/remote_state.json` (see
`remote_state.py`).
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
from .state_keys import backend_state_key


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

    def __init__(
        self,
        provider: SandboxProvider,
        *,
        provider_name: str | None = None,
        provider_config: dict[str, Any] | None = None,
    ) -> None:
        self.provider = provider
        self.provider_name = provider_name or provider.name
        self.provider_config = dict(provider_config or {})
        pool_size = self.provider_config.get("pool_size")
        if pool_size in (None, "", "unbounded"):
            self.pool_size: int | None = None
        else:
            try:
                parsed = int(pool_size)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"remote provider_config.pool_size must be an integer, got {pool_size!r}"
                ) from exc
            if parsed <= 0:
                raise RuntimeError("remote provider_config.pool_size must be > 0")
            self.pool_size = parsed
        self.state_key = backend_state_key(
            self.name,
            {
                "provider": self.provider_name,
                "provider_config": self.provider_config,
            },
        )
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
        self._ensure_state_file(ctx.root)
        self._reconcile_orphaned(ctx.root)

        # Step 2: under the state lock, find or create a free sandbox slot
        # and stamp the lease atomically. Slow operations (provision call,
        # network IO) happen outside the lock.
        slot_id, needs_provision, handle = self._claim_slot(ctx)

        try:
            if needs_provision:
                handle = self._provision_sandbox(slot_id)
                self._handles[slot_id] = handle
                with remote_state.locked_state(ctx.root, self.state_key) as state:
                    sandbox = next(
                        s for s in state["sandboxes"] if s["id"] == slot_id
                    )
                    sandbox["native_id"] = handle.native_id
                    sandbox["base_url"] = handle.base_url
                    sandbox["bearer_token"] = handle.bearer_token
                    sandbox["metadata"] = dict(handle.metadata or {})
                    sandbox["provisioned_at"] = utc_now()

            # Step 3: ship parent commit into the sandbox + check out the
            # experiment's branch.
            worktree_path = self._setup_workspace(ctx, handle, slot_id)
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
        handle = self._handle_for_slot(ctx.root, slot_id)
        if handle is not None:
            try:
                self.provider.tear_down(handle)
            except Exception:
                # Best-effort; sandbox may already be gone (network blip,
                # provider-side timeout). State cleanup proceeds regardless.
                pass
        with remote_state.locked_state(ctx.root, self.state_key) as state:
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

        POC behavior: ALSO tears down (no warm-reuse yet). When
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
        with remote_state.locked_state(ctx.root, self.state_key) as state:
            keep: list[dict[str, Any]] = []
            for sandbox in state["sandboxes"]:
                if sandbox.get("leased_by") is None:
                    handle = self._handle_from_record(sandbox)
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
            state = remote_state.read_state(root, self.state_key)
        except FileNotFoundError:
            state = {"sandboxes": []}
        for sandbox in state.get("sandboxes", []):
            handle = self._handle_from_record(sandbox)
            if handle is None:
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

        Claims a free slot if one exists; otherwise provisions a new
        sandbox unless `pool_size` has been reached.

        Returns (slot_id, needs_provision, existing_handle_or_None). If
        needs_provision is True, the caller must call _provision_sandbox
        OUTSIDE the state lock and then update the state with the handle.
        """
        from ..backends.protocol import PoolExhausted

        with remote_state.locked_state(ctx.root, self.state_key) as state:
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

            if self.pool_size is not None and len(state["sandboxes"]) >= self.pool_size:
                raise PoolExhausted(
                    "remote backend has no free sandbox; pool_size="
                    f"{self.pool_size} reached. Wait for an active experiment "
                    "to finish, or increase provider_config.pool_size."
                )

            slot_id = int(state.get("next_id", 0))
            state["next_id"] = slot_id + 1
            state["sandboxes"].append({
                "id": slot_id,
                "native_id": None,           # filled in after provision
                "base_url": None,
                "bearer_token": "",
                "leased_by": {
                    "exp_id": ctx.exp_id,
                    "pid": os.getpid(),
                    "leased_at": utc_now(),
                },
                "last_branch": ctx.branch,
                "provisioned_at": None,
            })
            return slot_id, True, None

    def _ensure_state_file(self, root: Path) -> None:
        """Create or reconcile remote_state.json for this provider config."""
        state_path = remote_state.remote_state_path(root, self.state_key)
        if state_path.exists():
            state = remote_state.read_state(root, self.state_key)
            if (
                state.get("provider") == self.provider_name
                and (state.get("provider_config", {}) or {}) == self.provider_config
            ):
                return
            leased = [
                sandbox["leased_by"]["exp_id"]
                for sandbox in state.get("sandboxes", [])
                if sandbox.get("leased_by")
            ]
            if leased:
                raise RuntimeError(
                    "cannot switch remote provider config while remote "
                    f"sandboxes are still leased: {', '.join(leased)}"
                )
        remote_state.init_state(
            root,
            provider=self.provider_name,
            provider_config=self.provider_config,
            state_key=self.state_key,
        )

    def _provision_sandbox(self, slot_id: int) -> SandboxHandle:
        """Call the provider to spin up a new container.

        The bearer token is generated here and held in process memory only.
        The image_ref + env are POC defaults; alpha.4 will plumb these
        through provider_config.
        """
        token = secrets.token_urlsafe(32)
        spec = SandboxSpec(
            image_ref="evo-sandbox-base",   # provider resolves to its own image system
            env={},                          # alpha.4: forwarded user secrets
            bearer_token=token,
        )
        handle = self.provider.provision(spec)
        # Use the handle's token, not the spec's. Manual provider returns
        # its own configured token (the user-managed sandbox-agent was
        # started with that token, not the freshly-generated one).
        self._tokens[slot_id] = handle.bearer_token
        return handle

    def client_for_node(self, root: Path, node: dict[str, Any]):
        """Return the provider-specific client for the sandbox leased by `node`.

        Used by cmd_run to route shell + fs ops through the provider's
        sandbox client. Re-hydrates the SandboxHandle from on-disk state if
        not in memory (different process; common because `evo new` and
        `evo run` are separate subprocess invocations from the agent).
        """
        slot_id = self._slot_for_exp(root, node["id"])
        if slot_id is None:
            raise RuntimeError(
                f"No sandbox leased by {node.get('id')!r}; "
                f"call backend.allocate() first."
            )
        handle = self._handle_for_slot(root, slot_id)
        if handle is None:
            raise RuntimeError(
                f"Sandbox slot {slot_id} for {node.get('id')!r} is missing "
                f"its provisioned handle in remote_state. Re-allocate via "
                f"`evo discard {node['id']} --reason ...` + "
                f"`evo new --parent ...`."
            )
        if not self.provider.is_alive(handle):
            raise RuntimeError(
                f"Sandbox for {node.get('id')!r} is no longer reachable. "
                f"Re-allocate via `evo discard {node['id']} --reason ...` + "
                f"`evo new --parent ...`."
            )
        return self.provider.build_client(handle)

    def _setup_workspace(
        self, ctx: AllocateCtx, handle: SandboxHandle | None, slot_id: int
    ) -> Path:
        """Ship parent commit into the sandbox + checkout the experiment branch.

        Steps:
          1. Ensure the in-sandbox workspace exists and is a git repo.
             The workspace path comes from handle.metadata["workspace_root"]
             when set (manual provider configures this for non-/workspace
             host filesystems); otherwise defaults to /workspace/repo.
          2. Ship parent commit via git bundle.
          3. Check out the experiment's branch at parent commit.

        Returns the in-sandbox workspace path. In remote mode there's no
        separate `git worktree`; the experiment's branch is checked out
        in place in the cloned repo.
        """
        from ..git_bundle import (
            SANDBOX_REPO_ROOT, SANDBOX_BUNDLE_DIR,
            ship_commit_to_sandbox,
        )

        if handle is None:
            raise RuntimeError(
                "_setup_workspace called without a SandboxHandle; "
                "indicates a backend ordering bug."
            )
        meta = handle.metadata or {}
        workspace_root = meta.get("workspace_root", SANDBOX_REPO_ROOT)
        bundle_dir = meta.get("bundle_dir", SANDBOX_BUNDLE_DIR)
        client = self.provider.build_client(handle)
        with client:
            # 1. Ensure the in-sandbox repo dir exists and is a git repo.
            client.fs_mkdir(workspace_root, recursive=True)
            init_check = client.process_run(
                "git", args=["rev-parse", "--git-dir"], cwd=workspace_root,
            )
            if init_check.exit_code != 0:
                # Fresh sandbox: init the repo + set committer identity for
                # any subsequent in-sandbox commits.
                init_result = client.process_run(
                    "git", args=["init", "-q"], cwd=workspace_root,
                )
                if init_result.exit_code != 0:
                    raise RuntimeError(
                        f"git init failed in sandbox: {init_result.stderr[:500]}"
                    )
                client.process_run(
                    "git", args=["config", "user.email", "evo@sandbox"],
                    cwd=workspace_root,
                )
                client.process_run(
                    "git", args=["config", "user.name", "evo"],
                    cwd=workspace_root,
                )

            # 2. Ship the parent commit. Skip if already there (re-leased
            # sandbox case, common when keep_warm lands).
            cat_check = client.process_run(
                "git", args=["cat-file", "-e", ctx.parent_commit],
                cwd=workspace_root,
            )
            if cat_check.exit_code != 0:
                ship_commit_to_sandbox(
                    client, local_repo=ctx.root, commit=ctx.parent_commit,
                    sandbox_repo=workspace_root, bundle_dir=bundle_dir,
                )

            # 3. Check out the experiment's branch at parent commit.
            checkout = client.process_run(
                "git",
                args=["checkout", "-B", ctx.branch, ctx.parent_commit],
                cwd=workspace_root,
            )
            if checkout.exit_code != 0:
                raise RuntimeError(
                    f"git checkout -B {ctx.branch} {ctx.parent_commit} "
                    f"failed in sandbox: {checkout.stderr[:500]}"
                )

        # Persist on the slot's state record so cmd_run can read the same
        # path via the backend client without re-fetching the handle.
        with remote_state.locked_state(ctx.root, self.state_key) as state:
            for sandbox in state["sandboxes"]:
                if sandbox["id"] == slot_id:
                    sandbox["workspace_root"] = workspace_root
                    sandbox["bundle_dir"] = bundle_dir
                    break
        return Path(workspace_root)

    def _slot_for_exp(self, root: Path, exp_id: str) -> int | None:
        """Return the slot id currently leased by `exp_id`, or None."""
        try:
            state = remote_state.read_state(root, self.state_key)
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
        with remote_state.locked_state(root, self.state_key) as state:
            for sandbox in state["sandboxes"]:
                if sandbox["id"] == slot_id:
                    lease = sandbox.get("leased_by")
                    if lease and lease.get("exp_id") == exp_id:
                        sandbox["leased_by"] = None
                    break

    def _handle_for_slot(self, root: Path, slot_id: int) -> SandboxHandle | None:
        handle = self._handles.get(slot_id)
        if handle is not None:
            return handle
        try:
            state = remote_state.read_state(root, self.state_key)
        except FileNotFoundError:
            return None
        sandbox = next((s for s in state["sandboxes"] if s["id"] == slot_id), None)
        return self._handle_from_record(sandbox)

    def _handle_from_record(
        self, sandbox_record: dict[str, Any] | None
    ) -> SandboxHandle | None:
        if sandbox_record is None or not sandbox_record.get("base_url"):
            return None
        slot_id = sandbox_record["id"]
        handle = SandboxHandle(
            provider=self.provider_name,
            base_url=sandbox_record["base_url"],
            bearer_token=sandbox_record.get("bearer_token", ""),
            native_id=sandbox_record.get("native_id") or f"slot-{slot_id}",
            metadata=sandbox_record.get("metadata") or {},
        )
        self._handles[slot_id] = handle
        self._tokens[slot_id] = handle.bearer_token
        return handle

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
        with remote_state.locked_state(root, self.state_key) as state_locked:
            for sandbox in state_locked["sandboxes"]:
                lease = sandbox.get("leased_by")
                if not lease:
                    continue
                exp_id = lease.get("exp_id")
                node = graph["nodes"].get(exp_id)
                if node is not None and node.get("status") in terminal:
                    sandbox["leased_by"] = None
