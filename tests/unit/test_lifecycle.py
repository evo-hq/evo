"""Tests for the v3 lifecycle changes:
- Hard-fail on update-ref errors (was silent before)
- Anchor-ref `refs/evo-anchor/<run>/<exp>` written for both worktree and remote
- discard guards (committed/active/has-children)
- prune accepting evaluated nodes
- restore covering pruned and discarded nodes
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


# Use the interpreter that's running pytest. Bare "python" isn't on PATH on
# macOS Homebrew (only "python3"); sys.executable always resolves.
PY = sys.executable


def _init_git_repo(root: Path) -> str:
    """Initialize a git repo at root with one commit. Return commit hash."""
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@evo"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
    (root / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True,
    )
    return out.stdout.strip()


class TestAnchorCommitHardFails(unittest.TestCase):
    """Stage 1: update-ref must hard-fail (was silent with check=False)."""

    def test_anchor_helper_raises_on_invalid_ref_name(self):
        """When update-ref is given an invalid ref name, the helper must
        raise RuntimeError instead of silently continuing. The pre-fix code
        used check=False and would swallow the failure, leaving the commit
        un-anchored."""
        from evo.cli import _anchor_commit_ref  # noqa: F401 (import inside test for clarity)

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            commit = _init_git_repo(root)
            # Ref name with a literal space is rejected by git update-ref.
            with self.assertRaises(RuntimeError) as ctx:
                _anchor_commit_ref(root, "run 0000", "exp_0001", commit)
            self.assertIn("anchor", str(ctx.exception).lower())

    def test_anchor_helper_succeeds_on_valid_input(self):
        """Sanity check: the helper writes the ref correctly when inputs
        are valid."""
        from evo.cli import _anchor_commit_ref

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            commit = _init_git_repo(root)
            _anchor_commit_ref(root, "run_0000", "exp_0001", commit)
            # Verify the ref now exists and points at the commit
            out = subprocess.run(
                ["git", "rev-parse", "refs/evo-anchor/run_0000/exp_0001"],
                cwd=root, check=True, capture_output=True, text=True,
            )
            self.assertEqual(out.stdout.strip(), commit)


class TestWorktreeAnchorRefSurvivesGC(unittest.TestCase):
    """Stage 2: worktree-backed committed nodes get an anchor ref written
    so the commit survives `git branch -D` + `git gc --prune=now`.

    Without the fix, discarding a committed worktree node deletes the only
    ref keeping the commit alive (`refs/heads/evo/<run>/<exp>`), and a
    subsequent `git gc` reclaims the commit. After the fix, the anchor at
    `refs/evo-anchor/<run>/<exp>` keeps the commit reachable.
    """

    def test_anchor_ref_keeps_commit_alive_through_gc(self):
        """End-to-end: spin up a real workspace, commit an experiment,
        delete the regular branch (simulating `evo discard`), run `git gc`,
        and confirm the commit is still reachable through the anchor."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            commit_a = _init_git_repo(root)

            # Simulate what cmd_run does after a successful commit:
            # 1. Make a new commit on a branch (the experiment's branch)
            subprocess.run(
                ["git", "checkout", "-q", "-b", "evo/run_0000/exp_0001"],
                cwd=root, check=True,
            )
            (root / "experiment.txt").write_text("change\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "exp"],
                cwd=root, check=True,
            )
            commit_b = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, check=True,
                capture_output=True, text=True,
            ).stdout.strip()

            # 2. Anchor the commit via the new helper (this is what cmd_run
            #    now does for worktree backend per Stage 2).
            from evo.cli import _anchor_commit_ref
            _anchor_commit_ref(root, "run_0000", "exp_0001", commit_b)

            # 3. Simulate `evo discard`: delete the regular branch.
            subprocess.run(["git", "checkout", "-q", "main"], cwd=root, check=False)
            # Also try master in case git defaults differ
            subprocess.run(
                ["git", "checkout", "-q", commit_a], cwd=root, check=True,
            )
            subprocess.run(
                ["git", "branch", "-D", "evo/run_0000/exp_0001"],
                cwd=root, check=True,
            )

            # 4. Run aggressive git gc
            subprocess.run(
                ["git", "gc", "--prune=now", "--quiet"],
                cwd=root, check=True,
            )

            # 5. The commit must still be reachable via the anchor ref
            result = subprocess.run(
                ["git", "rev-parse", "refs/evo-anchor/run_0000/exp_0001"],
                cwd=root, check=False, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0,
                             f"anchor ref missing or broken: {result.stderr}")
            self.assertEqual(result.stdout.strip(), commit_b,
                             "anchor ref points at wrong commit")

            # And the commit object is still in the ODB
            cat = subprocess.run(
                ["git", "cat-file", "-e", commit_b],
                cwd=root, check=False, capture_output=True,
            )
            self.assertEqual(cat.returncode, 0,
                             "commit was reclaimed by git gc despite anchor")

    def test_without_anchor_gc_reclaims_committed_branch(self):
        """Negative control: same flow but WITHOUT writing the anchor.
        This proves the test scenario actually triggers gc reclamation —
        otherwise the positive test would be meaningless."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            commit_a = _init_git_repo(root)
            subprocess.run(
                ["git", "checkout", "-q", "-b", "evo/run_0000/exp_0001"],
                cwd=root, check=True,
            )
            (root / "experiment.txt").write_text("change\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "exp"],
                cwd=root, check=True,
            )
            commit_b = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, check=True,
                capture_output=True, text=True,
            ).stdout.strip()
            # Skip the anchor step — this is the pre-fix behavior.

            subprocess.run(["git", "checkout", "-q", commit_a], cwd=root, check=True)
            subprocess.run(
                ["git", "branch", "-D", "evo/run_0000/exp_0001"],
                cwd=root, check=True,
            )
            # Drop reflog entries so prune can collect the commit.
            subprocess.run(
                ["git", "reflog", "expire", "--expire=now", "--all"],
                cwd=root, check=True,
            )
            subprocess.run(
                ["git", "gc", "--prune=now", "--quiet"],
                cwd=root, check=True,
            )

            # Commit object should now be GC'd
            cat = subprocess.run(
                ["git", "cat-file", "-e", commit_b],
                cwd=root, check=False, capture_output=True,
            )
            self.assertNotEqual(
                cat.returncode, 0,
                "expected gc to reclaim commit when no anchor exists; "
                "if this passes, the test scenario isn't actually triggering "
                "gc-reclamation and the positive test above is vacuous"
            )


def _build_graph_workspace(root: Path, nodes: dict) -> Path:
    """Set up a minimal .evo/run_0000/ workspace with the given graph nodes.
    Skips backend allocation; just writes the JSON files needed by cmd_*
    to read graph state."""
    from evo import core
    evo_dir = root / ".evo"
    run_dir = evo_dir / "run_0000"
    run_dir.mkdir(parents=True, exist_ok=True)
    # meta.json points at the active run
    (evo_dir / "meta.json").write_text(json.dumps({"active": "run_0000", "next_run": 1}))
    # config.json: minimal but valid
    (run_dir / "config.json").write_text(json.dumps({
        "metric": "max",
        "execution_backend": "worktree",
        "current_eval_epoch": 1,
    }))
    # graph.json with provided nodes plus root
    graph = core.default_graph()
    for nid, node_data in nodes.items():
        graph["nodes"][nid] = node_data
        # Wire children
        parent = node_data.get("parent")
        if parent and parent in graph["nodes"]:
            graph["nodes"][parent].setdefault("children", []).append(nid)
    (run_dir / "graph.json").write_text(json.dumps(graph))
    (run_dir / "annotations.json").write_text(json.dumps({"annotations": []}))
    (run_dir / "infra_log.json").write_text(json.dumps({"events": []}))
    return root


def _make_node(exp_id: str, parent: str, status: str, **kwargs) -> dict:
    """Minimal graph node fixture."""
    base = {
        "id": exp_id,
        "parent": parent,
        "children": [],
        "status": status,
        "hypothesis": kwargs.get("hypothesis", f"hyp for {exp_id}"),
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "eval_epoch": 1,
        "score": kwargs.get("score"),
        "branch": f"evo/run_0000/{exp_id}",
        "worktree": str(Path("/tmp") / f"evo-mock-{exp_id}"),
        "commit": kwargs.get("commit"),
        "pruned_reason": None,
        "gates": [],
        "current_attempt": 0,
        "notes": [],
    }
    base.update(kwargs)
    return base


class TestDiscardGuards(unittest.TestCase):
    """Stage 3a: discard refuses committed / active / has-children."""

    def _run_discard(self, root: Path, exp_id: str, reason: str = "test", force: bool = False):
        """Invoke cmd_discard with a synthetic argparse namespace."""
        from evo.cli import cmd_discard
        # Switch cwd to root so repo_root() resolves correctly
        import os
        prev = os.getcwd()
        os.chdir(root)
        try:
            ns = argparse.Namespace(exp_id=exp_id, reason=reason, force=force)
            return cmd_discard(ns)
        finally:
            os.chdir(prev)

    def test_discard_refuses_committed_node(self):
        """Discarding a committed node deletes its branch and orphans the
        commit. This is the primary footgun. After fix, must error and
        suggest `evo prune`."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init_git_repo(root)
            _build_graph_workspace(root, {
                "exp_0001": _make_node("exp_0001", "root", "committed",
                                       score=0.7, commit="abc1234"),
            })
            with self.assertRaises(RuntimeError) as ctx:
                self._run_discard(root, "exp_0001")
            msg = str(ctx.exception).lower()
            self.assertIn("committed", msg)
            self.assertIn("prune", msg)

    def test_discard_refuses_active_without_force(self):
        """Discarding an active node mid-run is racy. Must require --force."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init_git_repo(root)
            _build_graph_workspace(root, {
                "exp_0001": _make_node("exp_0001", "root", "active"),
            })
            with self.assertRaises(RuntimeError) as ctx:
                self._run_discard(root, "exp_0001")
            self.assertIn("active", str(ctx.exception).lower())
            self.assertIn("force", str(ctx.exception).lower())

    def test_discard_refuses_node_with_non_discarded_children(self):
        """Discarding a node with live children orphans the children's
        parent reference. Must refuse."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init_git_repo(root)
            _build_graph_workspace(root, {
                "exp_0001": _make_node("exp_0001", "root", "evaluated"),
                "exp_0002": _make_node("exp_0002", "exp_0001", "active"),
            })
            with self.assertRaises(RuntimeError) as ctx:
                self._run_discard(root, "exp_0001")
            msg = str(ctx.exception).lower()
            self.assertIn("child", msg)

    def test_discard_allows_evaluated_node(self):
        """Sanity: evaluated nodes without children should still be
        discardable (no regression in the happy path)."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init_git_repo(root)
            _build_graph_workspace(root, {
                "exp_0001": _make_node("exp_0001", "root", "evaluated"),
            })
            # Should NOT raise; backend.discard() is a no-op for non-existent worktree
            try:
                rc = self._run_discard(root, "exp_0001")
                self.assertEqual(rc, 0)
            except Exception as exc:
                # The only acceptable failure here is the backend call itself
                # (mock worktree doesn't exist); the guard logic must not raise.
                self.assertNotIn("committed", str(exc).lower())
                self.assertNotIn("active", str(exc).lower())

    def test_discard_allows_failed_node(self):
        """Sanity: failed nodes should still be discardable."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init_git_repo(root)
            _build_graph_workspace(root, {
                "exp_0001": _make_node("exp_0001", "root", "failed"),
            })
            try:
                rc = self._run_discard(root, "exp_0001")
                self.assertEqual(rc, 0)
            except Exception as exc:
                self.assertNotIn("committed", str(exc).lower())
                self.assertNotIn("active", str(exc).lower())

    def test_discard_preserves_declared_worktree_artifacts(self):
        """Registered result/trace artifacts should survive worktree cleanup."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init_git_repo(root)

            worktree = root / ".evo" / "run_0000" / "worktrees" / "exp_0001"
            (worktree / "final_model").mkdir(parents=True)
            (worktree / "final_model" / "adapter.bin").write_text("weights")
            (worktree / "logs").mkdir()
            (worktree / "logs" / "train.log").write_text("loss=0.1")

            _build_graph_workspace(root, {
                "exp_0001": _make_node(
                    "exp_0001", "root", "evaluated",
                    worktree=str(worktree), current_attempt=1, score=0.1,
                ),
            })
            exp_dir = root / ".evo" / "run_0000" / "experiments" / "exp_0001"
            attempt = exp_dir / "attempts" / "001"
            traces = attempt / "traces"
            traces.mkdir(parents=True)
            (attempt / "result.json").write_text(json.dumps({
                "score": 0.1,
                "artifacts": {"training-log": "logs/train.log"},
            }))
            (traces / "task_main.json").write_text(json.dumps({
                "task_id": "main",
                "score": 0.1,
                "artifacts": {"checkpoint": "final_model"},
            }))

            rc = self._run_discard(root, "exp_0001", reason="eval config mismatch")
            self.assertEqual(rc, 0)
            self.assertFalse(worktree.exists())

            result = json.loads((exp_dir / "result.json").read_text())
            self.assertEqual(result["status"], "discarded")
            self.assertEqual(
                result["artifact_manifest"],
                "artifacts/discarded/manifest.json",
            )
            preserved = result["preserved_artifacts"]
            self.assertEqual({item["label"] for item in preserved}, {"checkpoint", "training-log"})

            stored = {item["label"]: exp_dir / item["stored_path"] for item in preserved}
            self.assertEqual((stored["checkpoint"] / "adapter.bin").read_text(), "weights")
            self.assertEqual(stored["training-log"].read_text(), "loss=0.1")

            manifest = json.loads(
                (exp_dir / "artifacts" / "discarded" / "manifest.json").read_text()
            )
            self.assertEqual(len(manifest["artifacts"]), 2)


class TestPruneAcceptsEvaluated(unittest.TestCase):
    """Stage 3b: prune loosened to accept evaluated nodes too."""

    def _run_prune(self, root: Path, exp_id: str, reason: str = "test"):
        from evo.cli import cmd_prune
        import os
        prev = os.getcwd()
        os.chdir(root)
        try:
            ns = argparse.Namespace(exp_id=exp_id, reason=reason)
            return cmd_prune(ns)
        finally:
            os.chdir(prev)

    def test_prune_accepts_evaluated_node(self):
        """Pre-fix: prune required committed-only and rejected evaluated.
        Post-fix: evaluated is accepted (status flips to pruned)."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init_git_repo(root)
            _build_graph_workspace(root, {
                "exp_0001": _make_node("exp_0001", "root", "evaluated", score=0.5),
            })
            rc = self._run_prune(root, "exp_0001")
            self.assertEqual(rc, 0)

            # Verify status flipped
            from evo import core
            graph = core.load_graph(root)
            self.assertEqual(graph["nodes"]["exp_0001"]["status"], "pruned")
            self.assertEqual(graph["nodes"]["exp_0001"]["pruned_reason"], "test")

    def test_prune_still_accepts_committed_node(self):
        """Sanity: existing happy path unchanged."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init_git_repo(root)
            _build_graph_workspace(root, {
                "exp_0001": _make_node("exp_0001", "root", "committed",
                                       score=0.7, commit="abc"),
            })
            rc = self._run_prune(root, "exp_0001")
            self.assertEqual(rc, 0)

    def test_prune_rejects_active_node(self):
        """Active nodes still can't be pruned (running experiments)."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init_git_repo(root)
            _build_graph_workspace(root, {
                "exp_0001": _make_node("exp_0001", "root", "active"),
            })
            with self.assertRaises(RuntimeError):
                self._run_prune(root, "exp_0001")

    def test_prune_rejects_discarded_node(self):
        """Discarded nodes can't be pruned (already terminal)."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init_git_repo(root)
            _build_graph_workspace(root, {
                "exp_0001": _make_node("exp_0001", "root", "discarded"),
            })
            with self.assertRaises(RuntimeError):
                self._run_prune(root, "exp_0001")

    def test_prune_without_reason_succeeds_and_warns(self):
        """--reason is optional. The flip still happens; pruned_reason stays
        None (matching the initial state and what `evo restore` resets to);
        stderr warns so the omission is visible without breaking the call."""
        import io
        from unittest.mock import patch
        from evo import core
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init_git_repo(root)
            _build_graph_workspace(root, {
                "exp_0001": _make_node("exp_0001", "root", "committed",
                                       score=0.7, commit="abc"),
            })
            err = io.StringIO()
            with patch("sys.stderr", err):
                rc = self._run_prune(root, "exp_0001", reason=None)
            self.assertEqual(rc, 0)

            graph = core.load_graph(root)
            self.assertEqual(graph["nodes"]["exp_0001"]["status"], "pruned")
            self.assertIsNone(graph["nodes"]["exp_0001"]["pruned_reason"],
                              "empty state must be None — all readers truthy-check, "
                              "so a sentinel string would falsely render in dashboards")
            self.assertIn("WARNING", err.getvalue())
            self.assertIn("exp_0001", err.getvalue())


class TestRestore(unittest.TestCase):
    """Stage 3c: evo restore covers pruned→committed and discarded→committed."""

    def _run_restore(self, root: Path, exp_id: str):
        from evo.cli import cmd_restore
        import os
        prev = os.getcwd()
        os.chdir(root)
        try:
            ns = argparse.Namespace(exp_id=exp_id)
            return cmd_restore(ns)
        finally:
            os.chdir(prev)

    def test_restore_pruned_node_flips_status(self):
        """Restoring a pruned node flips status back to committed.
        Frontier eligibility returns; pruned_reason cleared."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init_git_repo(root)
            _build_graph_workspace(root, {
                "exp_0001": _make_node(
                    "exp_0001", "root", "pruned",
                    score=0.7, commit="abc",
                    pruned_reason="exhausted",
                ),
            })
            rc = self._run_restore(root, "exp_0001")
            self.assertEqual(rc, 0)

            from evo import core
            graph = core.load_graph(root)
            self.assertEqual(graph["nodes"]["exp_0001"]["status"], "committed")
            self.assertIsNone(graph["nodes"]["exp_0001"].get("pruned_reason"))

    def test_restore_discarded_node_recreates_branch(self):
        """Restoring a discarded node looks up `refs/evo-anchor/<run>/<exp>`,
        recreates the regular branch from it, and flips status."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            base_commit = _init_git_repo(root)

            # Make a commit on the experiment branch
            subprocess.run(
                ["git", "checkout", "-q", "-b", "evo/run_0000/exp_0001"],
                cwd=root, check=True,
            )
            (root / "experiment.txt").write_text("change\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "exp"],
                cwd=root, check=True,
            )
            exp_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, check=True,
                capture_output=True, text=True,
            ).stdout.strip()

            # Anchor the commit (this is what cmd_run does for worktree)
            from evo.cli import _anchor_commit_ref
            _anchor_commit_ref(root, "run_0000", "exp_0001", exp_commit)

            # Simulate discard: delete the regular branch
            subprocess.run(["git", "checkout", "-q", base_commit], cwd=root, check=True)
            subprocess.run(
                ["git", "branch", "-D", "evo/run_0000/exp_0001"],
                cwd=root, check=True,
            )

            # Build graph with discarded node referencing the (now-deleted)
            # branch and the surviving anchor.
            _build_graph_workspace(root, {
                "exp_0001": _make_node(
                    "exp_0001", "root", "discarded",
                    score=0.7, commit=exp_commit,
                    discard_reason="bad idea, retroactively wrong",
                ),
            })

            # Restore
            rc = self._run_restore(root, "exp_0001")
            self.assertEqual(rc, 0)

            # Status flipped
            from evo import core
            graph = core.load_graph(root)
            self.assertEqual(graph["nodes"]["exp_0001"]["status"], "committed")
            self.assertIsNone(graph["nodes"]["exp_0001"].get("discard_reason"))

            # Regular branch recreated, pointing at the right commit
            br = subprocess.run(
                ["git", "rev-parse", "refs/heads/evo/run_0000/exp_0001"],
                cwd=root, check=True, capture_output=True, text=True,
            )
            self.assertEqual(br.stdout.strip(), exp_commit)

    def test_restore_discarded_without_anchor_errors(self):
        """If `refs/evo-anchor/<run>/<exp>` doesn't exist (commit was lost), restore
        must error and point the user at the diff.patch fallback."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init_git_repo(root)
            _build_graph_workspace(root, {
                "exp_0001": _make_node(
                    "exp_0001", "root", "discarded",
                    score=0.7, commit="abcdef0123456789abcdef0123456789abcdef01",
                ),
            })
            with self.assertRaises(RuntimeError) as ctx:
                self._run_restore(root, "exp_0001")
            self.assertIn("diff.patch", str(ctx.exception).lower())

    def test_restore_rejects_already_committed_node(self):
        """Restoring something that's already committed is a no-op error."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init_git_repo(root)
            _build_graph_workspace(root, {
                "exp_0001": _make_node(
                    "exp_0001", "root", "committed",
                    score=0.7, commit="abc",
                ),
            })
            with self.assertRaises(RuntimeError):
                self._run_restore(root, "exp_0001")

    def test_restore_rejects_active_node(self):
        """Active nodes can't be restored — they were never killed."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init_git_repo(root)
            _build_graph_workspace(root, {
                "exp_0001": _make_node("exp_0001", "root", "active"),
            })
            with self.assertRaises(RuntimeError):
                self._run_restore(root, "exp_0001")


