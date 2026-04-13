from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "evo"


def run(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=cwd, check=check, capture_output=True, text=True)
    return result


def evo(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["uv", "run", "--project", str(PLUGIN_ROOT), "evo", *args], cwd=cwd, check=check)


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
    return json.loads((root / ".evo" / "run_0000" / "graph.json").read_text(encoding="utf-8"))


def load_outcome(root: Path, exp_id: str, attempt: int) -> dict:
    path = root / ".evo" / "run_0000" / "experiments" / exp_id / "attempts" / f"{attempt:03d}" / "outcome.json"
    return json.loads(path.read_text(encoding="utf-8"))


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
    write(root / ".evo" / "run_0000" / "worktrees" / "exp_0001" / "agent.py", 'STATE = "GOOD"\n')
    improved = evo(["run", "exp_0001"], cwd=root)
    assert "COMMITTED exp_0001 1.0" in improved.stdout

    evo(["new", "--parent", "exp_0001", "-m", "break the gate"], cwd=root)
    write(root / ".evo" / "run_0000" / "worktrees" / "exp_0002" / "agent.py", 'STATE = "GOOD FORBIDDEN"\n')
    gated = evo(["run", "exp_0002"], cwd=root)
    assert "EVALUATED exp_0002" in gated.stdout
    assert "gate_failed" in gated.stdout

    # Gate-failing node stays evaluated with worktree + branch intact for retry.
    assert (root / ".evo" / "run_0000" / "worktrees" / "exp_0002").exists()
    branches = run(["git", "branch", "--list", "evo/run_0000/exp_0002"], cwd=root).stdout.strip()
    assert branches, "branch should persist on evaluated outcome"

    evo(["annotate", "exp_0002", "0", "gate failure"], cwd=root)

    # Explicit discard cleans up both worktree and branch.
    evo(["discard", "exp_0002", "--reason", "abandon hypothesis"], cwd=root)
    assert not (root / ".evo" / "run_0000" / "worktrees" / "exp_0002").exists()
    branches = run(["git", "branch", "--list", "evo/run_0000/exp_0002"], cwd=root).stdout.strip()
    assert not branches
    # Per-attempt artifacts preserved for forensics.
    assert (root / ".evo" / "run_0000" / "experiments" / "exp_0002" / "attempts" / "001" / "outcome.json").exists()

    evo(["prune", "exp_0000", "--reason", "dominated"], cwd=root)

    graph = load_graph(root)
    assert graph["nodes"]["exp_0000"]["status"] == "pruned"
    assert graph["nodes"]["exp_0001"]["status"] == "committed"
    assert graph["nodes"]["exp_0002"]["status"] == "discarded"
    frontier = json.loads(evo(["frontier"], cwd=root).stdout)
    assert [node["id"] for node in frontier] == ["exp_0001"]

    evo(["reset", "--yes"], cwd=root)
    assert not (root / ".evo" / "run_0000").exists()
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
    write(root / ".evo" / "run_0000" / "worktrees" / "exp_0001" / "agent.py", 'STATE = "BETTER"\n')
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
    assert (root / ".evo" / "run_0000" / "worktrees" / "exp_0000").exists()


def test_gate_flow(root: Path) -> None:
    """Test gate add/list/remove and gate blocking during run."""
    # Set up a multi-task benchmark that reports per-task scores
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
score = 1.0 if "GOOD" in content else 0.5
traces_dir = os.environ.get("EVO_TRACES_DIR")
if traces_dir:
    Path(traces_dir).mkdir(parents=True, exist_ok=True)
print(json.dumps({"score": score, "tasks": {"0": score, "1": score}}))
""",
    )
    # Gate that checks a specific behavior is preserved
    write(
        root / "gate_refund.py",
        """from __future__ import annotations
import argparse
from pathlib import Path
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--agent", required=True)
args = parser.parse_args()
content = Path(args.agent).read_text(encoding="utf-8")
# Fails if agent contains BREAK_REFUND
sys.exit(1 if "BREAK_REFUND" in content else 0)
""",
    )
    write(
        root / "gate_cancel.py",
        """from __future__ import annotations
