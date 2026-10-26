"""Unit tests for the asset / reuse / classifier / abort CLI abstractions.

Covers the behavior the manual dry run exercised, so it's guarded in CI:
- evo discard --failure-class  -> recorded on the node
- evo new --from-artifact      -> resolver (dedup multi-source, ambiguity, missing)
- EVO_SEED_ARTIFACT/EVO_PARENT_POLICY in the run env when seeded
- preserve-on-discard records already-persistent artifacts as reusable
- evo abort tree-kill core (_descendant_pids finds the child tree)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

PY = sys.executable


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@evo"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    (root / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)


def _make_node(exp_id: str, parent: str, status: str, **kw) -> dict:
    base = {
        "id": exp_id, "parent": parent, "children": [], "status": status,
        "hypothesis": f"hyp {exp_id}", "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z", "eval_epoch": 1, "score": kw.get("score"),
        "branch": f"evo/run_0000/{exp_id}", "worktree": str(Path("/tmp") / f"evo-mock-{exp_id}"),
        "commit": kw.get("commit"), "pruned_reason": None, "gates": [],
        "current_attempt": 0, "notes": [],
    }
    base.update(kw)
    return base


def _build_workspace(root: Path, nodes: dict) -> None:
    from evo import core
    evo_dir = root / ".evo"
    run_dir = evo_dir / "run_0000"
    run_dir.mkdir(parents=True, exist_ok=True)
    (evo_dir / "meta.json").write_text(json.dumps({"active": "run_0000", "next_run": 1}))
    (run_dir / "config.json").write_text(json.dumps(
        {"metric": "max", "execution_backend": "worktree", "current_eval_epoch": 1}))
    graph = core.default_graph()
    for nid, node in nodes.items():
        graph["nodes"][nid] = node
        p = node.get("parent")
        if p and p in graph["nodes"]:
            graph["nodes"][p].setdefault("children", []).append(nid)
    (run_dir / "graph.json").write_text(json.dumps(graph))
    (run_dir / "annotations.json").write_text(json.dumps({"annotations": []}))
    (run_dir / "infra_log.json").write_text(json.dumps({"events": []}))


@contextmanager
def _cd(root: Path):
    prev = os.getcwd()
    os.chdir(root)
    try:
        yield
    finally:
        os.chdir(prev)


def _exp_dir(root: Path, exp: str) -> Path:
    from evo.core import experiments_dir_for
    return experiments_dir_for(root, exp)


def _write_manifest(root: Path, exp: str, artifacts: list[dict]) -> Path:
    dd = _exp_dir(root, exp) / "artifacts" / "discarded"
    dd.mkdir(parents=True, exist_ok=True)
    (dd / "manifest.json").write_text(
        json.dumps({"experiment_id": exp, "artifacts": artifacts, "skipped": []}))
    return dd


# --------------------------------------------------------------------------- #
# evo new --from-artifact : the resolver
# --------------------------------------------------------------------------- #
class TestResolvePreservedArtifact(unittest.TestCase):
    def _setup_one(self, root, *, label="checkpoint", n_sources=1):
        _init_git_repo(root)
        _build_workspace(root, {"exp_0001": _make_node("exp_0001", "root", "discarded")})
        dd = _write_manifest(root, "exp_0001", [
            {"label": label, "path": "ckpt", "source": f"src{i}",
             "stored_path": "artifacts/discarded/ckpt"} for i in range(n_sources)
        ])
        (dd / "ckpt").mkdir()  # the stored artifact must exist on disk
        (dd / "ckpt" / "weights").write_text("w")

    def test_single_artifact_resolves(self):
        from evo.cli import _resolve_preserved_artifact
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); self._setup_one(root)
            with _cd(root):
                r = _resolve_preserved_artifact(root, "exp_0001")
            self.assertEqual(r["source_exp"], "exp_0001")
            # r["path"] is a native filesystem path (backslashes on Windows);
            # normalize separators before comparing the suffix.
            self.assertTrue(r["path"].replace(os.sep, "/").endswith("artifacts/discarded/ckpt"))

    def test_multi_source_duplicates_are_deduped(self):
        # the manifest records the same artifact once per declaring source — must
        # NOT be mistaken for multiple distinct artifacts (the bug the dry run hit).
        from evo.cli import _resolve_preserved_artifact
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); self._setup_one(root, n_sources=3)
            with _cd(root):
                r = _resolve_preserved_artifact(root, "exp_0001")  # must not raise
            self.assertEqual(r["label"], "checkpoint")

    def test_ambiguous_distinct_artifacts_raise_without_label(self):
        from evo.cli import _resolve_preserved_artifact
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); _init_git_repo(root)
            _build_workspace(root, {"exp_0001": _make_node("exp_0001", "root", "discarded")})
            dd = _write_manifest(root, "exp_0001", [
                {"label": "ckpt", "path": "a", "stored_path": "artifacts/discarded/a"},
                {"label": "adapter", "path": "b", "stored_path": "artifacts/discarded/b"},
            ])
            (dd / "a").mkdir(); (dd / "b").mkdir()
            with _cd(root), self.assertRaises(RuntimeError) as ctx:
                _resolve_preserved_artifact(root, "exp_0001")
            self.assertIn("multiple", str(ctx.exception).lower())
            # ...but naming the label resolves it
            with _cd(root):
                r = _resolve_preserved_artifact(root, "exp_0001:adapter")
            self.assertEqual(r["label"], "adapter")

    def test_missing_manifest_raises(self):
        from evo.cli import _resolve_preserved_artifact
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); _init_git_repo(root)
            _build_workspace(root, {"exp_0001": _make_node("exp_0001", "root", "discarded")})
            with _cd(root), self.assertRaises(RuntimeError):
                _resolve_preserved_artifact(root, "exp_0001")


# --------------------------------------------------------------------------- #
# EVO_SEED_ARTIFACT / EVO_PARENT_POLICY in the run env
# --------------------------------------------------------------------------- #
class TestSeedArtifactEnv(unittest.TestCase):
    def _env_for(self, root, node):
        from evo.cli import _runtime_env_for_attempt
        from evo.core import load_config
        _build_workspace(root, {node["id"]: node})
        with _cd(root):
            return _runtime_env_for_attempt(
                root, load_config(root), exp_id=node["id"], attempt_label="001",
                worktree=Path(node["worktree"]), env_traces_dir="t",
                env_result_path="r.json", env_checkpoint_dir="ck",
            )

    def test_seed_env_set_when_from_artifact(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); _init_git_repo(root)
            node = _make_node("exp_0002", "root", "active",
                              from_artifact={"path": "/tmp/seed/ckpt", "source_exp": "exp_0001"})
            env = self._env_for(root, node)
            self.assertEqual(env.get("EVO_SEED_ARTIFACT"), "/tmp/seed/ckpt")
            self.assertEqual(env.get("EVO_PARENT_POLICY"), "/tmp/seed/ckpt")

    def test_seed_env_absent_without_from_artifact(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); _init_git_repo(root)
            env = self._env_for(root, _make_node("exp_0003", "root", "active"))
            self.assertNotIn("EVO_SEED_ARTIFACT", env)
            self.assertNotIn("EVO_PARENT_POLICY", env)


# --------------------------------------------------------------------------- #
# preserve-on-discard records already-persistent artifacts as reusable
# --------------------------------------------------------------------------- #
class TestPreserveAlreadyPersistent(unittest.TestCase):
    def test_already_persistent_recorded_as_reusable(self):
        from evo.cli import _preserve_discard_artifacts
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); _init_git_repo(root)
            _build_workspace(root, {"exp_0001": _make_node("exp_0001", "root", "discarded")})
            # an artifact written under the experiment record (e.g. EVO_CHECKPOINT_DIR) —
            # durable, no copy needed, but must be recorded as reusable (not just skipped).
            exp_dir = _exp_dir(root, "exp_0001")
            ckpt = exp_dir / "attempts" / "001" / "checkpoints"
            ckpt.mkdir(parents=True)
            (ckpt / "weights").write_text("w")
            worktree = Path(d) / "wt"; worktree.mkdir()
            node = _make_node("exp_0001", "root", "discarded", worktree=str(worktree),
                              benchmark_result={"artifacts": {"checkpoint": str(ckpt)}})
            with _cd(root):
                res = _preserve_discard_artifacts(root, node)
            labels = [a["label"] for a in res["artifacts"]]
            self.assertIn("checkpoint", labels)
            self.assertTrue(any(a.get("already_persistent") for a in res["artifacts"]))


# --------------------------------------------------------------------------- #
# evo discard --failure-class
# --------------------------------------------------------------------------- #
class TestFailureClass(unittest.TestCase):
    def test_failure_class_recorded_on_node(self):
        from evo.cli import cmd_discard
        from evo.core import load_graph
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); _init_git_repo(root)
            _build_workspace(root, {"exp_0001": _make_node("exp_0001", "root", "evaluated")})
            with _cd(root):
                cmd_discard(argparse.Namespace(
                    exp_id="exp_0001", reason="scorer bug", force=False, failure_class="eval"))
                node = load_graph(root)["nodes"]["exp_0001"]
            self.assertEqual(node["status"], "discarded")
            self.assertEqual(node["failure_class"], "eval")


# --------------------------------------------------------------------------- #
# task-skills config field
# --------------------------------------------------------------------------- #
class TestTaskSkillsConfig(unittest.TestCase):
    def _set(self, root, value):
        from evo.cli import cmd_config_set
        with _cd(root):
            cmd_config_set(argparse.Namespace(field="task-skills", value=value))
        return json.loads((root / ".evo" / "run_0000" / "config.json").read_text())

    def test_set_normalizes_comma_list(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); _init_git_repo(root)
            _build_workspace(root, {})
            cfg = self._set(root, "finetuning, observability")
            self.assertEqual(cfg["task_skills"], ["finetuning", "observability"])

    def test_clear_sets_none(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); _init_git_repo(root)
            _build_workspace(root, {})
            self._set(root, "finetuning")
            cfg = self._set(root, "")
            self.assertIsNone(cfg["task_skills"])


# --------------------------------------------------------------------------- #
# evo abort tree-kill core
# --------------------------------------------------------------------------- #
class TestDescendantPids(unittest.TestCase):
    def test_finds_child_tree(self):
        from evo.cli import _descendant_pids
        # parent python spawns a child python that sleeps, then sleeps itself.
        # A python child (not the `sleep` binary) keeps this portable to Windows.
        # The parent records the child's pid in a file (not a blocking pipe read,
        # which could hang the job), so the child can be killed in `finally` even
        # when _descendant_pids finds nothing.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            pidfile = os.path.join(d, "child.pid")
            tmp = pidfile + ".tmp"
            spawn = (
                "import os, subprocess, sys, time; "
                "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                f"open({tmp!r}, 'w').write(str(c.pid)); os.replace({tmp!r}, {pidfile!r}); "
                "time.sleep(60)"
            )
            parent = subprocess.Popen([PY, "-c", spawn])
            kids = []
            try:
                # Poll instead of sleeping once: on a loaded Windows runner the child
                # can be slow to spawn, and the PowerShell process-table query
                # (capped at 10s inside _descendant_pids) can time out and return []
                # on an attempt. The tree above lives 60s, so it outlives this 45s
                # window (a shorter-lived tree would exit before the polling ends).
                deadline = time.monotonic() + 45
                while not kids and time.monotonic() < deadline:
                    time.sleep(1)
                    kids = _descendant_pids(parent.pid)
                self.assertTrue(len(kids) >= 1, f"expected >=1 descendant, got {kids}")
            finally:
                try:
                    child_pid = int(Path(pidfile).read_text())
                except (OSError, ValueError):
                    child_pid = None  # helper never reported one
                # captured before killing the parent (descendants reparent on kill);
                # os.kill(pid, 9) maps to TerminateProcess on Windows.
                for pid in {p for p in (child_pid, *kids) if p}:
                    try:
                        os.kill(pid, 9)
                    except OSError:
                        pass
                parent.kill()
                parent.wait(timeout=5)
