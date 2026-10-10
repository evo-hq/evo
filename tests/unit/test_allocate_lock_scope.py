"""allocate_experiment must not hold the graph lock across backend.allocate() (#98).

The slow work (git `worktree add`, remote sandbox provisioning) lives in
`backend.allocate()`. Holding the graph.json advisory lock across it
serializes concurrent `evo new` calls and makes later ones exceed the 10s
lock timeout (`LockTimeoutError`) even though nothing is wrong.

This test drives two concurrent allocations through a backend whose
`allocate()` overlaps in time, and asserts they actually run concurrently
(max observed concurrency == 2) and both succeed with distinct ids. On the
old code the graph lock serializes them (max concurrency == 1).
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))

import evo.backends as backends
from evo.core import init_workspace, allocate_experiment, load_graph


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@evo"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
    (root / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)


class _ConcurrencyProbingBackend:
    """allocate() overlaps in time and records the max concurrency seen."""

    def __init__(self):
        self.name = "probe"
        self._lock = threading.Lock()
        self._live = 0
        self.max_concurrent = 0

    def allocate(self, ctx):
        with self._lock:
            self._live += 1
            self.max_concurrent = max(self.max_concurrent, self._live)
        time.sleep(0.5)  # overlap window
        with self._lock:
            self._live -= 1
        return type("R", (), {
            "worktree": str(ctx.root), "branch": ctx.branch,
            "commit": "deadbeef", "extra": {},
        })()


def test_allocate_runs_concurrently_and_yields_distinct_ids(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    _init_git_repo(root)
    init_workspace(root, target="t.py", benchmark="echo hi", metric="max", gate=None)

    probe = _ConcurrencyProbingBackend()
    monkeypatch.setattr(backends, "load_backend", lambda *a, **k: probe)

    results: dict[str, dict] = {}
    errors: dict[str, Exception] = {}

    def worker(name):
        try:
            results[name] = allocate_experiment(root, parent_id="root", hypothesis="h")
        except Exception as e:  # noqa: BLE001
            errors[name] = e

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"allocation errored: {errors}"
    ids = {r["id"] for r in results.values()}
    assert len(ids) == 2, f"expected 2 distinct ids, got {ids}"
    assert probe.max_concurrent == 2, (
        f"allocations serialized (max_concurrent={probe.max_concurrent}); "
        "the graph lock is still held across backend.allocate()"
    )
    graph = load_graph(root)
    for exp_id in ids:
        assert exp_id in graph["nodes"]
        assert exp_id in graph["nodes"]["root"]["children"]


class _ParentMutatingBackend:
    """Simulates a concurrent mutation of the parent during the unlocked
    backend.allocate() window: it edits graph.json on disk before returning,
    exactly as a racing `evo discard`/`evo prune` process would."""

    def __init__(self, root, parent_id, action):
        self.name = "probe"
        self._root = root
        self._parent_id = parent_id
        self._action = action  # "delete" or "prune"
        self.torn_down = False

    def allocate(self, ctx):
        from evo.core import load_graph, save_graph
        g = load_graph(self._root)
        if self._action == "delete":
            g["nodes"].pop(self._parent_id, None)
            for n in g["nodes"].values():
                if self._parent_id in n.get("children", []):
                    n["children"].remove(self._parent_id)
        elif self._action == "prune":
            g["nodes"][self._parent_id]["status"] = "pruned"
        save_graph(self._root, g)
        return type("R", (), {
            "worktree": str(ctx.root / "wt"), "branch": ctx.branch,
            "commit": "deadbeef", "extra": {},
        })()

    def release_lease(self, ctx):
        self.torn_down = True

    def gc(self, ctx):
        self.torn_down = True
        return True


def _setup_with_parent(tmp_path):
    root = tmp_path.resolve()
    _init_git_repo(root)
    init_workspace(root, target="t.py", benchmark="echo hi", metric="max", gate=None)
    # Real allocation of a committable parent (child of root).
    parent = allocate_experiment(root, parent_id="root", hypothesis="p")
    return root, parent["id"]


def test_parent_deleted_during_allocation_raises_and_leaves_no_orphan(tmp_path, monkeypatch):
    root, parent_id = _setup_with_parent(tmp_path)
    probe = _ParentMutatingBackend(root, parent_id, "delete")
    monkeypatch.setattr(backends, "load_backend", lambda *a, **k: probe)

    import pytest
    with pytest.raises(RuntimeError):
        allocate_experiment(root, parent_id=parent_id, hypothesis="child")

    graph = load_graph(root)
    # No node may reference the now-deleted parent (no dangling-parent orphan).
    orphans = [n["id"] for n in graph["nodes"].values() if n.get("parent") == parent_id]
    assert orphans == [], f"orphan node(s) attached to a deleted parent: {orphans}"
    assert probe.torn_down, "allocated workspace was not released on the failure path"


def test_parent_pruned_during_allocation_is_rejected(tmp_path, monkeypatch):
    root, parent_id = _setup_with_parent(tmp_path)
    probe = _ParentMutatingBackend(root, parent_id, "prune")
    monkeypatch.setattr(backends, "load_backend", lambda *a, **k: probe)

    import pytest
    with pytest.raises(RuntimeError):
        allocate_experiment(root, parent_id=parent_id, hypothesis="child")

    graph = load_graph(root)
    children = graph["nodes"][parent_id].get("children", [])
    assert children == [], f"child attached under a pruned parent: {children}"