import argparse
from pathlib import Path
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--agent", required=True)
args = parser.parse_args()
content = Path(args.agent).read_text(encoding="utf-8")
# Fails if agent contains BREAK_CANCEL
sys.exit(1 if "BREAK_CANCEL" in content else 0)
""",
    )
    run(["git", "add", "."], cwd=root)
    run(["git", "commit", "-m", "fixture: gates"], cwd=root)

    # Init workspace
    evo(["init", "--target", "agent.py", "--benchmark", "python eval.py --agent {target}", "--metric", "max"], cwd=root)

    # Add a gate on root
    evo(["gate", "add", "root", "--name", "refund_flow", "--command", "python gate_refund.py --agent {target}"], cwd=root)

    # List gates on root
    gate_list = json.loads(evo(["gate", "list", "root"], cwd=root).stdout)
    assert len(gate_list) == 1
    assert gate_list[0]["name"] == "refund_flow"
    assert gate_list[0]["from"] == "root"

    # Baseline -- should pass (no BREAK_REFUND)
    evo(["new", "--parent", "root", "-m", "baseline"], cwd=root)
    baseline = evo(["run", "exp_0000"], cwd=root)
    assert "COMMITTED exp_0000" in baseline.stdout

    # Add another gate on exp_0000 (child inherits root gate + this one)
    evo(["gate", "add", "exp_0000", "--name", "cancel_flow", "--command", "python gate_cancel.py --agent {target}"], cwd=root)

    # List effective gates on exp_0000 -- should see both
    gate_list = json.loads(evo(["gate", "list", "exp_0000"], cwd=root).stdout)
    assert len(gate_list) == 2
    names = {g["name"] for g in gate_list}
    assert names == {"refund_flow", "cancel_flow"}

    # Experiment that improves score but breaks the refund gate
    evo(["new", "--parent", "exp_0000", "-m", "break refund"], cwd=root)
    write(root / ".evo" / "run_0000" / "worktrees" / "exp_0001" / "agent.py", 'STATE = "GOOD BREAK_REFUND"\n')
    result = evo(["run", "exp_0001"], cwd=root)
    assert "GATE_FAILED" in result.stdout
    assert "EVALUATED exp_0001" in result.stdout

    # Experiment that improves score but breaks the cancel gate (inherited from exp_0000)
    evo(["new", "--parent", "exp_0000", "-m", "break cancel"], cwd=root)
    write(root / ".evo" / "run_0000" / "worktrees" / "exp_0002" / "agent.py", 'STATE = "GOOD BREAK_CANCEL"\n')
    result = evo(["run", "exp_0002"], cwd=root)
    assert "GATE_FAILED" in result.stdout
    assert "EVALUATED exp_0002" in result.stdout

    # Experiment that passes all gates
    evo(["new", "--parent", "exp_0000", "-m", "clean improvement"], cwd=root)
    write(root / ".evo" / "run_0000" / "worktrees" / "exp_0003" / "agent.py", 'STATE = "GOOD"\n')
    result = evo(["run", "exp_0003"], cwd=root)
    assert "COMMITTED exp_0003" in result.stdout
    assert "GATE_FAILED" not in result.stdout

    # Remove a gate and verify
    evo(["gate", "remove", "exp_0000", "--name", "cancel_flow"], cwd=root)
    gate_list = json.loads(evo(["gate", "list", "exp_0000"], cwd=root).stdout)
    assert len(gate_list) == 1
    assert gate_list[0]["name"] == "refund_flow"

    # Verify gate_failures stored on evaluated (not yet discarded) node.
    graph = load_graph(root)
    assert graph["nodes"]["exp_0001"]["status"] == "evaluated"
    assert graph["nodes"]["exp_0002"]["status"] == "evaluated"
    assert "refund_flow" in graph["nodes"]["exp_0001"].get("gate_failures", [])
    assert "cancel_flow" in graph["nodes"]["exp_0002"].get("gate_failures", [])

    # Verify outcome.json per attempt captures gate detail
    outcome_001 = load_outcome(root, "exp_0001", 1)
    assert outcome_001["outcome"] == "evaluated"
    gate_by_name = {g["name"]: g for g in outcome_001["gates"]}
    assert gate_by_name["refund_flow"]["passed"] is False
    assert gate_by_name["refund_flow"]["from"] == "root"


def test_retry_cap_and_fix(root: Path) -> None:
    """Covers the v0.2 lifecycle: evaluated preserves worktree, cap blocks
    retries, fix-then-retry flips to committed, discard is explicit."""
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
    evo(["new", "--parent", "root", "-m", "baseline"], cwd=root)
    evo(["run", "exp_0000"], cwd=root)
    evo(["new", "--parent", "exp_0000", "-m", "first-good"], cwd=root)
    write(root / ".evo" / "run_0000" / "worktrees" / "exp_0001" / "agent.py", 'STATE = "GOOD"\n')
    evo(["run", "exp_0001"], cwd=root)

    # Three evaluated attempts in a row to exhaust the cap.
    evo(["new", "--parent", "exp_0001", "-m", "regression loop"], cwd=root)
    wt = root / ".evo" / "run_0000" / "worktrees" / "exp_0002"
    for _ in range(3):
        write(wt / "agent.py", 'STATE = "baseline"\n')
        result = evo(["run", "exp_0002"], cwd=root)
        assert "EVALUATED exp_0002" in result.stdout

    graph = load_graph(root)
    assert graph["nodes"]["exp_0002"]["status"] == "evaluated"
    assert graph["nodes"]["exp_0002"]["evaluated_attempts"] == 3
    assert wt.exists(), "worktree preserved across evaluated retries"

    # Fourth run refused by cap.
    blocked = evo(["run", "exp_0002"], cwd=root, check=False)
    assert blocked.returncode == 1
    assert "exhausted 3/3 attempts" in blocked.stderr

    # Each evaluated attempt wrote its own outcome.json.
    for i in (1, 2, 3):
        o = load_outcome(root, "exp_0002", i)
        assert o["outcome"] == "evaluated"
        assert o["attempt"] == i

    # Explicit discard on cap-exhausted node deletes both worktree and branch.
    evo(["discard", "exp_0002", "--reason", "exhausted"], cwd=root)
    assert not wt.exists()
    branches = run(["git", "branch", "--list", "evo/run_0000/exp_0002"], cwd=root).stdout.strip()
    assert not branches
    graph = load_graph(root)
    assert graph["nodes"]["exp_0002"]["status"] == "discarded"

    # Fix-then-retry from scratch: branch a new exp, regress once, then fix.
    evo(["new", "--parent", "exp_0001", "-m", "fix flow"], cwd=root)
    wt3 = root / ".evo" / "run_0000" / "worktrees" / "exp_0003"
    write(wt3 / "agent.py", 'STATE = "baseline"\n')
    first = evo(["run", "exp_0003"], cwd=root)
    assert "EVALUATED exp_0003" in first.stdout
    # Now agent fixes the edit in the SAME worktree and re-runs.
    write(wt3 / "agent.py", 'STATE = "GOOD v2"\n')
    second = evo(["run", "exp_0003"], cwd=root)
    assert "COMMITTED exp_0003" in second.stdout
    graph = load_graph(root)
    assert graph["nodes"]["exp_0003"]["status"] == "committed"
    # Both attempt outcome.json files persist side by side.
    assert load_outcome(root, "exp_0003", 1)["outcome"] == "evaluated"
    assert load_outcome(root, "exp_0003", 2)["outcome"] == "committed"


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

        gate_repo = temp_root / "gate-repo"
        gate_repo.mkdir()
        init_repo(gate_repo)
        test_gate_flow(gate_repo)

        retry_repo = temp_root / "retry-repo"
        retry_repo.mkdir()
        init_repo(retry_repo)
        setup_max_repo(retry_repo)
        test_retry_cap_and_fix(retry_repo)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    print("E2E OK")


if __name__ == "__main__":
    main()
