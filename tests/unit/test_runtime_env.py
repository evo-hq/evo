from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from evo.core import parse_dotenv, resolve_runtime_env, runtime_env_summary

# POSIX-only: the dashboard cleanup fixture sends SIGTERM/SIGKILL (line ~46
# below). Windows doesn't ship signal.SIGKILL, and the kill-wait dance via
# os.kill(pid, 0) has different semantics. The subprocess CLI tests below
# also depend on signal-based teardown to free the dashboard's port between
# tests, which doesn't work cleanly on Windows.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: relies on signal.SIGKILL + os.kill semantics for "
    "dashboard cleanup between subprocess CLI tests",
)


REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "evo"


def run(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, check=check, capture_output=True, text=True)


def evo(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["uv", "run", "--project", str(PLUGIN_ROOT), "evo", *args], cwd=cwd, check=check)


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def init_repo(root: Path) -> None:
    run(["git", "init", "-b", "main"], cwd=root)
    run(["git", "config", "user.name", "evo"], cwd=root)
    run(["git", "config", "user.email", "evo@example.com"], cwd=root)


def shutdown_dashboard(root: Path) -> None:
    pid_file = root / ".evo" / "dashboard.pid"
    if not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    # Wait briefly for the dashboard to exit and release its port. Without
    # this, the next test in the same suite can race on bind() before the
    # OS has freed the socket.
    import time
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    # Still alive after 1s; force-kill.
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def test_parse_dotenv_simple_subset() -> None:
    parsed = parse_dotenv(
        """
        # comment
        A=one
        export B="two words"
        C='literal # value'
        D=plain # inline comment
        BAD-NAME=ignored
        """
    )
    assert parsed == {
        "A": "one",
        "B": "two words",
        "C": "literal # value",
        "D": "plain",
    }


def test_resolve_runtime_env_overlays_dotenv_over_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKEN", "shell")
    write(tmp_path / ".env", "TOKEN=dotenv\nOTHER=visible\n")
    config = {
        "runtime_env": {
            "inherit_shell": True,
            "dotenv": [{"path": ".env", "mode": "allow", "keys": ["TOKEN"]}],
        }
    }
    resolved = resolve_runtime_env(tmp_path, config)
    assert resolved["TOKEN"] == "dotenv"
    assert "OTHER" not in resolved


def test_resolve_runtime_env_remote_skips_shell_inheritance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOST_ONLY_VAR", "macos-value")
    write(tmp_path / ".env", "TOKEN=dotenv\n")
    config = {
        "runtime_env": {
            "inherit_shell": True,
            "dotenv": [{"path": ".env", "mode": "all"}],
        }
    }
    resolved = resolve_runtime_env(tmp_path, config, remote=True)
    assert "HOST_ONLY_VAR" not in resolved
    assert "HOME" not in resolved
    assert resolved["TOKEN"] == "dotenv"

    local = resolve_runtime_env(tmp_path, config, remote=False)
    assert local["HOST_ONLY_VAR"] == "macos-value"


def test_resolve_runtime_env_always_inherits_for_remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOST_ONLY_VAR", "macos-value")
    config = {"runtime_env": {"inherit_shell": "always", "dotenv": []}}
    resolved = resolve_runtime_env(tmp_path, config, remote=True)
    assert resolved["HOST_ONLY_VAR"] == "macos-value"


def test_resolve_runtime_env_off_stays_off_everywhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOST_ONLY_VAR", "macos-value")
    config = {"runtime_env": {"inherit_shell": False, "dotenv": []}}
    assert "HOST_ONLY_VAR" not in resolve_runtime_env(tmp_path, config, remote=False)
    assert "HOST_ONLY_VAR" not in resolve_runtime_env(tmp_path, config, remote=True)


def test_resolve_runtime_env_overlays_dashboard_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKEN", "shell")
    write(tmp_path / ".evo" / "run_0000" / "runtime_env_values.json", '{"variables":{"TOKEN":"dashboard"}}')
    config = {"runtime_env": {"inherit_shell": True, "dotenv": []}}
    from evo.core import _save_meta
    _save_meta(tmp_path, {"active": "run_0000", "next_run": 1})

    resolved = resolve_runtime_env(tmp_path, config)
    assert resolved["TOKEN"] == "dashboard"


