"""Worktree backend: creates a fresh `git worktree` per experiment.

Default backend. Codifies today's behavior from `core.allocate_experiment`,
`core.remove_worktree_only`, `core.delete_discarded_experiment`, and
`core.reset_runtime_state`.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .protocol import AllocateCtx, AllocateResult, DiscardCtx


class WorktreeBackend:
    """Workspace allocator that runs `git worktree add` per experiment."""

    name = "worktree"

    def allocate(self, ctx: AllocateCtx) -> AllocateResult:
        from ..core import (
            PROJECT_FILE,
            WORKSPACE_NAME,
            current_commit,
            git_branch_exists,
            project_path,
            worktrees_path,
        )

        root = ctx.root
        worktree = worktrees_path(root) / ctx.exp_id

        # A freshly allocated experiment ID should be collision-free. If a stale
        # branch or prunable worktree exists for that ID, clean it up here so a
        # partial prior run does not block new allocation.
        if worktree.exists():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree)],
                cwd=root,
                check=False,
            )
            shutil.rmtree(worktree, ignore_errors=True)
        subprocess.run(["git", "worktree", "prune"], cwd=root, check=False)
        if git_branch_exists(root, ctx.branch):
            subprocess.run(["git", "branch", "-D", ctx.branch], cwd=root, check=False)

        subprocess.run(
            ["git", "worktree", "add", "-b", ctx.branch, str(worktree), ctx.parent_ref],
            cwd=root,
            check=True,
        )

        # Propagate project.md into the worktree so it's accessible even
        # though it's not committed to git.
        project_src = project_path(root)
        if project_src.exists():
            worktree_evo = worktree / WORKSPACE_NAME
            worktree_evo.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(project_src), str(worktree_evo / PROJECT_FILE))

        return AllocateResult(
            worktree=worktree,
            commit=current_commit(worktree),
            branch=ctx.branch,
        )

    def discard(self, ctx: DiscardCtx) -> None:
        """Full cleanup: remove worktree directory and delete the branch."""
        self._remove_worktree_only(ctx.root, ctx.node)
        branch = ctx.node.get("branch")
        if branch:
            subprocess.run(["git", "branch", "-D", branch], cwd=ctx.root, check=False)

    def release_lease(self, ctx: DiscardCtx) -> None:
        """No-op. Worktree mode has no lease state."""

    def gc(self, ctx: DiscardCtx) -> bool:
        """Remove worktree directory only (today's `cmd_gc` behavior).
        Returns True so the CLI reports this node as freed."""
        self._remove_worktree_only(ctx.root, ctx.node)
        return True

    def reset_all(self, root: Path) -> None:
        """Wipe all worktrees and branches for the active run."""
        from ..core import _load_meta, workspace_path, worktrees_path

        meta = _load_meta(root)
        run_id = meta.get("active")
        workspace = workspace_path(root)
        wt_dir = worktrees_path(root)
        subprocess.run(["git", "worktree", "prune"], cwd=root, check=False)
        if wt_dir.exists():
            for path in sorted(wt_dir.iterdir()):
                if path.is_dir():
                    subprocess.run(
                        ["git", "worktree", "remove", "--force", str(path)],
                        cwd=root,
                        check=False,
                    )
                    shutil.rmtree(path, ignore_errors=True)
        subprocess.run(["git", "worktree", "prune"], cwd=root, check=False)
        # Only delete branches for this run (evo/<run_id>/*)
        branch_prefix = f"refs/heads/evo/{run_id}/" if run_id else "refs/heads/evo/"
        result = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname:short)", branch_prefix],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        for branch in [line.strip() for line in result.stdout.splitlines() if line.strip()]:
            subprocess.run(["git", "branch", "-D", branch], cwd=root, check=False)
        shutil.rmtree(workspace, ignore_errors=True)

    @staticmethod
    def _remove_worktree_only(root: Path, node: dict) -> None:
        worktree = Path(node["worktree"])
        if worktree.exists():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree)],
                cwd=root,
                check=False,
            )
        if worktree.exists():
            shutil.rmtree(worktree, ignore_errors=True)
        subprocess.run(["git", "worktree", "prune"], cwd=root, check=False)
