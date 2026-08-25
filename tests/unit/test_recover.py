"""Unit tests for `evo recover` and stale-active surfacing in `evo status`.

Crash recovery (issue #6): when `evo run` crashes / the shell dies / the box
reboots mid-run, the node is left at `status: "active"` in graph.json with a
dead driver PID stamped in attempt_state.json. `evo recover` sweeps those,
marks the dead ones `failed` (reason "process disappeared") while leaving the
worktree + traces intact, and `evo status` surfaces them prominently.

Real filesystem, real subprocesses. A live PID is this test process
(guaranteed alive for the check); a dead PID comes from a child spawned to
exit immediately, then reaped.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@evo"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
    (root / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)


def _make_workspace(root: Path) -> None:
    _init_git_repo(root)
    from evo.core import init_workspace
    init_workspace(root, target="agent.py", benchmark="echo hi", metric="max", gate=None)


def _dead_pid() -> int:
    """A guaranteed-dead PID: spawn a process that exits immediately, reap it."""
    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait()
    return child.pid


def _inject_node(
    root: Path,
    exp_id: str,
    *,
    status: str = "active",
    attempt: int = 1,
    pid: int | None = None,
    backend: str | None = None,
    write_state: bool = True,
) -> None:
    """Inject a node into graph.json (optionally with attempt_state carrying a
    PID). `pid=None` + `write_state=False` means no attempt_state at all."""
    from evo.core import load_graph, save_graph, attempt_dir
    from evo.cli import _write_attempt_state
    worktree = root / f"wt-{exp_id}"
    worktree.mkdir(exist_ok=True)
    graph = load_graph(root)
    node = {
        "id": exp_id, "parent": "root", "children": [],
        "status": status, "score": None, "hypothesis": "recover-test",
        "branch": f"evo/run_0000/{exp_id}", "commit": None,
        "current_attempt": attempt, "worktree": str(worktree),
    }
    if backend is not None:
        node["backend"] = backend
    graph["nodes"][exp_id] = node
    graph["nodes"]["root"].setdefault("children", []).append(exp_id)
    save_graph(root, graph)
    attempt_dir(root, exp_id, attempt).mkdir(parents=True, exist_ok=True)
    if write_state:
        _write_attempt_state(
            root, exp_id, attempt,
            phase="benchmark", status="running",
            started_at="2026-01-01T00:00:00+00:00",
            extra={"pid": pid if pid is not None else 0},
        )


def _node_status(root: Path, exp_id: str) -> str:
    from evo.core import load_graph
    return load_graph(root)["nodes"][exp_id]["status"]


class _WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _make_workspace(self.root)
        self._prev_cwd = Path.cwd()
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self._prev_cwd)
        self._tmp.cleanup()

    def _recover(self, dry_run: bool = False) -> tuple[int, str]:
        from evo.cli import cmd_recover
        out = io.StringIO()
        with patch("sys.stdout", out):
            rc = cmd_recover(argparse.Namespace(dry_run=dry_run, json=False))
        return rc, out.getvalue()

    def _status(self) -> str:
        from evo.cli import cmd_status
        out = io.StringIO()
        with patch("sys.stdout", out):
            cmd_status(argparse.Namespace())
        return out.getvalue()


class TestRecover(_WorkspaceCase):
    def test_marks_dead_active_node_failed(self):
        _inject_node(self.root, "exp_0001", pid=_dead_pid())
        rc, _ = self._recover()
        self.assertEqual(rc, 0)
        self.assertEqual(_node_status(self.root, "exp_0001"), "failed")

    def test_failed_node_reason_says_process_disappeared(self):
        from evo.core import load_graph
        _inject_node(self.root, "exp_0001", pid=_dead_pid())
        self._recover()
        node = load_graph(self.root)["nodes"]["exp_0001"]
        self.assertIn("process disappeared", (node.get("error") or "").lower())

    def test_preserves_worktree_and_traces(self):
        from evo.core import attempt_traces_dir
        _inject_node(self.root, "exp_0001", pid=_dead_pid())
        traces = attempt_traces_dir(self.root, "exp_0001", 1)
        traces.mkdir(parents=True, exist_ok=True)
        (traces / "partial.log").write_text("half a run\n")
        self._recover()
        # Unlike discard, recover must not delete partial data.
        self.assertTrue((self.root / "wt-exp_0001").exists())
        self.assertTrue((traces / "partial.log").exists())

    def test_leaves_live_active_node_active(self):
        _inject_node(self.root, "exp_0001", pid=os.getpid())
        self._recover()
        self.assertEqual(_node_status(self.root, "exp_0001"), "active")

    def test_dry_run_reports_but_does_not_mutate(self):
        _inject_node(self.root, "exp_0001", pid=_dead_pid())
        rc, out = self._recover(dry_run=True)
        self.assertEqual(rc, 0)
        self.assertIn("exp_0001", out)
        self.assertEqual(_node_status(self.root, "exp_0001"), "active")

    def test_missing_attempt_state_treated_as_stale(self):
        # active node whose attempt never wrote state (crash before stamp)
        _inject_node(self.root, "exp_0001", write_state=False)
        self._recover()
        self.assertEqual(_node_status(self.root, "exp_0001"), "failed")

    def test_ignores_non_active_nodes(self):
        # a committed node with a (coincidentally dead) pid must not be touched
        _inject_node(self.root, "exp_0001", status="committed", pid=_dead_pid())
        self._recover()
        self.assertEqual(_node_status(self.root, "exp_0001"), "committed")

    def test_skips_remote_nodes(self):
        # remote backend has its own resume metadata; recover leaves it alone
        _inject_node(self.root, "exp_0001", pid=_dead_pid(), backend="remote")
        self._recover()
        self.assertEqual(_node_status(self.root, "exp_0001"), "active")

    def test_finalizes_result_json(self):
        from evo.core import experiment_result_path
        _inject_node(self.root, "exp_0001", pid=_dead_pid())
        self._recover()
        result = json.loads(experiment_result_path(self.root, "exp_0001").read_text())
        self.assertEqual(result["status"], "failed")


class TestStatusSurfacesStale(_WorkspaceCase):
    def test_status_reports_stale_active_count(self):
        _inject_node(self.root, "exp_0001", pid=_dead_pid())
        out = self._status()
        self.assertIn("stale", out.lower())
        self.assertIn("exp_0001", out)

    def test_status_does_not_flag_live_active(self):
        _inject_node(self.root, "exp_0001", pid=os.getpid())
        out = self._status()
        self.assertNotIn("exp_0001", out)


if __name__ == "__main__":
    unittest.main()
