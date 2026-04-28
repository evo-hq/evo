"""Lineage forking live tests (EVO_LIVE_TEST_CLAUDE=1).

Real `claude -p` calls. Real cache_read measurement. Real cost (~$0.30-$1 per
full run). Skipped by default; gate via the env flag like tests/e2e.py does
for the other dispatch live tests.

Validates:

1. ``test_session_id_captured_on_dispatch``  -- a dispatched + committed
   experiment ends up with `session_id` and `session_runtime` on its node
   in the graph. Foundation for everything else.

2. ``test_lineage_fork_skips_explorer``  -- when dispatching a child of an
   experiment that already has a session_id, the dispatch result reports
   ``lineage=True`` and no new explorer record is written for that parent.

3. ``test_lineage_cache_reuse``  -- after a lineage fork, the child's
   ``cache_read_input_tokens`` is non-trivial (orders-of-magnitude higher
   than what an empty-cache baseline would show). This is the property
   that makes lineage worth the architecture; if it doesn't hold we have
   the wrong design.

4. ``test_lineage_in_pool_mode``  -- end-to-end pool + lineage. Parent
   commits in slot A, child forks via lineage and lands in slot B.
   Validates that the cross-backend correctness claim survives a real
   fork over a wire boundary.

These mirror the empirical-tests checklist Codex flagged before sign-off.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "evo"


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def _run(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, check=check, capture_output=True, text=True)


def _evo(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(["uv", "run", "--project", str(PLUGIN_ROOT), "evo", *args], cwd=cwd, check=check)


def _parse_dispatch_run_json(stdout: str) -> dict:
    """`evo dispatch run` prints the JSON result to stdout, but git worktree
    creation emits stray progress lines like `HEAD is now at ...` ahead of
    it. Find the first `{` and parse from there."""
    idx = stdout.find("{")
    if idx < 0:
        raise ValueError(f"no JSON in dispatch output:\n{stdout}")
    return json.loads(stdout[idx:])


def _shutdown_dashboard(root: Path) -> None:
    pid_file = root / ".evo" / "dashboard.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 15)
        except (OSError, ValueError):
            pass


def _build_minimal_workspace(root: Path) -> None:
    """Trivial workspace: a 1-line target and a benchmark that always scores 1.0.
    Enough for dispatch's read pass to have something to do."""
    _run(["git", "init", "-q", "-b", "main"], root)
    _run(["git", "config", "user.email", "t@t"], root)
    _run(["git", "config", "user.name", "t"], root)
    (root / "agent").mkdir(exist_ok=True)
    (root / "agent" / "solve.py").write_text(
        'def solve(t):\n    return t["a"] + t["b"]\n', encoding="utf-8"
    )
    (root / "benchmark.py").write_text(_BENCHMARK_SOURCE, encoding="utf-8")
    (root / ".gitignore").write_text("__pycache__/\n.evo/\n", encoding="utf-8")
    _run(["git", "add", "."], root)
    _run(["git", "commit", "-qm", "baseline"], root)
    (root / ".git" / "info" / "exclude").write_text(".evo/\n", encoding="utf-8")
    _evo(
        [
            "init",
            "--target", "agent/solve.py",
            "--benchmark", f"python3 {{worktree}}/benchmark.py --target {{target}}",
            "--metric", "max",
            "--host", "claude-code",
        ],
        cwd=root,
    )


def _build_pool_workspace(root: Path, workdir: Path) -> tuple[Path, Path]:
    """Pool-mode setup: bare remote, main repo, two slot clones. Returns
    (slot1, slot2) -- the main repo is `root`."""
    bare = workdir / "bare.git"
    _run(["git", "init", "--bare", "-q", str(bare)], cwd=workdir)
    _run(["git", "init", "-q", "-b", "main"], cwd=root)
    _run(["git", "remote", "add", "origin", str(bare)], cwd=root)
    _run(["git", "config", "user.email", "t@t"], root)
    _run(["git", "config", "user.name", "t"], root)
    (root / "agent").mkdir(exist_ok=True)
    (root / "agent" / "solve.py").write_text(
        'def solve(t):\n    return t["a"] + t["b"]\n', encoding="utf-8"
    )
    (root / "benchmark.py").write_text(_BENCHMARK_SOURCE, encoding="utf-8")
    (root / ".gitignore").write_text(".evo/\n.build-cache-stamp\n__pycache__/\n", encoding="utf-8")
    _run(["git", "add", "."], root)
    _run(["git", "commit", "-qm", "baseline"], root)
    _run(["git", "push", "-q", "origin", "main"], root)
    (root / ".git" / "info" / "exclude").write_text(".evo/\n", encoding="utf-8")

    slots = []
    for i in range(2):
        slot = workdir / f"ws-{i+1}"
        _run(["git", "clone", "-q", str(bare), str(slot)], cwd=workdir)
        _run(["git", "config", "user.email", "t@t"], slot)
        _run(["git", "config", "user.name", "t"], slot)
        (slot / ".build-cache-stamp").write_text(f"warm-stamp-{i}\n", encoding="utf-8")
        slots.append(slot)

    _evo(
        [
            "init",
            "--target", "agent/solve.py",
            "--benchmark", f"python3 {{worktree}}/benchmark.py --target {{target}}",
            "--metric", "max",
            "--host", "claude-code",
            "--backend", "pool",
            "--workspaces", f"{slots[0]},{slots[1]}",
        ],
        cwd=root,
    )
    return slots[0], slots[1]


