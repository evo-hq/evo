"""End-to-end tests for execution_backend = pool.

Exercises lease lifecycle, slot validation, pool exhaustion, untracked-file
persistence, branch-keep-on-discard, and cross-slot commit fetch. Uses real
subprocesses against a real bare-remote (no mocks).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "evo"


def _run(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, check=check, capture_output=True, text=True)


def _evo(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(["uv", "run", "--project", str(PLUGIN_ROOT), "evo", *args], cwd=cwd, check=check)


def _shutdown_dashboard(root: Path) -> None:
    pid_file = root / ".evo" / "dashboard.pid"
    if not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 15)
    except (OSError, ValueError):
        pass


def _build_pool_setup(workdir: Path) -> tuple[Path, Path, Path]:
    """Create a bare remote, a main repo cloned from it, and two slot clones.

    Returns (main_repo, slot_1, slot_2). The bare remote and the main repo
    share an `origin` URL; both slots are clones of the bare remote.
    """
    bare = workdir / "bare.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)

    main = workdir / "main"
    subprocess.run(["git", "clone", "-q", str(bare), str(main)], check=True)
    _run(["git", "config", "user.email", "t@t"], main)
    _run(["git", "config", "user.name", "t"], main)
    (main / "agent").mkdir()
    (main / "agent" / "solve.py").write_text(
        'def solve(t):\n    return t["a"] + t["b"]\n', encoding="utf-8"
    )
    (main / "benchmark.py").write_text(_BENCHMARK_SOURCE, encoding="utf-8")
    # Pool mode users MUST gitignore their warm state, otherwise `evo run`'s
    # `git add -A` captures it into the experiment commit and sibling slots
    # see "untracked working tree files would be overwritten by checkout".
    (main / ".gitignore").write_text(".build-cache-stamp\n__pycache__/\n", encoding="utf-8")
    _run(["git", "add", "."], main)
    _run(["git", "commit", "-qm", "baseline"], main)
    _run(["git", "push", "-q", "origin", "main"], main)
    (main / ".git" / "info" / "exclude").write_text(".evo/\n", encoding="utf-8")

    slots = []
    for i in range(2):
        slot = workdir / f"ws-{i+1}"
        subprocess.run(["git", "clone", "-q", str(bare), str(slot)], check=True)
        _run(["git", "config", "user.email", "t@t"], slot)
        _run(["git", "config", "user.name", "t"], slot)
        # Untracked stamp -- the warm state pool mode is supposed to preserve.
        (slot / ".build-cache-stamp").write_text(f"warm-stamp-{i}\n", encoding="utf-8")
        slots.append(slot)

    return main, slots[0], slots[1]


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


def test_init_validates_pool_slots(workdir: Path) -> None:
    """init rejects: --backend pool without --workspaces, --workspaces without
    --backend pool, missing slot path, non-git slot path."""
    main, slot1, slot2 = _build_pool_setup(workdir)

    # --backend pool requires --workspaces
    r = _evo(
        ["init", "--target", "agent/solve.py", "--benchmark", "true",
         "--metric", "max", "--host", "claude-code", "--backend", "pool"],
        cwd=main, check=False,
    )
    assert r.returncode != 0, r.stdout
    assert "requires --workspaces" in r.stderr, r.stderr

    # --workspaces without --backend pool
    r = _evo(
        ["init", "--target", "agent/solve.py", "--benchmark", "true",
         "--metric", "max", "--host", "claude-code",
         "--workspaces", f"{slot1},{slot2}"],
        cwd=main, check=False,
    )
    assert r.returncode != 0, r.stdout
    assert "only valid with --backend pool" in r.stderr, r.stderr

    # Missing slot path
    r = _evo(
        ["init", "--target", "agent/solve.py", "--benchmark", "true",
         "--metric", "max", "--host", "claude-code",
         "--backend", "pool",
         "--workspaces", f"{slot1},/no/such/path"],
        cwd=main, check=False,
    )
    assert r.returncode != 0
    assert "does not exist" in r.stderr, r.stderr

    # Non-git slot
    not_git = workdir / "not-git"
    not_git.mkdir()
    r = _evo(
        ["init", "--target", "agent/solve.py", "--benchmark", "true",
         "--metric", "max", "--host", "claude-code",
         "--backend", "pool",
         "--workspaces", f"{slot1},{not_git}"],
        cwd=main, check=False,
    )
    assert r.returncode != 0
    assert "not a git working tree" in r.stderr, r.stderr


def test_pool_lease_release_and_exhaustion(workdir: Path) -> None:
    """Two slots, three `evo new` calls: third hits PoolExhausted. After
    `evo run` commits exp_0000, the slot returns to the free queue."""
    main, slot1, slot2 = _build_pool_setup(workdir)
    _evo(
        ["init", "--target", "agent/solve.py",
         "--benchmark", f"python3 {{worktree}}/benchmark.py --target {{target}}",
         "--metric", "max", "--host", "claude-code",
         "--backend", "pool",
         "--workspaces", f"{slot1},{slot2}"],
        cwd=main,
    )
    try:
        _evo(["new", "--parent", "root", "-m", "first"], cwd=main)
        _evo(["new", "--parent", "root", "-m", "second"], cwd=main)
        r = _evo(["new", "--parent", "root", "-m", "third"], cwd=main, check=False)
        assert r.returncode != 0, r.stdout
        assert "pool exhausted" in r.stderr, r.stderr

        # Run exp_0000 to commit and free its slot.
        out = _evo(["run", "exp_0000"], cwd=main).stdout
        assert "COMMITTED exp_0000" in out, out

        status = _evo(["workspace", "status", "--json"], cwd=main).stdout
        slots = json.loads(status)["slots"]
        free_count = sum(1 for s in slots if s["leased_by"] is None)
        assert free_count == 1, slots

        # Fourth `evo new` should now succeed (lands on the freed slot).
        r = _evo(["new", "--parent", "exp_0000", "-m", "fourth"], cwd=main)
        assert r.returncode == 0, r.stdout
    finally:
        _shutdown_dashboard(main)


def test_untracked_files_persist_across_experiments(workdir: Path) -> None:
    """Untracked files in slots survive across pool leases. The agent's edits
    on a failed experiment should NOT be lost on retry."""
    main, slot1, slot2 = _build_pool_setup(workdir)
    _evo(
        ["init", "--target", "agent/solve.py",
         "--benchmark", f"python3 {{worktree}}/benchmark.py --target {{target}}",
         "--metric", "max", "--host", "claude-code",
         "--backend", "pool",
         "--workspaces", f"{slot1},{slot2}"],
        cwd=main,
    )
    try:
        # Run two experiments to completion; verify both stamps survive in
        # both slots (slot reuse doesn't blow away untracked files).
        _evo(["new", "--parent", "root", "-m", "first"], cwd=main)
        _evo(["run", "exp_0000"], cwd=main)
        _evo(["new", "--parent", "root", "-m", "second"], cwd=main)
        _evo(["run", "exp_0001"], cwd=main)
        # Use exp_0000 as parent to force re-lease of slot 0 (which had it).
        _evo(["new", "--parent", "exp_0000", "-m", "third"], cwd=main)
        _evo(["run", "exp_0002"], cwd=main)

        for slot in (slot1, slot2):
            stamp = slot / ".build-cache-stamp"
            assert stamp.exists(), f"stamp missing in {slot}"
            content = stamp.read_text(encoding="utf-8")
            assert "warm-stamp" in content, content
    finally:
        _shutdown_dashboard(main)


def test_discard_releases_lease_keeps_branch(workdir: Path) -> None:
    """`evo discard` releases the slot and (default) keeps the experiment's
    branch in the slot for inspection. Slot directory untouched."""
    main, slot1, slot2 = _build_pool_setup(workdir)
    _evo(
        ["init", "--target", "agent/solve.py",
         "--benchmark", f"python3 {{worktree}}/benchmark.py --target {{target}}",
         "--metric", "max", "--host", "claude-code",
         "--backend", "pool",
         "--workspaces", f"{slot1},{slot2}"],
        cwd=main,
    )
    try:
        _evo(["new", "--parent", "root", "-m", "to be discarded"], cwd=main)
        # Snapshot which slot got leased.
        status_before = json.loads(
            _evo(["workspace", "status", "--json"], cwd=main).stdout
        )
        leased_path = next(
            Path(s["path"]) for s in status_before["slots"]
            if s["leased_by"] is not None
        )

        _evo(["discard", "exp_0000", "--reason", "test"], cwd=main)

        # Slot now idle.
        status_after = json.loads(
            _evo(["workspace", "status", "--json"], cwd=main).stdout
        )
        free = sum(1 for s in status_after["slots"] if s["leased_by"] is None)
        assert free == 2, status_after

        # Slot directory still on disk.
        assert leased_path.exists(), f"{leased_path} was deleted"

        # Branch was kept in the slot (default policy).
        branches = _run(
            ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/evo/"],
            cwd=leased_path,
        ).stdout
        assert "evo/run_0000/exp_0000" in branches, branches
    finally:
        _shutdown_dashboard(main)


def test_main_repo_rejected_as_slot(workdir: Path) -> None:
    """init refuses if a slot path resolves to the main repo. Otherwise the
    next `evo new` would `git checkout -B evo/...` against the user's working
    branch -- silent data loss."""
    main, slot1, _slot2 = _build_pool_setup(workdir)
    r = _evo(
        ["init", "--target", "agent/solve.py", "--benchmark", "true",
         "--metric", "max", "--host", "claude-code",
         "--backend", "pool",
         "--workspaces", f"{slot1},{main}"],
        cwd=main, check=False,
    )
    assert r.returncode != 0, r.stdout
    assert "main repo" in r.stderr, r.stderr


def test_duplicate_and_aliased_slots_rejected(workdir: Path) -> None:
    """Same path twice and symlink aliases both rejected at init."""
    main, slot1, slot2 = _build_pool_setup(workdir)
    # Same path twice
    r = _evo(
        ["init", "--target", "agent/solve.py", "--benchmark", "true",
         "--metric", "max", "--host", "claude-code",
         "--backend", "pool",
         "--workspaces", f"{slot1},{slot1}"],
        cwd=main, check=False,
    )
    assert r.returncode != 0
    assert "duplicate" in r.stderr.lower(), r.stderr

    # Symlink alias of slot1
    alias = workdir / "ws-1-alias"
    alias.symlink_to(slot1)
    r = _evo(
        ["init", "--target", "agent/solve.py", "--benchmark", "true",
         "--metric", "max", "--host", "claude-code",
         "--backend", "pool",
         "--workspaces", f"{slot1},{alias}"],
        cwd=main, check=False,
    )
    assert r.returncode != 0
    assert "duplicate" in r.stderr.lower(), r.stderr

    # Nested: nested clone inside slot1, but cloned from the same bare so
    # origin matches and we can hit the nesting check.
    bare = workdir / "bare.git"
    nested = slot1 / "nested-clone"
    subprocess.run(["git", "clone", "-q", str(bare), str(nested)], check=True)
    r = _evo(
        ["init", "--target", "agent/solve.py", "--benchmark", "true",
         "--metric", "max", "--host", "claude-code",
         "--backend", "pool",
         "--workspaces", f"{slot1},{nested}"],
        cwd=main, check=False,
    )
    assert r.returncode != 0
    assert "nested" in r.stderr.lower() or "overlap" in r.stderr.lower() or "contains" in r.stderr.lower(), r.stderr


def test_dispatch_accepted_in_pool_mode_config(workdir: Path) -> None:
    """`evo dispatch` no longer refuses pool mode at the config layer.
    Lineage forking sidesteps the worktree-staleness issue. This test
    verifies the surface only (init succeeds; dispatch CLI parses) without
    spawning a real LLM. End-to-end Lineage is tested under
    EVO_LIVE_TEST_CLAUDE=1."""
    main, slot1, slot2 = _build_pool_setup(workdir)
    _evo(
        ["init", "--target", "agent/solve.py",
         "--benchmark", f"python3 {{worktree}}/benchmark.py --target {{target}}",
         "--metric", "max", "--host", "claude-code",
         "--backend", "pool",
         "--workspaces", f"{slot1},{slot2}"],
        cwd=main,
    )
    try:
        # `evo dispatch list` is host-validating but doesn't spawn Claude.
        # In pool mode it should succeed (no PoolMode rejection).
        r = _evo(["dispatch", "list"], cwd=main, check=False)
        # Either succeeds with empty list, or returns "no jobs" -- both indicate
        # the dispatch command was accepted (not rejected at config check).
        assert r.returncode == 0, (r.stdout, r.stderr)
    finally:
        _shutdown_dashboard(main)


def test_orphaned_lease_reconciled_on_next_allocate(workdir: Path) -> None:
    """If a process dies between `_mark_committed` and `release_lease`,
    the next `evo new` should reconcile: see the lease points at a
    `committed` node in the graph and clear it under the lock."""
    main, slot1, slot2 = _build_pool_setup(workdir)
    _evo(
        ["init", "--target", "agent/solve.py",
         "--benchmark", f"python3 {{worktree}}/benchmark.py --target {{target}}",
         "--metric", "max", "--host", "claude-code",
         "--backend", "pool",
         "--workspaces", f"{slot1},{slot2}"],
        cwd=main,
    )
    try:
        # Normal commit path -- exp_0000 committed, slot released cleanly.
        _evo(["new", "--parent", "root", "-m", "first"], cwd=main)
        _evo(["run", "exp_0000"], cwd=main)

        # Simulate a crash window: lease another experiment, then manually
        # forge pool_state to mark the slot as still leased to a (now-)
        # committed exp_0000. The next allocate should clear it.
        _evo(["new", "--parent", "root", "-m", "second"], cwd=main)
        _evo(["run", "exp_0001"], cwd=main)

        # By now both slots are free. Hand-edit pool_state to forge a stale
        # lease pointing at the committed exp_0000.
        state_path = main / ".evo" / "run_0000" / "pool_state.json"
        state = json.loads(state_path.read_text())
        state["slots"][0]["leased_by"] = {
            "exp_id": "exp_0000", "pid": 99999,
            "leased_at": "2026-01-01T00:00:00+00:00",
        }
        state_path.write_text(json.dumps(state, indent=2))

        # Allocate should reconcile the orphaned lease and succeed.
        r = _evo(["new", "--parent", "exp_0000", "-m", "after-crash"], cwd=main)
        assert r.returncode == 0, r.stdout

        state_after = json.loads(state_path.read_text())
        # The reconciled slot is now free OR leased by the new experiment;
        # both are correct -- the assertion is that the orphaned lease is gone.
        for slot in state_after["slots"]:
            lease = slot.get("leased_by")
            if lease is not None:
                assert lease["exp_id"] != "exp_0000", lease
    finally:
        _shutdown_dashboard(main)


def test_cross_slot_commit_fetch(workdir: Path) -> None:
    """Branching off a committed experiment forces a different slot to fetch
    the parent_commit from a sibling slot."""
    main, slot1, slot2 = _build_pool_setup(workdir)
    _evo(
        ["init", "--target", "agent/solve.py",
         "--benchmark", f"python3 {{worktree}}/benchmark.py --target {{target}}",
         "--metric", "max", "--host", "claude-code",
         "--backend", "pool",
         "--workspaces", f"{slot1},{slot2}"],
        cwd=main,
    )
    try:
        # exp_0000 -> slot 0 (or 1; whichever is first free)
        _evo(["new", "--parent", "root", "-m", "parent"], cwd=main)
        _evo(["run", "exp_0000"], cwd=main)
        # Force a different slot for the next experiment by leasing the
        # original slot first with another concurrent experiment.
        _evo(["new", "--parent", "root", "-m", "block original slot"], cwd=main)
        # exp_0002 must land on the OTHER slot and fetch exp_0000's commit.
        out = _evo(["new", "--parent", "exp_0000", "-m", "branch-off"], cwd=main).stdout
        assert "exp_0002" in out, out
        # Verify it actually ran.
        out_run = _evo(["run", "exp_0002"], cwd=main).stdout
        assert "COMMITTED exp_0002" in out_run, out_run
    finally:
        _shutdown_dashboard(main)


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="evo-pool-test-"))
    try:
        for fn in (
            test_init_validates_pool_slots,
            test_main_repo_rejected_as_slot,
            test_duplicate_and_aliased_slots_rejected,
            test_pool_lease_release_and_exhaustion,
            test_untracked_files_persist_across_experiments,
            test_discard_releases_lease_keeps_branch,
            test_cross_slot_commit_fetch,
            test_dispatch_accepted_in_pool_mode_config,
            test_orphaned_lease_reconciled_on_next_allocate,
        ):
            sub = workdir / fn.__name__
            sub.mkdir()
            print(f"--- {fn.__name__} ---")
            fn(sub)
            print(f"    OK")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print("E2E POOL OK")


if __name__ == "__main__":
    main()
