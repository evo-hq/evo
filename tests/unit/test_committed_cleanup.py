"""Cleanup failures must not overwrite successful experiment outcomes."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from evo import cli
from evo.backends.worktree import WorktreeBackend
from evo.core import (
    allocate_experiment,
    attempt_dir,
    attempt_outcome_path,
    experiment_result_path,
    init_workspace,
    load_graph,
)


def _init_experiment(root: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.test"],
        cwd=root,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "test"], cwd=root, check=True)
    (root / "agent.py").write_text("STATE = 1\n", encoding="utf-8")
    (root / "eval.py").write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "Path(os.environ['EVO_RESULT_PATH']).write_text("
        "json.dumps({'score': 1.0}))\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    init_workspace(
        root,
        target="agent.py",
        benchmark="python3 eval.py",
        metric="max",
        gate=None,
        host="generic",
        per_exp_timeout=30,
    )
    allocate_experiment(root, "root", "baseline")


def _fail_release(self: WorktreeBackend, ctx: object) -> None:
    raise RuntimeError("synthetic cleanup failure")


def test_run_cleanup_failure_keeps_committed_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _init_experiment(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(WorktreeBackend, "release_lease", _fail_release)

    args = cli.build_parser().parse_args(["run", "exp_0000"])
    assert args.func(args) == 0

    node = load_graph(tmp_path)["nodes"]["exp_0000"]
    result = json.loads(
        experiment_result_path(tmp_path, "exp_0000").read_text(encoding="utf-8")
    )
    outcome = json.loads(
        attempt_outcome_path(tmp_path, "exp_0000", 1).read_text(encoding="utf-8")
    )
    attempt_state = json.loads(
        (attempt_dir(tmp_path, "exp_0000", 1) / "attempt_state.json").read_text(
            encoding="utf-8"
        )
    )

    assert node["status"] == "committed"
    assert result["status"] == "committed"
    assert outcome["outcome"] == "committed"
    assert attempt_state["status"] == "committed"
    assert "workspace cleanup failed" in capsys.readouterr().err


def test_done_cleanup_failure_keeps_committed_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _init_experiment(tmp_path)
    monkeypatch.setattr(WorktreeBackend, "release_lease", _fail_release)
    args = argparse.Namespace(
        exp_id="exp_0000",
        traces=None,
        no_compare=False,
        score=1.0,
    )

    assert cli._record_done_result(tmp_path, args) == 0

    node = load_graph(tmp_path)["nodes"]["exp_0000"]
    result = json.loads(
        experiment_result_path(tmp_path, "exp_0000").read_text(encoding="utf-8")
    )
    assert node["status"] == "committed"
    assert result["status"] == "committed"
    assert "workspace cleanup failed" in capsys.readouterr().err