def test_runtime_env_summary_redacts_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKEN", "shell-secret")
    write(tmp_path / ".env", "TOKEN=dotenv-secret\n")
    config = {
        "runtime_env": {
            "inherit_shell": False,
            "dotenv": [{"path": ".env", "mode": "all"}],
        }
    }
    summary = runtime_env_summary(tmp_path, config)
    encoded = json.dumps(summary)
    assert "TOKEN" in encoded
    assert "dotenv-secret" not in encoded
    assert "shell-secret" not in encoded
    assert summary["configured_key_previews"] == {"TOKEN": "do...et"}
    assert summary["dotenv"][0]["key_previews"] == {"TOKEN": "do...et"}


def test_missing_runtime_env_file_fails_clearly(tmp_path: Path) -> None:
    config = {"runtime_env": {"inherit_shell": False, "dotenv": [{"path": ".env", "mode": "all"}]}}
    with pytest.raises(RuntimeError, match="runtime dotenv file not found"):
        resolve_runtime_env(tmp_path, config)


def test_cli_env_load_and_run_forward_runtime_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    init_repo(tmp_path)
    write(tmp_path / "agent.py", 'STATE = "baseline"\n')
    write(
        tmp_path / "eval.py",
        """from __future__ import annotations
import json
import os
from pathlib import Path

assert Path(os.environ["EVO_RESULT_PATH"]).is_absolute()
assert Path(os.environ["EVO_TRACES_DIR"]).is_absolute()
score = 1.0 if os.environ.get("TOKEN") == "from-file" else 0.0
Path(os.environ["EVO_RESULT_PATH"]).write_text(json.dumps({"score": score}), encoding="utf-8")
""",
    )
    write(
        tmp_path / "gate.py",
        """from __future__ import annotations
import os
import sys

if os.environ.get("TOKEN") != "from-file":
    sys.exit(2)
if any(key.startswith("EVO_") for key in os.environ):
    sys.exit(3)
""",
    )
    run(["git", "add", "."], cwd=tmp_path)
    run(["git", "commit", "-m", "fixture: runtime env"], cwd=tmp_path)

    monkeypatch.setenv("TOKEN", "shell")
    try:
        evo(
            [
                "init",
                "--target", "agent.py",
                "--benchmark", "python3 eval.py",
                "--gate", "python3 gate.py",
                "--metric", "max",
                "--host", "generic", "--per-exp-timeout", "1800",
            ],
            cwd=tmp_path,
        )
        write(tmp_path / ".env", "TOKEN=from-file\n")
        evo(["env", "load", ".env", "--all"], cwd=tmp_path)
        config_text = (tmp_path / ".evo" / "run_0000" / "config.json").read_text(encoding="utf-8")
        assert "from-file" not in config_text

        shown = evo(["env", "show", "--json"], cwd=tmp_path).stdout
        assert "TOKEN" in shown
        assert "from-file" not in shown

        evo(["new", "--parent", "root", "-m", "baseline"], cwd=tmp_path)
        result = evo(["run", "exp_0000"], cwd=tmp_path)
        assert "COMMITTED exp_0000 1.0" in result.stdout
    finally:
        shutdown_dashboard(tmp_path)


def test_evo_run_exposes_checkpoint_dir_and_records_attempt_state(tmp_path: Path) -> None:
    init_repo(tmp_path)
    write(tmp_path / "agent.py", 'STATE = "baseline"\n')
    write(
        tmp_path / "eval.py",
        """from __future__ import annotations
import json
import os
from pathlib import Path

checkpoint_dir = Path(os.environ["EVO_CHECKPOINT_DIR"])
checkpoint_dir.mkdir(parents=True, exist_ok=True)
(checkpoint_dir / "progress.json").write_text(json.dumps({"step": 1}), encoding="utf-8")
Path(os.environ["EVO_RESULT_PATH"]).write_text(json.dumps({"score": 1.0}), encoding="utf-8")
""",
    )
    run(["git", "add", "."], cwd=tmp_path)
    run(["git", "commit", "-m", "fixture: checkpoint"], cwd=tmp_path)

    try:
        evo(
            [
                "init",
                "--target", "agent.py",
                "--benchmark", "python3 eval.py",
                "--metric", "max",
                "--host", "generic", "--per-exp-timeout", "1800",
            ],
            cwd=tmp_path,
        )
        evo(["new", "--parent", "root", "-m", "checkpoint contract"], cwd=tmp_path)
        result = evo(["run", "exp_0000"], cwd=tmp_path)
        assert "COMMITTED exp_0000 1.0" in result.stdout

        attempt = tmp_path / ".evo" / "run_0000" / "experiments" / "exp_0000" / "attempts" / "001"
        assert (attempt / "checkpoints" / "progress.json").exists()
        state = json.loads((attempt / "attempt_state.json").read_text(encoding="utf-8"))
        assert state["phase"] == "complete"
        assert state["status"] == "committed"
        assert state["checkpoint_dir"].endswith("attempts/001/checkpoints")
        outcome = json.loads((attempt / "outcome.json").read_text(encoding="utf-8"))
        assert outcome["attempt_state"]["status"] == "committed"
    finally:
        shutdown_dashboard(tmp_path)


