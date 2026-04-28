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


class PoolBackend:
    """Workspace allocator that leases from a fixed set of pre-built slots."""

    name = "pool"

    def allocate(self, ctx: AllocateCtx) -> AllocateResult:
        """Lease a free slot, validate it, branch in place, return the path.

        Steps:
        1. Lock pool_state.json. Find a slot with leased_by == null. Stale
           leases (recorded PID dead but lease still set) are NOT auto-
           reclaimed -- the user runs `evo workspace release <id>` after
           confirming the slot is in a usable state.
        2. Validate: path exists, git working tree of the same repo, no
           uncommitted tracked changes, parent_commit reachable.
        3. Mark `leased_by = {exp_id, pid, leased_at}`. Release lock.
        4. `git checkout -B <branch> <parent_commit>` (no `git clean`).
        5. On checkout failure, atomically clear the lease (only if it still
           matches our exp_id+pid) and re-raise.
        """
        slot_path = self._claim_slot(ctx)
        try:
            self._checkout_in_slot(slot_path, ctx.branch, ctx.parent_commit)
        except Exception:
            # Roll back the lease we just took, if it still matches us.
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
        with state_io.locked_state(ctx.root) as state:
            for slot in state["slots"]:
                lease = slot.get("leased_by")
                if lease and lease.get("exp_id") == exp_id:
                    slot["leased_by"] = None
                    slot["last_branch"] = ctx.node.get("branch") or slot.get("last_branch")

    def gc(self, ctx: DiscardCtx) -> None:
        """No-op. Pool slots are user-owned; gc never touches them."""

    def reset_all(self, root: Path) -> None:
        """Release every lease. Slot directories untouched."""
        with state_io.locked_state(root) as state:
            for slot in state["slots"]:
                slot["leased_by"] = None

    # --- internals ---------------------------------------------------------

    def _claim_slot(self, ctx: AllocateCtx) -> Path:
        with state_io.locked_state(ctx.root) as state:
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
            slot_path = Path(free_slot["path"])
            self._validate_slot_basics(slot_path, free_slot["id"])
            self._ensure_parent_commit(slot_path, ctx.parent_commit, free_slot["id"], state["slots"])
            free_slot["leased_by"] = {
                "exp_id": ctx.exp_id,
                "pid": os.getpid(),
                "leased_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            return slot_path

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

    @staticmethod
    def _release_if_matches(root: Path, exp_id: str, pid: int) -> None:
        """Atomically clear the lease only if it still matches {exp_id, pid}."""
        with state_io.locked_state(root) as state:
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
