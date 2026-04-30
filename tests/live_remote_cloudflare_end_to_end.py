"""Live test: `evo run` an experiment inside a real Cloudflare sandbox.

Skipped unless BOTH `EVO_LIVE_TEST_CLOUDFLARE=1` and the bridge
credentials are set.
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

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "evo"
PLUGIN_SRC = PLUGIN_ROOT / "src"
sys.path.insert(0, str(PLUGIN_SRC))


def _gate() -> None:
    if os.environ.get("EVO_LIVE_TEST_CLOUDFLARE") != "1":
        print("SKIPPED (set EVO_LIVE_TEST_CLOUDFLARE=1 to enable)")
        sys.exit(0)
    if not os.environ.get("SANDBOX_API_URL"):
        print("SKIPPED (set SANDBOX_API_URL to enable)")
        sys.exit(0)
    if not os.environ.get("SANDBOX_API_KEY"):
        print("SKIPPED (set SANDBOX_API_KEY to enable)")
        sys.exit(0)


def _evo(args: list[str], cwd: Path, check: bool = True, timeout: int = 600):
    result = subprocess.run(
        ["uv", "run", "--project", str(PLUGIN_ROOT), "evo", *args],
        cwd=cwd, check=False, capture_output=True, text=True, timeout=timeout,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"evo {' '.join(args)} failed (rc={result.returncode}):\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result


def _remote_provider_config() -> str:
    return (
        f"api_url={os.environ['SANDBOX_API_URL']},"
        f"api_key={os.environ['SANDBOX_API_KEY']},"
        "timeout_seconds=300,"
        "health_timeout_seconds=90.0"
    )


def _build_repo(workdir: Path) -> Path:
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


def test_evo_run_against_cloudflare() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="evo-cloudflare-e2e-"))
    repo = _build_repo(workdir)

    try:
        _evo(
            ["init", "--target", "agent.py",
             "--benchmark", "python eval.py",
             "--metric", "max", "--host", "generic"],
            cwd=repo,
        )
        print("--- evo init OK ---")

        t0 = time.monotonic()
        out = _evo(
            ["new", "--parent", "root", "-m", "cloudflare e2e baseline",
             "--remote", "cloudflare",
             "--provider-config", _remote_provider_config()],
            cwd=repo,
            timeout=300,
        )
        print(f"--- evo new exp_0000 (provisions Cloudflare sandbox): {time.monotonic() - t0:.1f}s ---")
        print(out.stdout.strip())

        t0 = time.monotonic()
        run_out = _evo(["run", "exp_0000"], cwd=repo, timeout=300)
        print(f"--- evo run exp_0000: {time.monotonic() - t0:.1f}s ---")
        print(run_out.stdout.strip())
        assert "COMMITTED exp_0000 1.0" in run_out.stdout, run_out.stdout

        graph = json.loads(
            (repo / ".evo" / "run_0000" / "graph.json").read_text(encoding="utf-8")
        )
        commit_sha = graph["nodes"]["exp_0000"]["commit"]
        assert commit_sha, graph["nodes"]["exp_0000"]
        local_check = subprocess.run(
            ["git", "cat-file", "-e", commit_sha],
            cwd=repo,
            capture_output=True,
        )
        assert local_check.returncode == 0, commit_sha

        attempt_001 = (
            repo / ".evo" / "run_0000" / "experiments" / "exp_0000" / "attempts" / "001"
        )
        result = json.loads((attempt_001 / "result.json").read_text(encoding="utf-8"))
        assert result.get("score") == 1.0, result
        print("--- result.json fetched OK ---")
    finally:
        print("--- backstop cleanup ---")
        _evo(["reset", "--yes"], cwd=repo, check=False)
        shutil.rmtree(workdir, ignore_errors=True)


def test_multi_experiment_tree_cloudflare() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="evo-cloudflare-tree-"))
    repo = _build_repo(workdir)
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
            _evo(
                ["new", "--parent", parent, "-m", hyp,
                 "--remote", "cloudflare",
                 "--provider-config", _remote_provider_config()],
                cwd=repo,
                timeout=300,
            )
            print(f"    new (provision + ship parent commit): {time.monotonic() - t0:.1f}s")

            from evo.backends import remote_state as _rs
            state = _rs.read_state(repo)
            assert len(state["sandboxes"]) == 1, state
            sandbox = state["sandboxes"][0]
            assert sandbox["leased_by"]["exp_id"] == exp_id, sandbox
            print(f"    sandbox native_id: {sandbox['native_id']}")

            workspace = sandbox["workspace_root"]
            _evo(
                ["write", "--exp-id", exp_id, f"{workspace}/agent.py", "--content", agent_content],
                cwd=repo, timeout=60,
            )
            verify = _evo(["read", "--exp-id", exp_id, f"{workspace}/agent.py"], cwd=repo, timeout=30)
            assert verify.stdout == agent_content, verify.stdout

            t0 = time.monotonic()
            run_out = _evo(["run", exp_id], cwd=repo, timeout=300)
            print(f"    run (benchmark + commit + bundle out): {time.monotonic() - t0:.1f}s")
            assert f"COMMITTED {exp_id} {expected_score}" in run_out.stdout, run_out.stdout

            graph = json.loads(
                (repo / ".evo" / "run_0000" / "graph.json").read_text(encoding="utf-8")
            )
            commit_sha = graph["nodes"][exp_id]["commit"]
            local_check = subprocess.run(
                ["git", "cat-file", "-e", commit_sha],
                cwd=repo, capture_output=True,
            )
            assert local_check.returncode == 0, commit_sha
            commits.append(commit_sha)

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


def test_two_live_cloudflare_allocations_same_config() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="evo-cloudflare-concurrency-"))
    repo = _build_repo(workdir)

    try:
        _evo(
            ["init", "--target", "agent.py",
             "--benchmark", "python eval.py",
             "--metric", "max", "--host", "generic"],
            cwd=repo,
        )
        print("--- evo init OK ---")

        provider_config = _remote_provider_config() + ",pool_size=2"
        _evo(
            ["new", "--parent", "root", "-m", "cloudflare concurrency A",
             "--remote", "cloudflare",
             "--provider-config", provider_config],
            cwd=repo, timeout=300,
        )
        _evo(
            ["new", "--parent", "root", "-m", "cloudflare concurrency B",
             "--remote", "cloudflare",
             "--provider-config", provider_config],
            cwd=repo, timeout=300,
        )
        from evo.backends import remote_state as _rs

        state = _rs.read_state(repo)
        assert len(state["sandboxes"]) == 2, state
        leased = sorted(s["leased_by"]["exp_id"] for s in state["sandboxes"])
        assert leased == ["exp_0000", "exp_0001"], leased
        print("--- two live Cloudflare sandboxes allocated under one config ---")
    finally:
        print("--- backstop cleanup ---")
        _evo(["reset", "--yes"], cwd=repo, check=False)
        shutil.rmtree(workdir, ignore_errors=True)


def test_cloudflare_streaming_salvages_partial_artifacts() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="evo-cloudflare-salvage-"))
    repo = _build_repo(workdir)
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
        _evo(
            ["init", "--target", "agent.py",
             "--benchmark", "python eval.py",
             "--metric", "max", "--host", "generic"],
            cwd=repo,
        )
        print("--- evo init OK ---")

        _evo(
            ["new", "--parent", "root", "-m", "cloudflare salvage",
             "--remote", "cloudflare",
             "--provider-config", _remote_provider_config()],
            cwd=repo,
            timeout=300,
        )
        print("--- exp_0000 allocated ---")

        from evo.backends import remote_state as _rs
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
        time.sleep(5.0)
        print(f"--- terminating Cloudflare sandbox {native_id} mid-run ---")
        resp = requests.delete(
            f"{os.environ['SANDBOX_API_URL'].rstrip('/')}/v1/sandbox/{native_id}",
            headers={"Authorization": f"Bearer {os.environ['SANDBOX_API_KEY']}"},
            timeout=30.0,
        )
        assert resp.status_code in (204, 200), resp.text
        stdout, stderr = proc.communicate(timeout=180)
        assert proc.returncode != 0, (stdout, stderr)

        attempt_dir = (
            repo / ".evo" / "run_0000" / "experiments" / "exp_0000" / "attempts" / "001"
        )
        benchmark_log = (attempt_dir / "benchmark.log").read_text(encoding="utf-8")
        benchmark_err = (attempt_dir / "benchmark_err.log").read_text(encoding="utf-8")
        traces_dir = attempt_dir / "traces"
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
def main() -> None:
    _gate()
    print("=== Cloudflare single-experiment end-to-end ===")
    test_evo_run_against_cloudflare()
    print()
    print("=== Cloudflare multi-experiment tree ===")
    test_multi_experiment_tree_cloudflare()
    print()
    print("=== Cloudflare multi-allocation same config ===")
    test_two_live_cloudflare_allocations_same_config()
    print()
    print("=== Cloudflare mid-run salvage ===")
    test_cloudflare_streaming_salvages_partial_artifacts()
    print("LIVE CLOUDFLARE E2E OK")


if __name__ == "__main__":
    main()