def test_cli_config_show_and_set_basic_fields(tmp_path: Path) -> None:
    init_repo(tmp_path)
    write(tmp_path / "agent.py", 'STATE = "baseline"\n')
    write(tmp_path / "eval.py", 'print({"score": 0.0})\n')
    run(["git", "add", "."], cwd=tmp_path)
    run(["git", "commit", "-m", "fixture: config"], cwd=tmp_path)

    try:
        evo(
            [
                "init",
                "--target", "agent.py",
                "--benchmark", "python3 eval.py",
                "--metric", "max",
                "--host", "generic", "--per-exp-timeout", "1800",
            ],
            cwd=tmp_path,
        )
        evo(["config", "set", "benchmark", "python3 eval.py --updated"], cwd=tmp_path)
        evo(["config", "set", "metric", "min"], cwd=tmp_path)
        evo(["config", "set", "commit-strategy", "tracked-only"], cwd=tmp_path)
        shown = json.loads(evo(["config", "show", "--json"], cwd=tmp_path).stdout)
        assert shown["project_name"] == tmp_path.name
        assert shown["benchmark"] == "python3 eval.py --updated"
        assert shown["metric"] == "min"
        assert shown["commit_strategy"] == "tracked-only"
        assert "runtime_env" in shown

        evo(["config", "set", "project-name", "Demo Project"], cwd=tmp_path)
        shown = json.loads(evo(["config", "show", "--json"], cwd=tmp_path).stdout)
        assert shown["project_name"] == "Demo Project"
    finally:
        shutdown_dashboard(tmp_path)


def test_config_runtime_check_runs_hooks_and_prefix(tmp_path: Path) -> None:
    init_repo(tmp_path)
    write(tmp_path / "agent.py", 'STATE = "baseline"\n')
    write(
        tmp_path / "eval.py",
        """from __future__ import annotations
import json
import os
from pathlib import Path

worktree = Path(os.environ["EVO_WORKTREE"])
score = 1.0 if (
    os.environ.get("PREFIXED") == "1"
    and (worktree / "runtime_prepared.txt").exists()
    and (worktree / "runtime_before.txt").exists()
) else 0.0
Path(os.environ["EVO_RESULT_PATH"]).write_text(json.dumps({"score": score}), encoding="utf-8")
""",
    )
    write(
        tmp_path / "gate.py",
        """from __future__ import annotations
import os
import sys

sys.exit(0 if os.environ.get("PREFIXED") == "1" else 2)
""",
    )
    run(["git", "add", "."], cwd=tmp_path)
    run(["git", "commit", "-m", "fixture: runtime recipe"], cwd=tmp_path)

    try:
        evo(
            [
                "init",
                "--target", "agent.py",
                "--benchmark", "python3 eval.py",
                "--gate", "python3 gate.py",
                "--metric", "max",
                "--host", "generic", "--per-exp-timeout", "1800",
            ],
            cwd=tmp_path,
        )
        evo(
            [
                "config",
                "runtime",
                "set",
                "--prepare", "printf prepared > runtime_prepared.txt",
                "--before-run", "printf before > runtime_before.txt",
                "--prefix", "env PREFIXED=1",
            ],
            cwd=tmp_path,
        )
        runtime = json.loads(evo(["config", "runtime", "show", "--json"], cwd=tmp_path).stdout)
        assert runtime["prepare"] == "printf prepared > runtime_prepared.txt"
        assert runtime["before_run"] == "printf before > runtime_before.txt"
        assert runtime["prefix"] == "env PREFIXED=1"

        evo(["new", "--parent", "root", "-m", "baseline"], cwd=tmp_path)
        checked = evo(["run", "exp_0000", "--check"], cwd=tmp_path)
        assert "CHECK_PASSED exp_0000 score=1.0" in checked.stdout
        check_dir = tmp_path / ".evo" / "run_0000" / "experiments" / "exp_0000" / "checks" / "001"
        payload = json.loads((check_dir / "check.json").read_text(encoding="utf-8"))
        assert [item["name"] for item in payload["runtime"]] == ["prepare", "before_run"]
        assert (check_dir / "runtime_prepare.log").exists()
        assert (check_dir / "runtime_before_run.log").exists()
        assert payload["benchmark"]["command"] == "env PREFIXED=1 python3 eval.py"
        assert payload["gates"][0]["command"] == "env PREFIXED=1 python3 gate.py"
    finally:
        shutdown_dashboard(tmp_path)