@unittest.skipIf(
    sys.platform == "win32",
    "POSIX-only: TemporaryDirectory cleanup recurses while git index/pack "
    "files are still held by background processes on Windows",
)
class TestEvoRunWritesAnchorRef(unittest.TestCase):
    """Stage 2 integration: a real `evo init` + `evo new` + `evo run` cycle
    on the worktree backend must write `refs/evo-anchor/<run>/<exp>` so the commit
    survives `evo discard`. This is the production code-path proof for the
    refactor that lifted `_anchor_commit_ref` out of the `if remote and commit:`
    block."""

    def _run_evo(self, root: Path, args: list[str]) -> subprocess.CompletedProcess:
        """Invoke the evo CLI as a subprocess from `root`."""
        import sys as _sys
        return subprocess.run(
            [_sys.executable, "-c",
             "from evo.cli import main; import sys; sys.exit(main())",
             *args],
            cwd=root, check=False, capture_output=True, text=True,
            env={**__import__("os").environ, "PYTHONPATH": str(
                Path(__file__).resolve().parents[2] / "plugins" / "evo" / "src"
            )},
        )

    def test_worktree_run_writes_anchor_ref_at_commit(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            # Set up a real git repo with a benchmark that emits a high score.
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
            subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
            (root / "agent.py").write_text("# agent\n")
            (root / "benchmark.py").write_text(
                "import json, os, pathlib\n"
                "p = pathlib.Path(os.environ.get('EVO_RESULT_PATH') or 'result.json')\n"
                "p.parent.mkdir(parents=True, exist_ok=True)\n"
                "p.write_text(json.dumps({'tasks': {'t1': 0.9}, 'score': 0.9}))\n"
                "print('score=0.9')\n"
            )
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"],
                cwd=root, check=True,
            )

            # init evo
            r = self._run_evo(root, [
                "init", "--target", "agent.py",
                "--benchmark", f"{PY} benchmark.py",
                "--metric", "max", "--host", "generic",
            ])
            self.assertEqual(r.returncode, 0, f"init failed: {r.stderr}")

            # allocate child experiment
            r = self._run_evo(root, [
                "new", "--parent", "root", "-m", "test hypothesis",
            ])
            self.assertEqual(r.returncode, 0, f"new failed: {r.stderr}")

            # find the allocated exp_id from the graph
            from evo import core
            graph = core.load_graph(root)
            exp_ids = [nid for nid in graph["nodes"] if nid != "root"]
            self.assertEqual(len(exp_ids), 1)
            exp_id = exp_ids[0]

            # write a change so the commit isn't a no-op
            wt = Path(graph["nodes"][exp_id]["worktree"])
            (wt / "agent.py").write_text("# agent\n# experiment change\n")

            # run the experiment
            r = self._run_evo(root, ["run", exp_id])
            # Either commit or evaluated outcome — check anchor only on commit
            # (which is what happens here since score=0.9 > parent's 0.0)
            self.assertEqual(r.returncode, 0, f"run failed: {r.stderr}\n{r.stdout}")
            self.assertIn("COMMITTED", r.stdout, f"unexpected outcome: {r.stdout}")

            # The anchor ref must exist in the orchestrator's main repo
            ref = subprocess.run(
                ["git", "rev-parse", f"refs/evo-anchor/run_0000/{exp_id}"],
                cwd=root, check=False, capture_output=True, text=True,
            )
            self.assertEqual(
                ref.returncode, 0,
                f"anchor ref refs/evo-anchor/run_0000/{exp_id} was NOT written by "
                f"`evo run` on worktree backend. This is the Stage 2 fix; if "
                f"this fails, the lift-out-of-`if remote` regressed.\n"
                f"stderr: {ref.stderr}"
            )

            # And it must point at the actual experiment commit
            graph = core.load_graph(root)
            commit = graph["nodes"][exp_id]["commit"]
            self.assertEqual(ref.stdout.strip(), commit)