_BENCHMARK_SOURCE = """\
import argparse, json, os, importlib.util
from pathlib import Path
p = argparse.ArgumentParser(); p.add_argument('--target', required=True)
spec = importlib.util.spec_from_file_location('t', p.parse_args().target)
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
score = 1.0 if mod.solve({'a': 1, 'b': 2}) == 3 else 0.0
out = json.dumps({'score': score})
rp = os.environ.get('EVO_RESULT_PATH')
if rp:
    Path(rp).parent.mkdir(parents=True, exist_ok=True)
    Path(rp).write_text(out)
else:
    print(out)
"""


def _read_node(root: Path, exp_id: str) -> dict:
    """Read a node from the active run's graph. evo namespaces graphs per
    run under `.evo/run_NNNN/graph.json`; meta.json carries the active id."""
    meta = json.loads((root / ".evo" / "meta.json").read_text(encoding="utf-8"))
    run_id = meta["active"]
    graph = json.loads(
        (root / ".evo" / run_id / "graph.json").read_text(encoding="utf-8")
    )
    return graph["nodes"][exp_id]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_session_id_captured_on_dispatch(workdir: Path) -> None:
    """After `evo dispatch run` (foreground) commits an experiment via the
    dispatched agent, session_id is on the node in the graph."""
    root = workdir / "ws"
    root.mkdir()
    _build_minimal_workspace(root)
    try:
        # Foreground dispatch -- blocks until child commits; session_id is in the result.
        out = _evo(
            ["dispatch", "run", "--parent", "root", "-m",
             "Add a one-line docstring to agent/solve.py describing what the function does. Do not change behavior. Then evaluate this experiment by running `evo run exp_0000` from the main repo root. Report the result and exit.", "--budget", "1"],
            cwd=root,
        )
        result = _parse_dispatch_run_json(out.stdout)
        assert result.get("exit_code") == 0, result
        exp_id = result["exp_id"]

        node = _read_node(root, exp_id)
        assert node.get("status") == "committed", node["status"]
        assert node.get("session_id"), f"session_id missing on {exp_id}: {node}"
        assert node.get("session_runtime") == "claude-code", node.get("session_runtime")
    finally:
        _shutdown_dashboard(root)


def test_lineage_fork_skips_explorer(workdir: Path) -> None:
    """Dispatching a child of an experiment that has a session_id should
    take the lineage path -- result reports lineage=True, no new explorer
    record is written for that parent."""
    root = workdir / "ws"
    root.mkdir()
    _build_minimal_workspace(root)
    try:
        # First dispatch: child of root. Goes through explorer warming.
        _evo(
            ["dispatch", "run", "--parent", "root", "-m",
             "Add a brief comment at the top of agent/solve.py noting the function signature, then evaluate by running `evo run` for your assigned experiment id from the main repo root. Report the result and exit.", "--budget", "1"],
            cwd=root,
        )
        # exp_0000 now has session_id

        # exp_0000's explorer was warmed for parent=root; check it wasn't
        # warmed for parent=exp_0000 (we haven't dispatched that yet).
        explorer_for_exp_0000 = root / ".evo" / "run_0000" / "explorers" / "exp_0000.json"
        assert not explorer_for_exp_0000.exists()

        # Second dispatch: child of exp_0000. Should take lineage path.
        out = _evo(
            ["dispatch", "run", "--parent", "exp_0000", "-m",
             "Add a brief comment at the top of agent/solve.py noting the function signature, then evaluate by running `evo run` for your assigned experiment id from the main repo root. Report the result and exit.", "--budget", "1"],
            cwd=root,
        )
        result = _parse_dispatch_run_json(out.stdout)
        assert result.get("lineage") is True, f"expected lineage path, got: {result}"

        # No explorer record was written for exp_0000.
        assert not explorer_for_exp_0000.exists()
    finally:
        _shutdown_dashboard(root)