def test_run_check_writes_artifacts_without_changing_node_status(tmp_path: Path) -> None:
    init_repo(tmp_path)
    write(tmp_path / "agent.py", 'STATE = "baseline"\n')
    write(
        tmp_path / "eval.py",
        """from __future__ import annotations
import json
import os
from pathlib import Path

Path(os.environ["EVO_RESULT_PATH"]).write_text(json.dumps({"score": 0.25}), encoding="utf-8")
Path(os.environ["EVO_TRACES_DIR"]).mkdir(parents=True, exist_ok=True)
Path(os.environ["EVO_TRACES_DIR"], "task_0.json").write_text(
    json.dumps({"task_id": "0", "score": 0.25}),
    encoding="utf-8",
)
""",
    )
    write(tmp_path / "gate.py", "import sys\nsys.exit(0)\n")
    run(["git", "add", "."], cwd=tmp_path)
    run(["git", "commit", "-m", "fixture: check"], cwd=tmp_path)

    try:
        evo(
            [
                "init",
                "--target", "agent.py",
                "--benchmark", "python3 eval.py",
                "--gate", "python3 gate.py",
                "--metric", "max",
                "--host", "generic", "--per-exp-timeout", "1800",
            ],
            cwd=tmp_path,
        )
        evo(["new", "--parent", "root", "-m", "baseline"], cwd=tmp_path)
        checked = evo(["run", "exp_0000", "--check"], cwd=tmp_path)
        assert "CHECK_PASSED exp_0000 score=0.25" in checked.stdout

        graph = json.loads((tmp_path / ".evo" / "run_0000" / "graph.json").read_text(encoding="utf-8"))
        node = graph["nodes"]["exp_0000"]
        assert node["status"] == "pending"
        assert node["current_attempt"] == 0
        assert node["score"] is None

        check_dir = tmp_path / ".evo" / "run_0000" / "experiments" / "exp_0000" / "checks" / "001"
        check_payload = json.loads((check_dir / "check.json").read_text(encoding="utf-8"))
        assert check_payload["status"] == "passed"
        assert check_payload["score"] == 0.25
        assert (check_dir / "benchmark.log").exists()
        assert (check_dir / "result.json").exists()
        assert (check_dir / "traces" / "task_0.json").exists()

        result = evo(["run", "exp_0000"], cwd=tmp_path)
        assert "COMMITTED exp_0000 0.25" in result.stdout
    finally:
        shutdown_dashboard(tmp_path)


def test_gate_check_runs_gates_without_changing_node_status(tmp_path: Path) -> None:
    init_repo(tmp_path)
    write(tmp_path / "agent.py", 'STATE = "baseline"\n')
    write(tmp_path / "eval.py", 'print({"score": 0.0})\n')
    write(
        tmp_path / "gate.py",
        """from __future__ import annotations
import argparse
from pathlib import Path
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--agent", required=True)
args = parser.parse_args()
sys.exit(0 if Path(args.agent).exists() else 1)
""",
    )
    run(["git", "add", "."], cwd=tmp_path)
    run(["git", "commit", "-m", "fixture: gate check"], cwd=tmp_path)

    try:
        evo(
            [
                "init",
                "--target", "agent.py",
                "--benchmark", "python3 eval.py",
                "--metric", "max",
                "--host", "generic", "--per-exp-timeout", "1800",
            ],
            cwd=tmp_path,
        )
        evo(["gate", "add", "root", "--name", "exists", "--command", "python3 gate.py --agent {target}"], cwd=tmp_path)
        evo(["new", "--parent", "root", "-m", "baseline"], cwd=tmp_path)
        checked = evo(["gate", "check", "exp_0000"], cwd=tmp_path)
        assert "GATE_CHECK_PASSED exp_0000 1 gates" in checked.stdout

        graph = json.loads((tmp_path / ".evo" / "run_0000" / "graph.json").read_text(encoding="utf-8"))
        assert graph["nodes"]["exp_0000"]["status"] == "pending"
        check_dir = tmp_path / ".evo" / "run_0000" / "experiments" / "exp_0000" / "checks" / "001"
        payload = json.loads((check_dir / "gate_check.json").read_text(encoding="utf-8"))
        assert payload["status"] == "passed"
        assert payload["gates"][0]["name"] == "exists"
        assert (check_dir / "gates" / "exists.log").exists()
    finally:
        shutdown_dashboard(tmp_path)