@unittest.skipIf(
    sys.platform == "win32",
    "POSIX-only: TemporaryDirectory cleanup recurses while git index/pack "
    "files are still held by background processes on Windows",
)
class TestNewAfterRestoreWorks(unittest.TestCase):
    """Stage 3c integration: after `evo restore` un-discards a node, you can
    actually allocate a child via `evo new --parent <restored_id>`. Proves
    that the recreated `refs/heads/<branch>` is valid for `git worktree add`."""

    def setUp(self):
        self._dashboard_roots: list[Path] = []

    def tearDown(self):
        # Kill any dashboard processes spawned by `evo init` so subsequent
        # tests don't exhaust the dashboard port range (8080-8099).
        import os, signal
        for root in self._dashboard_roots:
            pid_file = root / ".evo" / "dashboard.pid"
            if pid_file.exists():
                try:
                    pid = int(pid_file.read_text().strip())
                    os.kill(pid, signal.SIGTERM)
                except (OSError, ValueError):
                    pass

    def _run_evo(self, root: Path, args: list[str]) -> subprocess.CompletedProcess:
        import sys as _sys
        # Track root so tearDown can kill the dashboard later.
        if args and args[0] == "init" and root not in self._dashboard_roots:
            self._dashboard_roots.append(root)
        return subprocess.run(
            [_sys.executable, "-c",
             "from evo.cli import main; import sys; sys.exit(main())",
             *args],
            cwd=root, check=False, capture_output=True, text=True,
            env={**__import__("os").environ, "PYTHONPATH": str(
                Path(__file__).resolve().parents[2] / "plugins" / "evo" / "src"
            )},
        )

    def test_evo_new_works_after_restore(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
            subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
            (root / "agent.py").write_text("# agent\n")
            (root / "benchmark.py").write_text(
                "import json, os, pathlib\n"
                "p = pathlib.Path(os.environ.get('EVO_RESULT_PATH') or 'result.json')\n"
                "p.parent.mkdir(parents=True, exist_ok=True)\n"
                "p.write_text(json.dumps({'tasks': {'t1': 0.9}, 'score': 0.9}))\n"
                "print('score=0.9')\n"
            )
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"],
                cwd=root, check=True,
            )

            r = self._run_evo(root, [
                "init", "--target", "agent.py",
                "--benchmark", f"{PY} benchmark.py",
                "--metric", "max", "--host", "generic",
            ])
            self.assertEqual(r.returncode, 0, f"init: {r.stderr}")

            r = self._run_evo(root, ["new", "--parent", "root", "-m", "h1"])
            self.assertEqual(r.returncode, 0, f"new: {r.stderr}")

            from evo import core
            graph = core.load_graph(root)
            exp_id = next(nid for nid in graph["nodes"] if nid != "root")
            wt = Path(graph["nodes"][exp_id]["worktree"])
            (wt / "agent.py").write_text("# agent\n# experiment change\n")

            r = self._run_evo(root, ["run", exp_id])
            self.assertEqual(r.returncode, 0, f"run: {r.stderr}")
            self.assertIn("COMMITTED", r.stdout)

            # Now manually walk through discard → restore → new
            # Discard: refused for committed, so prune first
            r = self._run_evo(root, ["prune", exp_id, "--reason", "test"])
            self.assertEqual(r.returncode, 0, f"prune: {r.stderr}")

            r = self._run_evo(root, ["restore", exp_id])
            self.assertEqual(r.returncode, 0, f"restore: {r.stderr}")

            # Verify status is committed again
            graph = core.load_graph(root)
            self.assertEqual(graph["nodes"][exp_id]["status"], "committed")

            # Allocate a child from the restored node
            r = self._run_evo(root, ["new", "--parent", exp_id, "-m", "child"])
            self.assertEqual(
                r.returncode, 0,
                f"`evo new --parent {exp_id}` failed after restore. "
                f"This means the recreated branch ref or commit reachability "
                f"is broken.\nstderr: {r.stderr}"
            )

            # The child node should exist
            graph = core.load_graph(root)
            children = graph["nodes"][exp_id].get("children", [])
            self.assertEqual(len(children), 1, f"child not allocated: {children}")


