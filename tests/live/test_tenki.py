"""Live test: `evo run` an experiment inside a real Tenki sandbox.

Skipped unless `EVO_LIVE_TEST_TENKI=1`, a Tenki auth token
(`TENKI_API_KEY` or `TENKI_AUTH_TOKEN`), and `TENKI_PROJECT_ID` are set.
Requires the optional `tenki-sandbox` SDK.
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

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "evo"
PLUGIN_SRC = PLUGIN_ROOT / "src"
sys.path.insert(0, str(PLUGIN_SRC))


def _skip_reason() -> str | None:
    if os.environ.get("EVO_LIVE_TEST_TENKI") != "1":
        return "set EVO_LIVE_TEST_TENKI=1 to enable"
    if not (os.environ.get("TENKI_API_KEY") or os.environ.get("TENKI_AUTH_TOKEN")):
        return "set TENKI_API_KEY or TENKI_AUTH_TOKEN to enable"
    if not os.environ.get("TENKI_PROJECT_ID", "").strip():
        return "set TENKI_PROJECT_ID to enable"
    try:
        import tenki_sandbox  # noqa: F401
    except ImportError:
        return "tenki-sandbox SDK not installed"
    return None


def _gate() -> None:
    reason = _skip_reason()
    if reason:
        print(f"SKIPPED ({reason})")
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


def test_evo_run_against_tenki() -> None:
    reason = _skip_reason()
    if reason:
        import pytest
        pytest.skip(reason)
    workdir = Path(tempfile.mkdtemp(prefix="evo-tenki-e2e-"))
    repo = _build_repo(workdir)

    try:
        provider_config = (
            "timeout_seconds=600,"
            "health_timeout_seconds=90.0"
        )
        _evo(
            ["init", "--target", "agent.py",
             "--benchmark", "python3 eval.py",
             "--metric", "max", "--host", "generic", "--per-exp-timeout", "1800"],
            cwd=repo,
        )
        print("--- evo init OK ---")

        t0 = time.monotonic()
        out = _evo(
            ["new", "--parent", "root", "-m", "tenki e2e baseline",
             "--remote", "tenki",
             "--provider-config", provider_config],
            cwd=repo,
            timeout=600,
        )
        print(f"--- evo new exp_0000 (provisions Tenki sandbox): {time.monotonic() - t0:.1f}s ---")
        print(out.stdout.strip())

        t0 = time.monotonic()
        run_out = _evo(["run", "exp_0000"], cwd=repo, timeout=600)
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
        _evo(["reset", "--yes"], cwd=repo)
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> None:
    _gate()
    print("=== Tenki single-experiment end-to-end ===")
    test_evo_run_against_tenki()
    print("LIVE TENKI E2E OK")


if __name__ == "__main__":
    main()
