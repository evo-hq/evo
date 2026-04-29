"""Pool backend: leases user-provided pre-built workspace directories.

Each `evo new` leases an idle slot from the user-defined pool, runs
`git checkout -B <branch> <parent_commit>` in the slot (no `git clean`),
and returns the slot path as the experiment's worktree. The lease is
held until `committed` or `discarded`; `failed` retains the lease so
retries can resume against the agent's prior edits.

evo never creates, deletes, or modifies untracked files in slot directories
-- they are user-owned. `discard` releases the lease and (by default) keeps
the experiment's branch in the slot for inspection.
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import pool_state as state_io
from .protocol import (
    AllocateCtx,
    AllocateResult,
    DiscardCtx,
    PoolExhausted,
    PoolSlotDirty,
    PoolSlotInvalid,
    PoolSlotMissingCommit,
)
from .state_keys import backend_state_key


class PoolBackend:
    """Workspace allocator that leases from a fixed set of pre-built slots."""

    name = "pool"

    def __init__(self, slot_paths: list[str] | None = None) -> None:
        self.slot_paths = list(slot_paths or [])
        self.state_key = backend_state_key(self.name, {"slots": self.slot_paths})

    def allocate(self, ctx: AllocateCtx) -> AllocateResult:
        """Lease a free slot, validate it, branch in place, return the path.

        Lock hold time is minimized: the pool_state.json file lock is held
        only long enough to reconcile orphaned leases, find an idle slot,
        and mark `leased_by`. Validation (`git diff` in the slot) and parent
        commit fetching (`git fetch --all`, sibling-slot fetches) run
        outside the lock -- they can take seconds-to-minutes on large
        repos and the advisory lock would otherwise time out concurrent
        pool operations (`evo workspace status`, sibling `evo new` calls,
        lease release on commit).

        On any post-claim failure (validation, fetch, checkout), the lease
        is released atomically only if it still matches our {exp_id, pid}.
        """
        self._ensure_state_file(ctx.root)
        slot_path = self._claim_slot(ctx)
        try:
            self._validate_slot_basics(slot_path, self._slot_id_for(ctx.root, slot_path))
            self._ensure_parent_commit(
                slot_path,
                ctx.parent_commit,
                self._slot_id_for(ctx.root, slot_path),
                self._all_slot_paths(ctx.root),
            )
            self._checkout_in_slot(slot_path, ctx.branch, ctx.parent_commit)
        except Exception:
            self._release_if_matches(ctx.root, ctx.exp_id, os.getpid())
            raise
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=slot_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return AllocateResult(worktree=slot_path, commit=head, branch=ctx.branch)

    def discard(self, ctx: DiscardCtx) -> None:
        """Release the lease. Keep the branch (default). Slot dir untouched."""
        self.release_lease(ctx)

    def release_lease(self, ctx: DiscardCtx) -> None:
        """Clear the lease for this experiment's slot. Idempotent."""
        exp_id = ctx.node["id"]
        with state_io.locked_state(ctx.root, self.state_key) as state:
            for slot in state["slots"]:
                lease = slot.get("leased_by")
                if lease and lease.get("exp_id") == exp_id:
                    slot["leased_by"] = None
                    slot["last_branch"] = ctx.node.get("branch") or slot.get("last_branch")

    def gc(self, ctx: DiscardCtx) -> bool:
        """No-op. Pool slots are user-owned; gc never touches them. Returns
        False so the CLI doesn't report pool nodes as freed by gc."""
        return False

    def reset_all(self, root: Path) -> None:
        """Release every lease, then wipe the run's controller-side state.

        Slot directories are user-owned and never touched. But `.evo/run_NNNN/`
        (graph.json, config, experiments, forks, pool_state.json) is evo's
        own and gets removed -- mirroring WorktreeBackend.reset_all so that
        `evo status` after `evo reset` correctly reports "workspace not
        initialized" instead of returning stale run state.
        """
        import shutil

        from ..core import workspace_path

        with state_io.locked_state(root, self.state_key) as state:
            for slot in state["slots"]:
                slot["leased_by"] = None
        shutil.rmtree(workspace_path(root), ignore_errors=True)

    def _ensure_state_file(self, root: Path) -> None:
        """Initialize or reconcile pool_state.json for this backend config."""
        state_path = state_io.pool_state_path(root, self.state_key)
        if not state_path.exists():
            state_io.init_state(root, self.slot_paths, self.state_key)
            return

        state = state_io.read_state(root, self.state_key)
        existing_slots = [slot["path"] for slot in state.get("slots", [])]
        if existing_slots == self.slot_paths:
            return

        leased = [
            slot["leased_by"]["exp_id"]
            for slot in state.get("slots", [])
            if slot.get("leased_by")
        ]
        if leased:
            raise RuntimeError(
                "cannot switch pool slot sets while pool experiments are still "
                f"leased: {', '.join(leased)}"
            )
        state_io.init_state(root, self.slot_paths, self.state_key)

    # --- internals ---------------------------------------------------------

    def _claim_slot(self, ctx: AllocateCtx) -> Path:
        """Reconcile + find + claim under the lock; nothing slow here.
        Validation and git fetching happen in the caller, outside the lock.
        """
        with state_io.locked_state(ctx.root, self.state_key) as state:
            self._reconcile_orphaned_leases(ctx.root, state)
            free_slot = next(
                (s for s in state["slots"] if s.get("leased_by") is None),
                None,
            )
            if free_slot is None:
                lessees = [
                    s["leased_by"].get("exp_id", "?")
                    for s in state["slots"]
                    if s.get("leased_by")
                ]
                raise PoolExhausted(
                    f"pool exhausted ({len(state['slots'])}/{len(state['slots'])} "
                    f"leased to {', '.join(lessees)}). "
                    f"Wait for an experiment to complete, or run "
                    f"`evo workspace status` to inspect."
                )
            free_slot["leased_by"] = {
                "exp_id": ctx.exp_id,
                "pid": os.getpid(),
                "leased_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            return Path(free_slot["path"])

    def _slot_id_for(self, root: Path, slot_path: Path) -> int:
        """Map a slot path back to its id by reading the (no-lock) state."""
        state = state_io.read_state(root, self.state_key)
        for slot in state["slots"]:
            if Path(slot["path"]) == slot_path:
                return int(slot["id"])
        return -1

    def _all_slot_paths(self, root: Path) -> list[dict]:
        """Snapshot of slots (paths + ids) for sibling-fetch fallback. No
        lock -- the snapshot is informational; if a sibling state changes
        between snapshot and fetch, the fetch just falls through harmlessly.
        """
        return state_io.read_state(root, self.state_key)["slots"]

    @staticmethod
    def _validate_slot_basics(slot: Path, slot_id: int) -> None:
        if not slot.exists() or not (slot / ".git").exists():
            raise PoolSlotInvalid(
                f"slot {slot_id} ({slot}) is not a git working tree. "
                f"Init validation should have caught this; the slot may have been moved."
            )
        # Reject if there are uncommitted tracked changes -- evo refuses to
        # overwrite user edits.
        diff = subprocess.run(["git", "diff", "--quiet"], cwd=slot, check=False)
        cached = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=slot, check=False)
        if diff.returncode != 0 or cached.returncode != 0:
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=slot, check=False, capture_output=True, text=True,
            ).stdout.strip()
            raise PoolSlotDirty(
                f"slot {slot_id} ({slot}) has uncommitted tracked changes:\n"
                f"{status}\n"
                f"evo refuses to overwrite user edits. "
                f"Commit or stash inside the slot, then re-run."
            )

    @staticmethod
    def _commit_present(slot: Path, commit: str) -> bool:
        return subprocess.run(
            ["git", "cat-file", "-e", commit],
            cwd=slot, check=False, capture_output=True,
        ).returncode == 0

    def _ensure_parent_commit(
        self, slot: Path, parent_commit: str, slot_id: int, all_slots: list[dict]
    ) -> None:
        """Ensure parent_commit is reachable in the slot's git store.

        Lookup order:
        1. Already present locally → done.
        2. `git fetch --all` (origin); recheck → done if found.
        3. Sibling slot scan: a previous experiment in another slot may have
           created the commit; fetch directly from that slot's git dir.
        4. Otherwise raise PoolSlotMissingCommit.

        Step 3 lets pool mode work without requiring the user to push
        experiment branches to a shared remote. Each commit lives in the slot
        that produced it; sibling slots fetch on demand.
        """
        if self._commit_present(slot, parent_commit):
            return
        subprocess.run(["git", "fetch", "--all"], cwd=slot, check=False)
        if self._commit_present(slot, parent_commit):
            return
        for other in all_slots:
            other_path = Path(other["path"])
            if other_path == slot:
                continue
            if not (other_path / ".git").exists():
                continue
            if not self._commit_present(other_path, parent_commit):
                continue
            subprocess.run(
                ["git", "fetch", str(other_path), parent_commit],
                cwd=slot, check=False,
            )
            if self._commit_present(slot, parent_commit):
                return
        raise PoolSlotMissingCommit(
            f"slot {slot_id} ({slot}) does not have parent commit "
            f"{parent_commit[:12]} locally. `git fetch` from origin and "
            f"sibling slots both failed to retrieve it. Update the slot manually."
        )

    @staticmethod
    def _checkout_in_slot(slot: Path, branch: str, parent_commit: str) -> None:
        """`git checkout -B <branch> <parent_commit>` with no `git clean`."""
        subprocess.run(
            ["git", "checkout", "-B", branch, parent_commit],
            cwd=slot,
            check=True,
        )

    def _release_if_matches(self, root: Path, exp_id: str, pid: int) -> None:
        """Atomically clear the lease only if it still matches {exp_id, pid}."""
        with state_io.locked_state(root, self.state_key) as state:
            for slot in state["slots"]:
                lease = slot.get("leased_by")
                if lease and lease.get("exp_id") == exp_id and lease.get("pid") == pid:
                    slot["leased_by"] = None

    @staticmethod
    def _reconcile_orphaned_leases(root: Path, state: dict) -> None:
        """Clear any lease whose experiment is already terminal in the graph.

        Defends the crash window between `_mark_committed` and `release_lease`
        in `cli.cmd_run`: if the process dies after the graph update but
        before the lease release, the slot would otherwise be pinned forever.
        Same applies to `_record_done_result` and `cmd_discard`. Called under
        the state lock during `allocate`.
        """
        from ..core import graph_path, load_json, default_graph

        graph = load_json(graph_path(root), default_graph())
        nodes = graph.get("nodes", {})
        for slot in state["slots"]:
            lease = slot.get("leased_by")
            if not lease:
                continue
            exp_id = lease.get("exp_id")
            node = nodes.get(exp_id)
            if node and node.get("status") in {"committed", "discarded"}:
                slot["leased_by"] = None
                slot["last_branch"] = node.get("branch") or slot.get("last_branch")