class TestLegacyAnchorFallback(unittest.TestCase):
    """Backwards compatibility: workspaces created before the v3 namespace
    rename used `refs/evo/<run>/<exp>` for remote-mode anchors. After
    renaming to `refs/evo-anchor/<run>/<exp>`, `evo restore` on those legacy
    nodes must still find the commit via the old ref name.
    """

    def test_restore_finds_legacy_refs_evo_namespace(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            base_commit = _init_git_repo(root)

            # Make an experiment commit
            subprocess.run(
                ["git", "checkout", "-q", "-b", "evo/run_0000/exp_legacy"],
                cwd=root, check=True,
            )
            (root / "experiment.txt").write_text("legacy change\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "exp"],
                cwd=root, check=True,
            )
            exp_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, check=True,
                capture_output=True, text=True,
            ).stdout.strip()

            # Write the LEGACY anchor namespace (refs/evo/...) — this is what
            # the pre-v3 code wrote for remote-mode commits.
            subprocess.run(
                ["git", "update-ref", "refs/evo/run_0000/exp_legacy", exp_commit],
                cwd=root, check=True,
            )

            # Simulate discard: delete the regular branch
            subprocess.run(["git", "checkout", "-q", base_commit], cwd=root, check=True)
            subprocess.run(
                ["git", "branch", "-D", "evo/run_0000/exp_legacy"],
                cwd=root, check=True,
            )

            # Build graph with the discarded legacy node
            _build_graph_workspace(root, {
                "exp_legacy": _make_node(
                    "exp_legacy", "root", "discarded",
                    score=0.7, commit=exp_commit,
                ),
            })

            # Restore should find the commit via the legacy namespace
            from evo.cli import cmd_restore
            import os
            prev = os.getcwd()
            os.chdir(root)
            try:
                rc = cmd_restore(argparse.Namespace(exp_id="exp_legacy"))
                self.assertEqual(rc, 0)
            finally:
                os.chdir(prev)

            # Status flipped
            from evo import core
            graph = core.load_graph(root)
            self.assertEqual(graph["nodes"]["exp_legacy"]["status"], "committed")

            # Branch recreated and points at the right commit
            br = subprocess.run(
                ["git", "rev-parse", "refs/heads/evo/run_0000/exp_legacy"],
                cwd=root, check=True, capture_output=True, text=True,
            )
            self.assertEqual(br.stdout.strip(), exp_commit)