def test_lineage_cache_reuse(workdir: Path) -> None:
    """A lineage-forked child's cache_read_input_tokens should be substantial
    (we expect tens of thousands of cached tokens, since the parent's
    transcript is forked in full). If this number is near zero, the lineage
    architecture isn't actually saving cost and we have the wrong design."""
    root = workdir / "ws"
    root.mkdir()
    _build_minimal_workspace(root)
    try:
        _evo(
            ["dispatch", "run", "--parent", "root", "-m",
             "Read agent/solve.py and benchmark.py, add a brief docstring to solve, then run `evo run` for your assigned experiment id from the main repo root.",
             "--budget", "2"],
            cwd=root,
        )
        out = _evo(
            ["dispatch", "run", "--parent", "exp_0000", "-m",
             "Add a brief comment at the top of agent/solve.py noting the function signature, then evaluate by running `evo run` for your assigned experiment id from the main repo root. Report the result and exit.", "--budget", "1"],
            cwd=root,
        )
        result = _parse_dispatch_run_json(out.stdout)
        usage = result.get("usage", {})
        cache_read = usage.get("cache_read_input_tokens", 0)
        # The lineage-forked child resumes the parent's session, so the
        # parent's KV cache prefix is reused. Empirical threshold: at least
        # 5k tokens. A non-lineage cold start would show ~0.
        assert cache_read >= 5000, (
            f"cache_read_input_tokens={cache_read} too low; lineage may not "
            f"be reusing prefix cache as designed. usage={usage}"
        )
    finally:
        _shutdown_dashboard(root)


def test_lineage_in_pool_mode(workdir: Path) -> None:
    """Lineage fork survives the pool-slot reuse boundary: parent commits in
    slot A, child lineage-forks (no explorer) and runs in slot B. The dispatch
    result must report lineage=True even when the workspace has changed."""
    root = workdir / "ws"
    root.mkdir()
    slot1, slot2 = _build_pool_workspace(root, workdir)
    try:
        _evo(
            ["dispatch", "run", "--parent", "root", "-m",
             "Add a brief comment at the top of agent/solve.py noting the function signature, then evaluate by running `evo run` for your assigned experiment id from the main repo root. Report the result and exit.", "--budget", "1"],
            cwd=root,
        )
        # exp_0000 is now committed; its slot's lease is released.
        out = _evo(
            ["dispatch", "run", "--parent", "exp_0000", "-m",
             "Add a brief comment at the top of agent/solve.py noting the function signature, then evaluate by running `evo run` for your assigned experiment id from the main repo root. Report the result and exit.", "--budget", "1"],
            cwd=root,
        )
        result = _parse_dispatch_run_json(out.stdout)
        assert result.get("lineage") is True, result
        # Worktree path is one of the slots, NOT under .evo/.../worktrees.
        node = _read_node(root, result["exp_id"])
        worktree = Path(node["worktree"]).resolve()
        assert worktree in (slot1.resolve(), slot2.resolve()), worktree
    finally:
        _shutdown_dashboard(root)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def main() -> None:
    if os.environ.get("EVO_LIVE_TEST_CLAUDE") != "1":
        print("e2e_lineage live: skipped (set EVO_LIVE_TEST_CLAUDE=1 to enable)")
        return

    if shutil.which(os.environ.get("EVO_CLAUDE_BIN", "claude")) is None:
        print("e2e_lineage live: skipped (claude not on PATH)")
        return

    print("e2e_lineage live: starting (real LLM calls; ~$0.30-$1 cost)")

    workdir = Path(tempfile.mkdtemp(prefix="evo-lineage-test-"))
    try:
        for fn in (
            test_session_id_captured_on_dispatch,
            test_lineage_fork_skips_explorer,
            test_lineage_cache_reuse,
            test_lineage_in_pool_mode,
        ):
            sub = workdir / fn.__name__
            sub.mkdir()
            print(f"--- {fn.__name__} ---")
            fn(sub)
            print(f"    OK")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print("E2E LINEAGE OK")


if __name__ == "__main__":
    main()
