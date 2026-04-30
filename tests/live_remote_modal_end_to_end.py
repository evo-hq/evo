"""Full end-to-end test: `evo run` an experiment inside a real Modal sandbox.

Provisions a Modal sandbox, runs the actual evo CLI through it (init,
new, run), validates the experiment commits with score read from
`$EVO_RESULT_PATH` inside the container, and tears down.

Skipped unless `EVO_LIVE_TEST_MODAL=1`. Requires:
  - `modal` Python SDK installed (`uv pip install modal`)
  - Modal authenticated (`modal token new`)

Cost: provisions one Modal sandbox for ~30-90 seconds. A few cents of
Modal credit per run.

Run from repo root:
    EVO_LIVE_TEST_MODAL=1 uv run --project plugins/evo \
        python tests/live_remote_modal_end_to_end.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "evo"
PLUGIN_SRC = PLUGIN_ROOT / "src"
sys.path.insert(0, str(PLUGIN_SRC))


def _gate() -> None:
    if os.environ.get("EVO_LIVE_TEST_MODAL") != "1":
        print("SKIPPED (set EVO_LIVE_TEST_MODAL=1 to enable)")
        sys.exit(0)
    try:
        import modal  # noqa: F401
    except ImportError:
        print("SKIPPED (modal SDK not installed)")
        sys.exit(0)


def _evo(args: list[str], cwd: Path, check: bool = True, timeout: int = 600):
    result = subprocess.run(
        ["uv", "run", "--project", str(PLUGIN_ROOT), "evo", *args],
        cwd=cwd, check=False, capture_output=True, text=True, timeout=timeout,
    )
    if check and result.returncode != 0:
        # Surface stdout + stderr in the assertion message so failures
        # in CI/manual runs are diagnosable without rerunning.
        raise RuntimeError(
            f"evo {' '.join(args)} failed (rc={result.returncode}):\n"
            f"STDOUT:\n{result.stdout}\n"
            f"STDERR:\n{result.stderr}"
        )
    return result


def _build_repo(workdir: Path) -> Path:
    """Tiny fixture repo with a benchmark that writes to $EVO_RESULT_PATH."""
    repo = workdir / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "agent.py").write_text("STATE = 'baseline'\n", encoding="utf-8")
    (repo / "eval.py").write_text(
        "import os, json\n"
        "from pathlib import Path\n"
        "result_path = os.environ['EVO_RESULT_PATH']\n"
        "Path(result_path).parent.mkdir(parents=True, exist_ok=True)\n"
        "Path(result_path).write_text(json.dumps({'score': 1.0, 'tasks': {}}))\n"
        "print(json.dumps({'score': 1.0}))\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, check=True)
    return repo


def _new_remote_modal(
    repo: Path,
    *,
    parent: str,
    hypothesis: str,
    provider_config: str,
    timeout: int = 300,
):
    return _evo(
        [
            "new",
            "--parent", parent,
            "-m", hypothesis,
            "--remote", "modal",
            "--provider-config", provider_config,
        ],
        cwd=repo,
        timeout=timeout,
    )


def test_evo_run_against_modal() -> None:
    """End-to-end: provision Modal sandbox, run baseline experiment in it."""
    workdir = Path(tempfile.mkdtemp(prefix="evo-modal-e2e-"))
    repo = _build_repo(workdir)

    try:
        # Init records project facts only. Remote provisioning happens on
        # the per-experiment override passed to `evo new`.
        provider_config = (
            "app_name=evo-live-e2e,"
            "timeout_seconds=300,"
            "health_timeout_seconds=90.0"
        )
        _evo(
            ["init", "--target", "agent.py",
             "--benchmark", "python eval.py",
             "--metric", "max", "--host", "generic"],
            cwd=repo,
        )
        print("--- evo init OK ---")

        # `evo new` drives RemoteSandboxBackend.allocate which:
        #   1. Provisions a Modal sandbox via ModalProvider.provision
        #   2. Polls /v1/health until the in-container sandbox-agent answers
        #   3. Ships the parent commit via git bundle
        #   4. Checks out the experiment's branch in the sandbox
        print("--- evo new exp_0000 (provisions Modal sandbox) ---")
        t0 = time.monotonic()
        out = _new_remote_modal(
            repo,
            parent="root",
            hypothesis="modal e2e baseline",
            provider_config=provider_config,
            timeout=300,
        )
        print(f"    provisioned + allocated in {time.monotonic() - t0:.1f}s")
        print(f"    {out.stdout.strip()}")

        # Step 4: evo run exp_0000. This is the full path:
        #   - render diff inside the sandbox
        #   - run benchmark inside the sandbox (writes EVO_RESULT_PATH)
        #   - read result.json back to local
        #   - compare scores, commit (git ops in sandbox + bundle out)
        print("--- evo run exp_0000 ---")
        run_out = _evo(["run", "exp_0000"], cwd=repo)
        print(f"    stdout: {run_out.stdout.strip()}")
        assert "COMMITTED exp_0000" in run_out.stdout, run_out.stdout

        # Step 5: verify the experiment commit landed in the local repo
        # via git bundle round-trip.
        graph = json.loads(
            (repo / ".evo" / "run_0000" / "graph.json").read_text(encoding="utf-8")
        )
        commit_sha = graph["nodes"]["exp_0000"]["commit"]
        assert commit_sha
        print(f"    committed: {commit_sha[:12]}")
        local_check = subprocess.run(
            ["git", "cat-file", "-e", commit_sha],
            cwd=repo, capture_output=True,
        )
        assert local_check.returncode == 0, (
            f"experiment commit {commit_sha} not landed in local repo via bundle"
        )
        print(f"    commit reachable in local repo OK")

        # Step 6: verify result.json + traces dir came back from the sandbox.
        attempt_001 = repo / ".evo" / "run_0000" / "experiments" / "exp_0000" / "attempts" / "001"
        result_path = attempt_001 / "result.json"
        assert result_path.exists(), f"result.json missing at {result_path}"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert result.get("score") == 1.0, result
        print(f"    result.json fetched OK: score={result['score']}")

    finally:
        # The sandbox was torn down by `evo run`'s commit path
        # (release_lease tears down in POC). Best-effort additional
        # cleanup via `evo reset` -- catches any case where commit
        # didn't release (e.g., test asserted before COMMITTED).
        print("--- backstop cleanup ---")
        _evo(["reset", "--yes"], cwd=repo, check=False)
        shutil.rmtree(workdir, ignore_errors=True)


def test_multi_experiment_tree_modal() -> None:
    """Tree of experiments through Modal, exercising the full lease
    lifecycle:

      root -> exp_0000 (commits, sandbox torn down)
           -> exp_0001 (fresh sandbox provisioned, parent commit shipped
                        from local git -- the local repo got it back via
                        bundle when exp_0000 committed)
              -> exp_0002 (fresh again; parent is exp_0001)

    Validates:
      - POC's tear-down-on-release behavior (one sandbox per experiment)
      - Parent commit flows local <-> sandbox via bundles across experiments
      - remote_state.json correctly removes prior sandbox + provisions new
      - cross-experiment commit reachability in local repo
    """
    workdir = Path(tempfile.mkdtemp(prefix="evo-modal-tree-"))
    repo = _build_repo(workdir)
    # Mutate eval.py so each experiment can produce a distinct score by
    # editing agent.py. Score = number of "GOOD" tokens in agent.py.
    (repo / "eval.py").write_text(
        "import os, json\n"
        "from pathlib import Path\n"
        "agent = Path('agent.py').read_text()\n"
        "score = float(agent.count('GOOD'))\n"
        "result_path = os.environ['EVO_RESULT_PATH']\n"
        "Path(result_path).parent.mkdir(parents=True, exist_ok=True)\n"
        "Path(result_path).write_text(json.dumps({'score': score, 'tasks': {}}))\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "score-by-good-count"], cwd=repo, check=True)

    try:
        provider_config = (
            "app_name=evo-live-tree,"
            "timeout_seconds=300,"
            "health_timeout_seconds=90.0"
        )
        _evo(
            ["init", "--target", "agent.py",
             "--benchmark", "python eval.py",
             "--metric", "max", "--host", "generic"],
            cwd=repo,
        )
        print("--- evo init OK ---")

        commits: list[str] = []
        for depth, (parent, hyp, agent_content, expected_score) in enumerate([
            ("root",     "baseline",   "STATE = ''\n",                       0.0),
            ("exp_0000", "one good",   "STATE = 'GOOD'\n",                   1.0),
            ("exp_0001", "two goods",  "STATE = 'GOOD GOOD'\n",              2.0),
        ]):
            exp_id = f"exp_{depth:04d}"
            print(f"\n--- depth {depth}: parent={parent} -> {exp_id} ---")

            t0 = time.monotonic()
            _new_remote_modal(
                repo,
                parent=parent,
                hypothesis=hyp,
                provider_config=provider_config,
                timeout=300,
            )
            print(f"    new (provision + ship parent commit): {time.monotonic() - t0:.1f}s")

            # Verify a fresh sandbox got provisioned (not reused).
            from evo.backends import remote_state as _rs
            state = _rs.read_state(repo)
            assert len(state["sandboxes"]) == 1, state
            sandbox = state["sandboxes"][0]
            assert sandbox["leased_by"]["exp_id"] == exp_id, sandbox
            print(f"    sandbox native_id: {sandbox['native_id']}")

            # The "subagent" edits agent.py via the production path:
            # `evo write --exp-id <id>`. This is what a real agent would
            # invoke through its Bash tool. Routes through
            # WorkspaceExecutor -> sandbox-agent HTTP -> in-sandbox fs.
            workspace = sandbox["workspace_root"]
            t_edit = time.monotonic()
            _evo(["write", "--exp-id", exp_id,
                  f"{workspace}/agent.py", "--content", agent_content],
                 cwd=repo, timeout=60)
            print(f"    edit via `evo write --exp-id`: {time.monotonic() - t_edit:.1f}s")

            # Verify the edit landed -- read it back via `evo read`.
            verify = _evo(["read", "--exp-id", exp_id,
                           f"{workspace}/agent.py"], cwd=repo, timeout=30)
            assert verify.stdout == agent_content, (
                f"read-back mismatch:\n  wrote:  {agent_content!r}\n  read:   {verify.stdout!r}"
            )

            t0 = time.monotonic()
            run_out = _evo(["run", exp_id], cwd=repo, timeout=300)
            print(f"    run (benchmark + commit + bundle out): {time.monotonic() - t0:.1f}s")
            assert f"COMMITTED {exp_id} {expected_score}" in run_out.stdout, run_out.stdout

            # The commit hash must be reachable in the LOCAL repo afterwards
            # (otherwise the next iteration can't ship it as the parent
            # commit into a fresh sandbox).
            graph = json.loads(
                (repo / ".evo" / "run_0000" / "graph.json").read_text(encoding="utf-8")
            )
            commit_sha = graph["nodes"][exp_id]["commit"]
            local_check = subprocess.run(
                ["git", "cat-file", "-e", commit_sha],
                cwd=repo, capture_output=True,
            )
            assert local_check.returncode == 0, (
                f"{exp_id} commit {commit_sha} not in local repo"
            )
            commits.append(commit_sha)
            print(f"    committed: {commit_sha[:12]} (local-reachable)")

            # After commit, release_lease should have torn down the
            # sandbox. remote_state should show no leased sandbox.
            state_after = _rs.read_state(repo)
            assert all(s.get("leased_by") is None for s in state_after["sandboxes"]), (
                f"sandbox still leased after commit: {state_after}"
            )
            print(f"    sandbox torn down on release_lease")

        print(f"\n--- tree complete: {len(commits)} commits, all local-reachable ---")

        # Verify the chain: each commit's parent should be the previous one.
        for i, sha in enumerate(commits):
            if i == 0:
                continue
            parent_check = subprocess.run(
                ["git", "rev-parse", f"{sha}^"],
                cwd=repo, capture_output=True, text=True,
            )
            assert parent_check.returncode == 0, parent_check.stderr
            assert parent_check.stdout.strip() == commits[i - 1], (
                f"chain broken: {sha[:12]}^ = {parent_check.stdout.strip()[:12]}, "
                f"expected {commits[i-1][:12]}"
            )
        print("--- chain integrity verified ---")

    finally:
        print("--- backstop cleanup ---")
        _evo(["reset", "--yes"], cwd=repo, check=False)
        shutil.rmtree(workdir, ignore_errors=True)


def test_two_live_modal_allocations_same_config() -> None:
    """Two unrun experiments with the same Modal config should both allocate."""
    workdir = Path(tempfile.mkdtemp(prefix="evo-modal-concurrency-"))
    repo = _build_repo(workdir)

    try:
        provider_config = (
            "app_name=evo-live-concurrency,"
            "timeout_seconds=300,"
            "health_timeout_seconds=90.0,"
            "pool_size=2"
        )
        _evo(
            ["init", "--target", "agent.py",
             "--benchmark", "python eval.py",
             "--metric", "max", "--host", "generic"],
            cwd=repo,
        )
        print("--- evo init OK ---")

        t0 = time.monotonic()
        _new_remote_modal(
            repo,
            parent="root",
            hypothesis="modal concurrency A",
            provider_config=provider_config,
            timeout=300,
        )
        print(f"--- exp_0000 allocated in {time.monotonic() - t0:.1f}s ---")

        t0 = time.monotonic()
        _new_remote_modal(
            repo,
            parent="root",
            hypothesis="modal concurrency B",
            provider_config=provider_config,
            timeout=300,
        )
        print(f"--- exp_0001 allocated in {time.monotonic() - t0:.1f}s ---")

        from evo.backends import remote_state as _rs

        state = _rs.read_state(repo)
        assert len(state["sandboxes"]) == 2, state
        leased = sorted(s["leased_by"]["exp_id"] for s in state["sandboxes"])
        assert leased == ["exp_0000", "exp_0001"], leased
        print("--- two live Modal sandboxes allocated under one config ---")
    finally:
        print("--- backstop cleanup ---")
        _evo(["reset", "--yes"], cwd=repo, check=False)
        shutil.rmtree(workdir, ignore_errors=True)


def test_modal_streaming_salvages_partial_artifacts() -> None:
    """Kill a real Modal sandbox mid-run and verify partial logs/traces survive."""
    workdir = Path(tempfile.mkdtemp(prefix="evo-modal-salvage-"))
    repo = workdir / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "agent.py").write_text("STATE = 'baseline'\n", encoding="utf-8")
    (repo / "eval.py").write_text(
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "traces = Path(os.environ['EVO_TRACES_DIR'])\n"
        "traces.mkdir(parents=True, exist_ok=True)\n"
        "for i in range(6):\n"
        "    payload = {'task_id': i, 'score': float(i + 1), 'summary': f'task-{i}'}\n"
        "    (traces / f'task_{i}.json').write_text(json.dumps(payload))\n"
        "    print(f'tick-{i}', flush=True)\n"
        "    print(f'err-{i}', file=sys.stderr, flush=True)\n"
        "    time.sleep(1.0)\n"
        "Path(os.environ['EVO_RESULT_PATH']).write_text(json.dumps({'score': 6.0, 'tasks': {'0': 6.0}}))\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "stream fixture"], cwd=repo, check=True)

    try:
        provider_config = (
            "app_name=evo-live-salvage,"
            "timeout_seconds=300,"
            "health_timeout_seconds=90.0"
        )
        _evo(
            ["init", "--target", "agent.py",
             "--benchmark", "python eval.py",
             "--metric", "max", "--host", "generic"],
            cwd=repo,
        )
        print("--- evo init OK ---")

        _new_remote_modal(
            repo,
            parent="root",
            hypothesis="modal salvage",
            provider_config=provider_config,
            timeout=300,
        )
        print("--- exp_0000 allocated ---")

        from evo.backends import remote_state as _rs
        import modal

        state = _rs.read_state(repo)
        sandbox = next(
            s for s in state["sandboxes"]
            if (s.get("leased_by") or {}).get("exp_id") == "exp_0000"
        )
        native_id = sandbox["native_id"]

        proc = subprocess.Popen(
            ["uv", "run", "--project", str(PLUGIN_ROOT), "evo", "run", "exp_0000"],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        attempt_dir = (
            repo / ".evo" / "run_0000" / "experiments" / "exp_0000" / "attempts" / "001"
        )
        benchmark_log_path = attempt_dir / "benchmark.log"
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if benchmark_log_path.exists():
                break
            if proc.poll() is not None:
                stdout, stderr = proc.communicate(timeout=5)
                raise AssertionError(
                    f"evo run exited before salvage test could terminate the sandbox:\n"
                    f"stdout:\n{stdout}\n\nstderr:\n{stderr}"
                )
            time.sleep(0.25)
        # Give the benchmark enough time to emit multiple trace files and
        # log lines before termination. Killing too early makes the test
        # probe process-startup timing instead of salvage behavior.
        time.sleep(5.0)
        print(f"--- terminating Modal sandbox {native_id} mid-run ---")
        modal.Sandbox.from_id(native_id).terminate()
        stdout, stderr = proc.communicate(timeout=180)
        assert proc.returncode != 0, (stdout, stderr)

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if (attempt_dir / "benchmark.log").exists():
                break
            time.sleep(0.25)
        benchmark_log = (attempt_dir / "benchmark.log").read_text(encoding="utf-8")
        benchmark_err = (attempt_dir / "benchmark_err.log").read_text(encoding="utf-8")
        traces_dir = attempt_dir / "traces"
        while time.monotonic() < deadline:
            if list(traces_dir.glob("task_*.json")):
                break
            time.sleep(0.25)
        trace_files = sorted(traces_dir.glob("task_*.json"))

        assert "tick-0" in benchmark_log, benchmark_log
        assert "err-0" in benchmark_err, benchmark_err
        assert trace_files, list(traces_dir.glob("*"))

        graph = json.loads(
            (repo / ".evo" / "run_0000" / "graph.json").read_text(encoding="utf-8")
        )
        node = graph["nodes"]["exp_0000"]
        assert node["status"] == "failed", node
        assert node.get("score") is not None, node
        assert node["score"] >= 1.0, node
        print(
            f"--- salvage OK: {len(trace_files)} traces, "
            f"score={node['score']}, stdout bytes={len(benchmark_log)} ---"
        )
    finally:
        print("--- backstop cleanup ---")
        _evo(["reset", "--yes"], cwd=repo, check=False)
        shutil.rmtree(workdir, ignore_errors=True)


def test_branched_tree_modal() -> None:
    """Multi-branch realistic experiment tree on Modal.

    Shape (7 experiments, 3 levels):
        root
        +-- exp_0000  score=1    committed
        |   +-- exp_0001  score=3    committed
        |   |   +-- exp_0002  score=2    evaluated (regressed vs parent=3)
        |   |   +-- exp_0003  score=5    committed (best leaf)
        |   |   +-- exp_0004  FORBIDDEN  evaluated (gate failed)
        |   +-- exp_0005  score=4    committed
        +-- exp_0006  score=2    committed (sibling of exp_0000)

    Exercises: multi-branch (root and exp_0001 each have multiple children),
    multi-level (3 levels of commits), score regression handling,
    gate failure handling, parent-commit flow across branches.
    Realistic ratio: benchmark sleeps ~3s so it dominates the per-run
    time vs. provision/commit overhead.
    """
    workdir = Path(tempfile.mkdtemp(prefix="evo-modal-branch-"))
    repo = workdir / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "agent.py").write_text("STATE = '0'\n", encoding="utf-8")
    # eval.py reads STATE as a number = score. Sleeps to give benchmark
    # phase realistic dominance over provisioning/commit overhead.
    (repo / "eval.py").write_text(
        "import os, json, time, re\n"
        "from pathlib import Path\n"
        "agent = Path('agent.py').read_text()\n"
        "match = re.search(r\"STATE\\s*=\\s*'([^']*)'\", agent)\n"
        "raw = match.group(1) if match else '0'\n"
        "try:\n"
        "    score = float(raw.split()[0])\n"
        "except (ValueError, IndexError):\n"
        "    score = 0.0\n"
        "time.sleep(3)\n"
        "result_path = os.environ['EVO_RESULT_PATH']\n"
        "Path(result_path).parent.mkdir(parents=True, exist_ok=True)\n"
        "Path(result_path).write_text(json.dumps({'score': score, 'tasks': {}}))\n",
        encoding="utf-8",
    )
    # gate.py rejects any agent containing FORBIDDEN. Stdin-free, no EVO_*.
    (repo / "gate.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "agent = Path('agent.py').read_text()\n"
        "sys.exit(1 if 'FORBIDDEN' in agent else 0)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture: state-as-score + forbidden-gate"],
                   cwd=repo, check=True)

    try:
        provider_config = (
            "app_name=evo-live-branch,"
            "timeout_seconds=300,"
            "health_timeout_seconds=90.0"
        )
        _evo(
            ["init", "--target", "agent.py",
             "--benchmark", "python eval.py",
             "--gate", "python gate.py",
             "--metric", "max", "--host", "generic"],
            cwd=repo,
        )
        print("--- evo init OK (gate=python gate.py, metric=max) ---")

        from evo.backends import remote_state as _rs
        from evo.sandbox_client import SandboxAgentClient

        # The "agent" driver: we know what edit each experiment should make.
        # Each row: (parent_id, hypothesis, agent_content, expected_outcome)
        # expected_outcome is the substring that must appear in evo run's stdout.
        plan = [
            ("root",     "baseline-low",  "STATE = '1'\n",            "COMMITTED exp_0000 1.0"),
            ("exp_0000", "improve-1to3",  "STATE = '3'\n",            "COMMITTED exp_0001 3.0"),
            ("exp_0001", "regress-3to2",  "STATE = '2'\n",            "EVALUATED exp_0002"),
            ("exp_0001", "improve-3to5",  "STATE = '5'\n",            "COMMITTED exp_0003 5.0"),
            ("exp_0001", "tries-forbidden", "STATE = '10 FORBIDDEN'\n", "EVALUATED exp_0004"),
            ("exp_0000", "sibling-branch", "STATE = '4'\n",           "COMMITTED exp_0005 4.0"),
            ("root",     "alt-baseline",  "STATE = '2'\n",            "COMMITTED exp_0006 2.0"),
        ]

        committed_shas: dict[str, str] = {}
        outcomes: list[tuple[str, str, str]] = []
        wall_t0 = time.monotonic()

        for idx, (parent, hyp, agent_content, expected) in enumerate(plan):
            exp_id = f"exp_{idx:04d}"
            print(f"\n--- {exp_id} parent={parent} hypothesis={hyp!r} ---")

            t0 = time.monotonic()
            _new_remote_modal(
                repo,
                parent=parent,
                hypothesis=hyp,
                provider_config=provider_config,
                timeout=300,
            )
            t_new = time.monotonic() - t0

            # The "subagent" edits agent.py via the production path:
            # `evo write --exp-id <id>`. Each subagent passes its own
            # exp_id; evo CLI looks up which sandbox is leased to that
            # exp_id and routes the write there. This validates the
            # safety property: subagent for exp_NNNN can only touch
            # the sandbox leased to exp_NNNN.
            state = _rs.read_state(repo)
            sandbox = next(s for s in state["sandboxes"]
                           if (s.get("leased_by") or {}).get("exp_id") == exp_id)
            workspace = sandbox["workspace_root"]
            _evo(["write", "--exp-id", exp_id,
                  f"{workspace}/agent.py", "--content", agent_content],
                 cwd=repo, timeout=60)

            t0 = time.monotonic()
            run_out = _evo(["run", exp_id], cwd=repo, timeout=300, check=False)
            t_run = time.monotonic() - t0

            assert expected in run_out.stdout, (
                f"{exp_id} expected {expected!r} in stdout, got:\n"
                f"  STDOUT: {run_out.stdout}\n"
                f"  STDERR: {run_out.stderr}"
            )
            print(f"    new={t_new:.1f}s  run={t_run:.1f}s  -> {run_out.stdout.strip().splitlines()[-1]}")

            # If COMMITTED, verify the commit landed locally.
            graph = json.loads(
                (repo / ".evo" / "run_0000" / "graph.json").read_text(encoding="utf-8")
            )
            node = graph["nodes"][exp_id]
            outcomes.append((exp_id, node["status"], hyp))
            if node["status"] == "committed":
                sha = node["commit"]
                committed_shas[exp_id] = sha
                local_check = subprocess.run(
                    ["git", "cat-file", "-e", sha],
                    cwd=repo, capture_output=True,
                )
                assert local_check.returncode == 0, (
                    f"{exp_id} commit {sha} not in local repo"
                )

            # If EVALUATED (regression / gate failure), the experiment should
            # be discarded so its slot can be reused. POC behavior: discard
            # also tears down the sandbox.
            if node["status"] == "evaluated":
                _evo(["discard", exp_id, "--reason", f"test cleanup: {hyp}"],
                     cwd=repo, check=False)

        wall_total = time.monotonic() - wall_t0
        print(f"\n--- tree complete in {wall_total:.1f}s ({len(plan)} experiments) ---")

        # Print a summary table.
        print("    " + "-" * 70)
        for exp_id, status, hyp in outcomes:
            sha = committed_shas.get(exp_id, "")
            sha_str = sha[:12] if sha else ""
            print(f"    {exp_id}  {status:12} {sha_str:14} {hyp}")
        print("    " + "-" * 70)

        # Validate the chain integrity for the committed nodes:
        # every committed exp_id's parent commit should be reachable.
        graph = json.loads(
            (repo / ".evo" / "run_0000" / "graph.json").read_text(encoding="utf-8")
        )
        for exp_id, sha in committed_shas.items():
            node = graph["nodes"][exp_id]
            parent_id = node["parent"]
            if parent_id == "root":
                continue
            parent_sha = graph["nodes"][parent_id].get("commit")
            assert parent_sha, f"{parent_id} should have a commit if it's a committed parent"
            # Verify the commit's parent in git matches.
            git_parent = subprocess.run(
                ["git", "rev-parse", f"{sha}^"],
                cwd=repo, capture_output=True, text=True,
            )
            if git_parent.returncode == 0:
                assert git_parent.stdout.strip() == parent_sha, (
                    f"{exp_id} commit {sha[:12]}'s git parent "
                    f"{git_parent.stdout.strip()[:12]} != {parent_id} "
                    f"commit {parent_sha[:12]}"
                )
        print("--- chain integrity verified across branches ---")

        # Best leaf check: exp_0003 should have the highest score.
        max_score = max(
            (n["score"] for n in graph["nodes"].values()
             if n.get("status") == "committed" and n.get("score") is not None),
            default=None,
        )
        assert max_score == 5.0, f"expected max committed score 5.0, got {max_score}"
        print("--- best score (5.0 at exp_0003) verified ---")

    finally:
        print("--- backstop cleanup ---")
        _evo(["reset", "--yes"], cwd=repo, check=False)
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> None:
    _gate()
    print("=== Modal single-experiment end-to-end ===")
    test_evo_run_against_modal()
    print()
    print("=== Modal multi-experiment linear tree ===")
    test_multi_experiment_tree_modal()
    print()
    print("=== Modal multi-allocation same config ===")
    test_two_live_modal_allocations_same_config()
    print()
    print("=== Modal mid-run salvage ===")
    test_modal_streaming_salvages_partial_artifacts()
    print()
    print("=== Modal multi-branch experiment tree ===")
    test_branched_tree_modal()
    print("LIVE MODAL E2E OK")


if __name__ == "__main__":
    main()