@unittest.skipIf(
    sys.platform == "win32",
    "POSIX-only: TemporaryDirectory cleanup recurses while git index/pack "
    "files are still held by background processes on Windows",
)
class TestPoolBackendAnchor(unittest.TestCase):
    """Stage 4: pool-committed nodes must mirror their commit into the main
    repo at run-time, write `refs/evo-anchor/<run>/<exp>` like worktree+remote
    do, and survive both `evo discard`'s lease release and aggressive `git gc`
    in the main repo. Without this, pool commits live only in slot dirs and
    are unreachable from the orchestrator's `cwd=root`."""

    def _run_evo(self, root: Path, args: list[str]) -> subprocess.CompletedProcess:
        import sys as _sys
        return subprocess.run(
            [_sys.executable, "-c",
             "from evo.cli import main; import sys; sys.exit(main())",
             *args],
            cwd=root, check=False, capture_output=True, text=True,
            env={**__import__("os").environ, "PYTHONPATH": str(
                Path(__file__).resolve().parents[2] / "plugins" / "evo" / "src"
            )},
        )

    def setUp(self):
        self._dashboard_roots: list[Path] = []

    def tearDown(self):
        import os, signal
        for root in self._dashboard_roots:
            pid_file = root / ".evo" / "dashboard.pid"
            if pid_file.exists():
                try:
                    pid = int(pid_file.read_text().strip())
                    os.kill(pid, signal.SIGTERM)
                except (OSError, ValueError):
                    pass

    def _setup_pool_workspace(self, td: Path) -> tuple[Path, list[Path]]:
        """Initialize a main repo + 2 pre-built pool slots cloned from it."""
        main_repo = td / "main"
        main_repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=main_repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=main_repo, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=main_repo, check=True)
        subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=main_repo, check=True)
        (main_repo / "agent.py").write_text("# agent\n")
        (main_repo / "benchmark.py").write_text(
            "import json, os, pathlib\n"
            "p = pathlib.Path(os.environ.get('EVO_RESULT_PATH') or 'result.json')\n"
            "p.parent.mkdir(parents=True, exist_ok=True)\n"
            "p.write_text(json.dumps({'tasks': {'t1': 0.9}, 'score': 0.9}))\n"
            "print('score=0.9')\n"
        )
        subprocess.run(["git", "add", "-A"], cwd=main_repo, check=True)
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"],
            cwd=main_repo, check=True,
        )

        slots: list[Path] = []
        for i in range(2):
            slot = td / f"slot-{i}"
            subprocess.run(
                ["git", "clone", "-q", str(main_repo), str(slot)],
                check=True,
            )
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=slot, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=slot, check=True)
            subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=slot, check=True)
            slots.append(slot)
        return main_repo, slots

    def test_pool_commit_anchored_in_main_repo(self):
        """After a pool experiment commits, refs/evo-anchor/<run>/<exp>
        in the main repo must point at a commit the main repo can resolve."""
        with tempfile.TemporaryDirectory() as d:
            td = Path(d)
            main_repo, slots = self._setup_pool_workspace(td)
            self._dashboard_roots.append(main_repo)

            slot_arg = ",".join(str(s) for s in slots)
            r = self._run_evo(main_repo, [
                "init", "--target", "agent.py",
                "--benchmark", f"{PY} benchmark.py",
                "--metric", "max", "--host", "generic",
            ])
            self.assertEqual(r.returncode, 0, r.stderr)
            r = self._run_evo(main_repo, [
                "config", "backend", "pool", "--workspaces", slot_arg,
            ])
            self.assertEqual(r.returncode, 0, r.stderr)
            r = self._run_evo(main_repo, ["new", "--parent", "root", "-m", "test"])
            self.assertEqual(r.returncode, 0, r.stderr)

            # Edit + run inside the slot.
            from evo import core
            graph = core.load_graph(main_repo)
            exp_id = next(nid for nid in graph["nodes"] if nid != "root")
            wt = Path(graph["nodes"][exp_id]["worktree"])
            (wt / "agent.py").write_text("# agent\n# pool experiment edit\n")

            r = self._run_evo(main_repo, ["run", exp_id])
            self.assertEqual(r.returncode, 0, f"run: {r.stderr}\n{r.stdout}")
            self.assertIn("COMMITTED", r.stdout, f"unexpected: {r.stdout}")

            # Anchor must exist in MAIN repo and be resolvable to a real
            # commit (the main repo must have the objects, not just the ref).
            anchor = subprocess.run(
                ["git", "rev-parse", f"refs/evo-anchor/run_0000/{exp_id}"],
                cwd=main_repo, check=False, capture_output=True, text=True,
            )
            self.assertEqual(anchor.returncode, 0,
                             f"anchor missing in main repo: {anchor.stderr}")
            commit_hash = anchor.stdout.strip()
            cat = subprocess.run(
                ["git", "cat-file", "-e", commit_hash],
                cwd=main_repo, check=False, capture_output=True,
            )
            self.assertEqual(
                cat.returncode, 0,
                f"anchor points at commit {commit_hash} but main repo "
                f"doesn't have the object — Stage 4 mirror didn't work"
            )

    def test_pool_commit_survives_main_repo_gc(self):
        """The full point of the anchor: even if every slot were wiped and
        main-repo `git gc --prune=now` ran, the anchor keeps the commit
        alive. Simulate by running gc and verifying the commit is still
        reachable."""
        with tempfile.TemporaryDirectory() as d:
            td = Path(d)
            main_repo, slots = self._setup_pool_workspace(td)
            self._dashboard_roots.append(main_repo)

            slot_arg = ",".join(str(s) for s in slots)
            self._run_evo(main_repo, [
                "init", "--target", "agent.py",
                "--benchmark", f"{PY} benchmark.py",
                "--metric", "max", "--host", "generic",
            ])
            self._run_evo(main_repo, [
                "config", "backend", "pool", "--workspaces", slot_arg,
            ])
            self._run_evo(main_repo, ["new", "--parent", "root", "-m", "test"])

            from evo import core
            graph = core.load_graph(main_repo)
            exp_id = next(nid for nid in graph["nodes"] if nid != "root")
            wt = Path(graph["nodes"][exp_id]["worktree"])
            (wt / "agent.py").write_text("# agent\n# pool gc test\n")

            r = self._run_evo(main_repo, ["run", exp_id])
            self.assertIn("COMMITTED", r.stdout, r.stdout)

            # Resolve anchor → commit hash
            commit = subprocess.run(
                ["git", "rev-parse", f"refs/evo-anchor/run_0000/{exp_id}"],
                cwd=main_repo, check=True, capture_output=True, text=True,
            ).stdout.strip()

            # Run aggressive gc in MAIN REPO (not the slot)
            subprocess.run(
                ["git", "reflog", "expire", "--expire=now", "--all"],
                cwd=main_repo, check=True,
            )
            subprocess.run(
                ["git", "gc", "--prune=now", "--quiet"],
                cwd=main_repo, check=True,
            )

            # Commit must still be reachable in the main repo
            cat = subprocess.run(
                ["git", "cat-file", "-e", commit],
                cwd=main_repo, check=False, capture_output=True,
            )
            self.assertEqual(
                cat.returncode, 0,
                f"anchor failed to keep commit {commit[:8]} alive across "
                f"main-repo `git gc --prune=now`"
            )


if __name__ == "__main__":
    unittest.main()
