from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def run(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=cwd, check=check, capture_output=True, text=True)
    return result


def evo(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["uv", "run", "--project", str(REPO_ROOT), "evo", *args], cwd=cwd, check=check)


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def init_repo(root: Path) -> None:
    run(["git", "init", "-b", "main"], cwd=root)
    run(["git", "config", "user.name", "evo"], cwd=root)
    run(["git", "config", "user.email", "evo@example.com"], cwd=root)


def setup_max_repo(root: Path) -> None:
    write(
        root / "agent.py",
        'STATE = "baseline"\n',
    )
    write(
        root / "eval.py",
        """from __future__ import annotations
import argparse
import json
import os
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--agent", required=True)
args = parser.parse_args()
content = Path(args.agent).read_text(encoding="utf-8")
score = 1.0 if "GOOD" in content else 0.0
traces_dir = os.environ.get("EVO_TRACES_DIR")
if traces_dir:
    Path(traces_dir).mkdir(parents=True, exist_ok=True)
    Path(traces_dir, "task_0.json").write_text(json.dumps({
        "experiment_id": "external",
        "task_id": "0",
        "status": "passed" if score > 0 else "failed",
        "score": score
    }, indent=2), encoding="utf-8")
print(json.dumps({"score": score, "tasks": {"0": score}}))
""",
    )
    write(
        root / "gate.py",
        """from __future__ import annotations
import argparse
from pathlib import Path
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--agent", required=True)
args = parser.parse_args()
content = Path(args.agent).read_text(encoding="utf-8")
sys.exit(1 if "FORBIDDEN" in content else 0)
""",
    )
    run(["git", "add", "."], cwd=root)
    run(["git", "commit", "-m", "fixture: max"], cwd=root)


def setup_min_repo(root: Path) -> None:
    write(
        root / "agent.py",
        'STATE = "baseline"\n',
    )
    write(
        root / "eval.py",
        """from __future__ import annotations
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--agent", required=True)
args = parser.parse_args()
content = Path(args.agent).read_text(encoding="utf-8")
score = 5.0 if "BETTER" in content else 10.0
print(json.dumps({"score": score, "tasks": {"0": score}}))
""",
    )
    run(["git", "add", "."], cwd=root)
    run(["git", "commit", "-m", "fixture: min"], cwd=root)


def load_graph(root: Path) -> dict:
    return json.loads((root / ".evo" / "graph.json").read_text(encoding="utf-8"))


def parse_last_json_blob(text: str) -> dict:
    start = text.rfind("{")
    if start == -1:
        raise ValueError(f"No JSON object found in output: {text!r}")
    return json.loads(text[start:])


def test_max_flow(root: Path) -> None:
    evo(
        [
            "init",
            "--target",
            "agent.py",
            "--benchmark",
            "python eval.py --agent {target}",
            "--gate",
            "python gate.py --agent {target}",
            "--metric",
            "max",
        ],
        cwd=root,
    )
    evo(["new", "--parent", "root", "-m", "baseline"], cwd=root)
    baseline = evo(["run", "exp_0000"], cwd=root)
    assert "COMMITTED exp_0000 0.0" in baseline.stdout

    evo(["new", "--parent", "exp_0000", "-m", "make it good"], cwd=root)
    write(root / ".evo" / "worktrees" / "exp_0001" / "agent.py", 'STATE = "GOOD"\n')
    improved = evo(["run", "exp_0001"], cwd=root)
    assert "COMMITTED exp_0001 1.0" in improved.stdout

    evo(["new", "--parent", "exp_0001", "-m", "break the gate"], cwd=root)
    write(root / ".evo" / "worktrees" / "exp_0002" / "agent.py", 'STATE = "GOOD FORBIDDEN"\n')
    gated = evo(["run", "exp_0002"], cwd=root)
    assert "DISCARDED exp_0002 1.0" in gated.stdout

    evo(["annotate", "exp_0002", "0", "gate failure"], cwd=root)
    evo(["prune", "exp_0000", "--reason", "dominated"], cwd=root)

    graph = load_graph(root)
    assert graph["nodes"]["exp_0000"]["status"] == "pruned"
    assert graph["nodes"]["exp_0001"]["status"] == "committed"
    assert graph["nodes"]["exp_0002"]["status"] == "discarded"
    frontier = json.loads(evo(["frontier"], cwd=root).stdout)
    assert [node["id"] for node in frontier] == ["exp_0001"]

    evo(["reset", "--yes"], cwd=root)
    assert not (root / ".evo").exists()
    branches = run(["git", "branch", "--list", "evo/*"], cwd=root).stdout.strip()
    assert not branches


def test_min_flow(root: Path) -> None:
    evo(
        [
            "init",
            "--target",
            "agent.py",
            "--benchmark",
            "python eval.py --agent {target}",
            "--metric",
            "min",
        ],
        cwd=root,
    )
    evo(["new", "--parent", "root", "-m", "baseline"], cwd=root)
    baseline = evo(["run", "exp_0000"], cwd=root)
    assert "COMMITTED exp_0000 10.0" in baseline.stdout

    evo(["new", "--parent", "exp_0000", "-m", "lower score"], cwd=root)
    write(root / ".evo" / "worktrees" / "exp_0001" / "agent.py", 'STATE = "BETTER"\n')
    improved = evo(["run", "exp_0001"], cwd=root)
    assert "COMMITTED exp_0001 5.0" in improved.stdout

    graph = load_graph(root)
    assert graph["nodes"]["exp_0001"]["score"] == 5.0
    status = evo(["status"], cwd=root).stdout
    assert "metric=min" in status
    assert "best=5.0" in status


def test_stale_branch_recovery(root: Path) -> None:
    evo(
        [
            "init",
            "--target",
            "agent.py",
            "--benchmark",
            "python eval.py --agent {target}",
            "--metric",
            "max",
        ],
        cwd=root,
    )
    run(["git", "branch", "evo/exp_0000"], cwd=root)
    created = evo(["new", "--parent", "root", "-m", "recover stale branch"], cwd=root)
    payload = parse_last_json_blob(created.stdout)
    assert payload["id"] == "exp_0000"
    assert (root / ".evo" / "worktrees" / "exp_0000").exists()


def main() -> None:
    temp_root = Path(tempfile.mkdtemp(prefix="evo-e2e-"))
    try:
        max_repo = temp_root / "max-repo"
        max_repo.mkdir()
        init_repo(max_repo)
        setup_max_repo(max_repo)
        test_max_flow(max_repo)

        min_repo = temp_root / "min-repo"
        min_repo.mkdir()
        init_repo(min_repo)
        setup_min_repo(min_repo)
        test_min_flow(min_repo)

        stale_repo = temp_root / "stale-repo"
        stale_repo.mkdir()
        init_repo(stale_repo)
        setup_max_repo(stale_repo)
        test_stale_branch_recovery(stale_repo)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    print("E2E OK")


if __name__ == "__main__":
    main()
