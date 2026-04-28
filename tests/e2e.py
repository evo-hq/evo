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


def setup_sdk_repo(root: Path) -> None:
    """Benchmark uses the SDK; exercises the EVO_RESULT_PATH file channel."""
    sdk_src = REPO_ROOT / "sdk" / "python" / "src"
    write(
        root / "agent.py",
        'STATE = "baseline"\n',
    )
    write(
        root / "eval.py",
        f"""from __future__ import annotations
import argparse
import sys
from pathlib import Path

sys.path.insert(0, r"{sdk_src}")
from evo_agent import Run

parser = argparse.ArgumentParser()
parser.add_argument("--agent", required=True)
args = parser.parse_args()
content = Path(args.agent).read_text(encoding="utf-8")
score = 1.0 if "GOOD" in content else 0.0

# Stdout noise that would have tripped the old line-scan parser.
print("score: 0.99 (warmup)")
print("0.42")
with Run() as run:
    run.report("0", score=score, summary="ok" if score > 0 else "fail")
print({{"score": 0.5, "spurious": True}})
""",
    )
    run(["git", "add", "."], cwd=root)
    run(["git", "commit", "-m", "fixture: sdk"], cwd=root)


def test_sdk_result_file_flow(root: Path) -> None:
    """SDK benchmark publishes via result.json; stdout noise is ignored."""
    evo(
        [
            "init",
            "--target",
            "agent.py",
            "--benchmark",
            "python eval.py --agent {target}",
            "--metric",
            "max",
            "--host",
            "generic",
        ],
        cwd=root,
    )
    evo(["new", "--parent", "root", "-m", "baseline"], cwd=root)
    baseline = evo(["run", "exp_0000"], cwd=root)
    assert "COMMITTED exp_0000 0.0" in baseline.stdout, baseline.stdout

    a_dir = root / ".evo" / "run_0000" / "experiments" / "exp_0000" / "attempts" / "001"
    result_path = a_dir / "result.json"
    assert result_path.exists()
    written = json.loads(result_path.read_text(encoding="utf-8"))
    assert written["score"] == 0.0, written
    assert written["tasks"] == {"0": 0.0}, written

    log_text = (a_dir / "benchmark.log").read_text(encoding="utf-8")
    assert "score: 0.99" in log_text
    assert '"spurious": True' in log_text or "'spurious': True" in log_text
    assert '"score": 0.5' not in log_text

    outcome = load_outcome(root, "exp_0000", 1)
    assert outcome["benchmark"]["result"]["score"] == 0.0
    assert outcome["benchmark"]["result"]["tasks"] == {"0": 0.0}

    leftovers = [p.name for p in a_dir.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], f"leftover tmp files: {leftovers}"

    evo(["new", "--parent", "exp_0000", "-m", "make it good"], cwd=root)
    write(root / ".evo" / "run_0000" / "worktrees" / "exp_0001" / "agent.py", 'STATE = "GOOD"\n')
    improved = evo(["run", "exp_0001"], cwd=root)
    assert "COMMITTED exp_0001 1.0" in improved.stdout

    a_dir2 = root / ".evo" / "run_0000" / "experiments" / "exp_0001" / "attempts" / "001"
    written2 = json.loads((a_dir2 / "result.json").read_text(encoding="utf-8"))
    assert written2["score"] == 1.0


def setup_sdk_repo_with_gate(root: Path) -> None:
    """Benchmark + SDK-using gate. Without env scoping the gate would
    overwrite result.json via inherited EVO_RESULT_PATH."""
    sdk_src = REPO_ROOT / "sdk" / "python" / "src"
    write(
        root / "agent.py",
        'STATE = "baseline"\n',
    )
    write(
        root / "eval.py",
        f"""from __future__ import annotations
import argparse
import sys
from pathlib import Path

sys.path.insert(0, r"{sdk_src}")
from evo_agent import Run

parser = argparse.ArgumentParser()
parser.add_argument("--agent", required=True)
args = parser.parse_args()
content = Path(args.agent).read_text(encoding="utf-8")
score = 1.0 if "GOOD" in content else 0.0
with Run() as run:
    run.report("0", score=score)
""",
    )
    write(
        root / "gate.py",
        f"""from __future__ import annotations
import argparse
import sys
from pathlib import Path

sys.path.insert(0, r"{sdk_src}")
from evo_agent import Run

parser = argparse.ArgumentParser()
parser.add_argument("--agent", required=True)
args = parser.parse_args()
# Sentinel score so an overwrite shows up in the assertion.
with Run() as run:
    run.report("gate_task", score=0.5555)
sys.exit(0)
""",
    )
    run(["git", "add", "."], cwd=root)
    run(["git", "commit", "-m", "fixture: sdk + gate"], cwd=root)


def test_sdk_gate_does_not_overwrite_result_file(root: Path) -> None:
    """Gate using the SDK must not overwrite result.json via inherited env."""
    evo(
        [
            "init",
            "--target", "agent.py",
            "--benchmark", "python eval.py --agent {target}",
            "--gate", "python gate.py --agent {target}",
            "--metric", "max",
            "--host", "generic",
        ],
        cwd=root,
    )
    evo(["new", "--parent", "root", "-m", "baseline"], cwd=root)
    result = evo(["run", "exp_0000"], cwd=root)
    assert "COMMITTED exp_0000 0.0" in result.stdout, result.stdout

    a_dir = root / ".evo" / "run_0000" / "experiments" / "exp_0000" / "attempts" / "001"
    written = json.loads((a_dir / "result.json").read_text(encoding="utf-8"))
    outcome = load_outcome(root, "exp_0000", 1)

    assert written["score"] == 0.0, written
    assert written["tasks"] == {"0": 0.0}, written
    assert outcome["benchmark"]["result"]["score"] == written["score"]


def setup_sdk_repo_with_gate_overlapping_trace(root: Path) -> None:
    """Benchmark + gate both report on task "0" with different scores; used
    to detect gate trace clobbering benchmark trace via inherited env."""
    sdk_src = REPO_ROOT / "sdk" / "python" / "src"
    write(
        root / "agent.py",
        'STATE = "baseline"\n',
    )
    write(
        root / "eval.py",
        f"""from __future__ import annotations
import argparse
import sys
from pathlib import Path

sys.path.insert(0, r"{sdk_src}")
from evo_agent import Run

parser = argparse.ArgumentParser()
parser.add_argument("--agent", required=True)
args = parser.parse_args()
content = Path(args.agent).read_text(encoding="utf-8")
score = 1.0 if "GOOD" in content else 0.0
with Run() as run:
    run.report("0", score=score, summary="benchmark")
""",
    )
    write(
        root / "gate.py",
        f"""from __future__ import annotations
import argparse
import sys
from pathlib import Path

sys.path.insert(0, r"{sdk_src}")
from evo_agent import Run

parser = argparse.ArgumentParser()
parser.add_argument("--agent", required=True)
args = parser.parse_args()
# Same task id as the benchmark with a sentinel score; an overwrite is detectable.
with Run() as run:
    run.report("0", score=0.7777, summary="GATE_SENTINEL")
sys.exit(0)
""",
    )
    run(["git", "add", "."], cwd=root)
    run(["git", "commit", "-m", "fixture: sdk + gate overlapping trace"], cwd=root)


def test_sdk_gate_does_not_overwrite_benchmark_traces(root: Path) -> None:
    """Gate using the SDK must not overwrite benchmark task traces via
    inherited EVO_TRACES_DIR."""
    evo(
        [
            "init",
            "--target", "agent.py",
            "--benchmark", "python eval.py --agent {target}",
            "--gate", "python gate.py --agent {target}",
            "--metric", "max",
            "--host", "generic",
        ],
        cwd=root,
    )
    evo(["new", "--parent", "root", "-m", "baseline"], cwd=root)
    result = evo(["run", "exp_0000"], cwd=root)
    assert "COMMITTED exp_0000 0.0" in result.stdout, result.stdout

    a_dir = root / ".evo" / "run_0000" / "experiments" / "exp_0000" / "attempts" / "001"
    trace_path = a_dir / "traces" / "task_0.json"
    assert trace_path.exists(), f"missing benchmark trace at {trace_path}"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))

    assert trace["score"] == 0.0, trace
    assert trace.get("summary") == "benchmark", trace


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
            "--host",
            "generic",
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
    # `evo frontier` emits an envelope `{strategy, nodes: [...], generated_at}`
    # since commit 641813a (frontier: configurable selection strategies).
    assert [node["id"] for node in frontier["nodes"]] == ["exp_0001"]

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
            "--host",
            "generic",
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
            "--host",
            "generic",
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
    evo(["init", "--target", "agent.py", "--benchmark", "python eval.py --agent {target}", "--metric", "max", "--host", "generic"], cwd=root)

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

    # `evo get` returns effective gates (own + inherited) and exposes
    # own-only gates under `own_gates`. exp_0000 inherits refund_flow
    # from root and owns cancel_flow.
    got = json.loads(evo(["get", "exp_0000"], cwd=root).stdout)
    assert {g["name"] for g in got["gates"]} == {"refund_flow", "cancel_flow"}
    assert {g["name"] for g in got["own_gates"]} == {"cancel_flow"}

    # For root, effective and own are identical.
    got_root = json.loads(evo(["get", "root"], cwd=root).stdout)
    assert {g["name"] for g in got_root["gates"]} == {"refund_flow"}
    assert {g["name"] for g in got_root["own_gates"]} == {"refund_flow"}

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
            "--host",
            "generic",
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


# ===========================================================================
# Live dispatch tests (EVO_LIVE_TEST_CLAUDE=1). Real claude -p / codex exec
# subprocesses, real LLM calls, real workspaces. ~$0.30-$1 per full run.
# ===========================================================================


import os as _os
import shutil as _shutil
import sys as _sys

PLUGIN_SRC = PLUGIN_ROOT / "src"


def _setup_dispatch_workspace(root: Path, *, host: str | None) -> None:
    """Trivial workspace just rich enough to hold an explorer + record."""
    write(root / "bench.sh", "echo score:1.0\n")
    (root / "bench.sh").chmod(0o755)
    args = [
        "init",
        "--target", "bench.sh",
        "--benchmark", "./bench.sh",
        "--metric", "max",
    ]
    if host is not None:
        args += ["--host", host]
    evo(args, cwd=root)


def _strip_host_from_meta(root: Path) -> None:
    """Simulate a workspace built before the host signature field existed."""
    p = root / ".evo" / "meta.json"
    meta = json.loads(p.read_text(encoding="utf-8"))
    meta.pop("host", None)
    p.write_text(json.dumps(meta), encoding="utf-8")


def _import_dispatch():
    if str(PLUGIN_SRC) not in _sys.path:
        _sys.path.insert(0, str(PLUGIN_SRC))
    import evo.dispatch as dispatch  # noqa: WPS433
    import evo.core as core  # noqa: WPS433
    return dispatch, core


def _shutdown_dashboard(root: Path) -> None:
    pid_file = root / ".evo" / "dashboard.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            _os.kill(pid, 15)
        except (ValueError, ProcessLookupError, OSError):
            pass


def _resolve_test_evo_bin() -> Path:
    """Pick the evo binary the test should hand to a spawned orchestrator.

    Priority: EVO_BIN env override → plugin's .venv/bin/evo → system PATH.
    Spawned orchestrator inherits the test's PATH but the user may have an
    older `evo` installed globally that lacks the host subcommand. Pass an
    explicit absolute path to make the test deterministic."""
    override = _os.environ.get("EVO_BIN")
    if override:
        return Path(override)
    venv_bin = PLUGIN_ROOT / ".venv" / "bin" / "evo"
    if venv_bin.exists():
        return venv_bin
    found = _shutil.which("evo")
    if not found:
        raise RuntimeError("no evo binary found; set EVO_BIN or install plugins/evo")
    return Path(found)


def test_dispatch_ensure_explorer_spawns_and_persists(root: Path) -> None:
    """Real claude -p call through dispatch.ensure_explorer; assert record
    is written to disk with all required fields."""
    dispatch, core = _import_dispatch()
    _setup_dispatch_workspace(root, host="claude-code")
    record = dispatch.ensure_explorer(root, parent_id="root")
    assert record["host"] == "claude-code", record
    assert record["session_id"], record
    assert record["worktree_commit"], record
    assert record["skill_hash"], record
    assert record["ttl_expires_at"], record
    on_disk = json.loads(dispatch.explorer_record_path(root, "root").read_text())
    assert on_disk["session_id"] == record["session_id"]
    print("  PASS test_dispatch_ensure_explorer_spawns_and_persists")


def test_dispatch_ensure_explorer_reuses_within_ttl(root: Path) -> None:
    """Second call with the same parent reuses the record (same session_id,
    same created_at) — proves the cache predicate works."""
    dispatch, core = _import_dispatch()
    _setup_dispatch_workspace(root, host="claude-code")
    rec1 = dispatch.ensure_explorer(root, parent_id="root")
    rec2 = dispatch.ensure_explorer(root, parent_id="root")
    assert rec2["session_id"] == rec1["session_id"], (rec1["session_id"], rec2["session_id"])
    assert rec2["created_at"] == rec1["created_at"]
    print("  PASS test_dispatch_ensure_explorer_reuses_within_ttl")


def test_dispatch_ensure_explorer_rebuilds_when_skill_changes(root: Path) -> None:
    """Edit subagent/SKILL.md → next ensure_explorer rebuilds with new
    session_id. Restores the skill in finally."""
    dispatch, core = _import_dispatch()
    _setup_dispatch_workspace(root, host="claude-code")
    rec1 = dispatch.ensure_explorer(root, parent_id="root")
    skill = dispatch.subagent_skill_path()
    original = skill.read_text(encoding="utf-8")
    try:
        skill.write_text(original + "\n<!-- live e2e invalidation marker -->\n", encoding="utf-8")
        rec2 = dispatch.ensure_explorer(root, parent_id="root")
        assert rec2["session_id"] != rec1["session_id"], "skill change must rebuild explorer"
        assert rec2["skill_hash"] != rec1["skill_hash"]
    finally:
        skill.write_text(original, encoding="utf-8")
    print("  PASS test_dispatch_ensure_explorer_rebuilds_when_skill_changes")


_CLAUDE_ORCH_PROMPT = (
    "You are an evo optimization orchestrator running in Claude Code. "
    "The workspace at {workspace} predates the host signature field. "
    "Read {plugin_root}/skills/optimize/SKILL.md and follow step 0.1 "
    "EXACTLY ONCE on that workspace, then stop. Use exactly this binary "
    "for every evo invocation (do not rely on PATH): {evo_bin}. "
    "After you run the migration command, run '{evo_bin} host show' from "
    "that workspace and report what it returns. Do not do anything else "
    "from the optimize loop beyond step 0.1."
)


def test_dispatch_orchestrator_step01_claude(root: Path) -> None:
    """Dogfood: real claude -p as orchestrator runs optimize/SKILL.md
    step 0.1 on a pre-upgrade workspace; assert host gets set to
    claude-code via the orchestrator's own self-declaration."""
    dispatch, core = _import_dispatch()
    _setup_dispatch_workspace(root, host="claude-code")
    _strip_host_from_meta(root)
    _shutdown_dashboard(root)
    assert core.get_host(root) is None

    evo_bin = _resolve_test_evo_bin()
    prompt = _CLAUDE_ORCH_PROMPT.format(
        workspace=str(root),
        plugin_root=str(PLUGIN_ROOT),
        evo_bin=str(evo_bin),
    )

    proc = subprocess.run(
        [
            "claude", "-p",
            "--output-format", "json",
            "--permission-mode", "bypassPermissions",
            "--add-dir", str(root),
            "--add-dir", str(PLUGIN_ROOT),
        ],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, f"claude -p failed: {proc.stderr[:500]}"

    events = json.loads(proc.stdout)
    result = next((e for e in events if e.get("type") == "result"), None)
    assert result is not None, "no result event in claude -p output"
    assert result.get("subtype") == "success", f"orchestrator failed: {result.get('result', '')[:300]}"

    final_host = core.get_host(root)
    assert final_host == "claude-code", (
        f"claude orchestrator did not run step 0.1 correctly. "
        f"final host: {final_host!r}; "
        f"orchestrator final message: {result.get('result', '')[:400]}"
    )
    print(f"  PASS test_dispatch_orchestrator_step01_claude (cost ${result.get('total_cost_usd', 0):.2f})")


_CODEX_ORCH_PROMPT = (
    "You are an evo optimization orchestrator running in Codex CLI. "
    "The workspace at {workspace} predates the host signature field. "
    "Read {plugin_root}/skills/optimize/SKILL.md and follow step 0.1 "
    "EXACTLY ONCE on that workspace, then stop. Use exactly this binary "
    "for every evo invocation (do not rely on PATH): {evo_bin}. "
    "Your runtime is Codex (not Claude Code) — declare it as `codex` per "
    "the SUPPORTED_HOSTS list in the skill. After you run the migration "
    "command, run '{evo_bin} host show' from that workspace and report "
    "what it returns. Do not do anything else from the optimize loop "
    "beyond step 0.1. Do not attempt `evo dispatch` — it is unsupported "
    "on this host."
)


def test_dispatch_orchestrator_step01_codex(root: Path) -> None:
    """Same dogfood as the claude variant but spawning real `codex exec`.
    Asserts host becomes 'codex' and that no explorer was created (i.e.
    dispatch was correctly skipped by the orchestrator)."""
    dispatch, core = _import_dispatch()
    if _shutil.which("codex") is None:
        print("  SKIP test_dispatch_orchestrator_step01_codex (codex not on PATH)")
        return

    _setup_dispatch_workspace(root, host="codex")
    _strip_host_from_meta(root)
    _shutdown_dashboard(root)
    assert core.get_host(root) is None

    evo_bin = _resolve_test_evo_bin()
    prompt = _CODEX_ORCH_PROMPT.format(
        workspace=str(root),
        plugin_root=str(PLUGIN_ROOT),
        evo_bin=str(evo_bin),
    )

    proc = subprocess.run(
        [
            "codex", "exec",
            "--json",
            # Test runs in an isolated tmpdir with no real benchmark; safe to
            # bypass approvals so codex doesn't hang waiting for permission.
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            # Pin reasoning effort low and ignore the user's ~/.codex/config.toml
            # so a personal `model_reasoning_effort = "high"` doesn't make this
            # test churn for many minutes on a trivial step-0.1 task.
            "--ignore-user-config",
            "-c", "model_reasoning_effort=low",
            "-",
        ],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=600,
    )

    final_host = core.get_host(root)
    assert final_host == "codex", (
        f"codex orchestrator did not declare host=codex correctly. "
        f"final host: {final_host!r}; codex stderr: {proc.stderr[:300]}"
    )
    explorer_path = dispatch.explorer_record_path(root, "root")
    assert not explorer_path.exists(), (
        f"codex orchestrator should not have invoked dispatch, but "
        f"{explorer_path} exists"
    )
    print(f"  PASS test_dispatch_orchestrator_step01_codex (host={final_host}, dispatch not used)")


def test_pruned_parent_rejects_new_children(root: Path) -> None:
    """`evo new --parent <pruned_id>` must error out with a clear message.
    Pruning is a hard contract, not just a status flag."""
    evo(
        ["init", "--target", "agent.py",
         "--benchmark", "python eval.py --agent {target}",
         "--metric", "max", "--host", "generic"],
        cwd=root,
    )
    evo(["new", "--parent", "root", "-m", "baseline"], cwd=root)
    out = evo(["run", "exp_0000"], cwd=root)
    assert "COMMITTED exp_0000" in out.stdout
    evo(["prune", "exp_0000", "--reason", "test prune"], cwd=root)

    # Must error out, not silently allocate.
    result = evo(["new", "--parent", "exp_0000", "-m", "should fail"], cwd=root, check=False)
    assert result.returncode != 0, result.stdout
    assert "pruned" in (result.stderr + result.stdout).lower(), result.stderr

    # Graph state unchanged: no exp_0001 was allocated.
    graph = load_graph(root)
    assert "exp_0001" not in graph["nodes"], graph["nodes"].keys()


def test_scratchpad_aggregates_per_node_notes(root: Path) -> None:
    """`evo set --note` writes per-node notes; the scratchpad's Notes section
    must include them. Previously the section read only the legacy notes.md
    and missed all `evo set --note` writes."""
    evo(
        ["init", "--target", "agent.py",
         "--benchmark", "python eval.py --agent {target}",
         "--metric", "max", "--host", "generic"],
        cwd=root,
    )
    evo(["new", "--parent", "root", "-m", "baseline"], cwd=root)
    evo(["run", "exp_0000"], cwd=root)
    evo(["set", "exp_0000", "--note", "this is a per-node note for exp_0000"], cwd=root)
    evo(["scratchpad"], cwd=root)
    scratchpad = (root / ".evo" / "run_0000" / "scratchpad.md").read_text(encoding="utf-8")
    assert "## Notes" in scratchpad
    assert "this is a per-node note for exp_0000" in scratchpad, scratchpad


def test_evo_done_writes_attempt_scoped_traces(root: Path) -> None:
    """`evo done --traces` must write into attempts/NNN/traces/ so that
    `evo traces` and the dashboard surface manually-recorded traces. Before
    this fix, traces landed under experiments/<id>/traces/ which neither
    reader looks at."""
    evo(
        ["init", "--target", "agent.py",
         "--benchmark", "python eval.py --agent {target}",
         "--metric", "max", "--host", "generic"],
        cwd=root,
    )
    evo(["new", "--parent", "root", "-m", "manual record"], cwd=root)
    # Stage a fake traces directory mimicking what an out-of-band benchmark
    # would produce.
    fake = root.parent / "fake-traces"
    fake.mkdir(exist_ok=True)
    (fake / "task_0.json").write_text(
        json.dumps({"experiment_id": "exp_0000", "task_id": "0", "status": "passed", "score": 1.0}),
        encoding="utf-8",
    )
    evo(["done", "exp_0000", "--score", "0.5", "--traces", str(fake)], cwd=root)

    attempts_dir = root / ".evo" / "run_0000" / "experiments" / "exp_0000" / "attempts"
    attempt_dirs = sorted(attempts_dir.iterdir())
    assert len(attempt_dirs) == 1, [p.name for p in attempt_dirs]
    traces_dir = attempt_dirs[0] / "traces"
    assert (traces_dir / "task_0.json").exists(), list(attempt_dirs[0].iterdir())

    # `evo traces` should now find it.
    out = evo(["traces", "exp_0000", "0"], cwd=root)
    assert "passed" in out.stdout, out.stdout


def test_dashboard_traces_and_log_routes(root: Path) -> None:
    """Dashboard must read attempt-scoped traces (not the never-written
    experiments/<id>/traces/ path) and accept nested log paths so callers
    can request attempts/001/benchmark.log."""
    from evo.dashboard import create_app
    evo(
        ["init", "--target", "agent.py",
         "--benchmark", "python eval.py --agent {target}",
         "--metric", "max", "--host", "generic"],
        cwd=root,
    )
    evo(["new", "--parent", "root", "-m", "for dashboard"], cwd=root)
    evo(["run", "exp_0000"], cwd=root)

    app = create_app(root=root)
    client = app.test_client()
    try:

        # /traces returns the attempt-scoped traces dict
        resp = client.get("/api/node/exp_0000/traces")
        assert resp.status_code == 200, resp.status_code
        payload = json.loads(resp.data)
        # The max-repo benchmark writes task_0.json into EVO_TRACES_DIR
        assert "task_0.json" in payload, payload

        # /traces/<task_id> returns the specific task envelope
        resp = client.get("/api/node/exp_0000/traces/0")
        assert resp.status_code == 200, resp.status_code
        envelope = json.loads(resp.data)
        assert envelope.get("task_id") == "0", envelope

        # /log/<filename> with bare name auto-resolves to latest attempt
        resp = client.get("/api/node/exp_0000/log/benchmark.log")
        assert resp.status_code == 200
        assert resp.data, "benchmark.log should be non-empty"

        # /log/<path> with explicit attempts/NNN/ path also works
        resp = client.get("/api/node/exp_0000/log/attempts/001/benchmark.log")
        assert resp.status_code == 200
        assert resp.data

        # Path traversal is blocked
        resp = client.get("/api/node/exp_0000/log/../../etc/passwd")
        assert resp.status_code in (400, 404)
    finally:
        pass


def test_worktree_mode_commit_strategy_unchanged(root: Path) -> None:
    """Regression: alpha.2 must not alter worktree-mode behavior. Default
    commit_strategy is 'all', `--i-staged-new-files yes` is a silent no-op,
    untracked non-gitignored files in the worktree get committed."""
    evo(
        ["init", "--target", "agent.py",
         "--benchmark", "python eval.py --agent {target}",
         "--metric", "max", "--host", "generic"],
        cwd=root,
    )
    config = json.loads(
        (root / ".evo" / "run_0000" / "config.json").read_text(encoding="utf-8")
    )
    assert config["commit_strategy"] == "all", config
    assert config["execution_backend"] == "worktree", config

    evo(["new", "--parent", "root", "-m", "baseline"], cwd=root)
    out_baseline = evo(["run", "exp_0000"], cwd=root)
    assert "COMMITTED exp_0000" in out_baseline.stdout

    evo(["new", "--parent", "exp_0000", "-m", "edit and stray"], cwd=root)
    wt = root / ".evo" / "run_0000" / "worktrees" / "exp_0001"
    write(wt / "agent.py", 'STATE = "GOOD"\n')
    # An untracked, non-gitignored file in the worktree. In worktree mode
    # this should be committed (`git add -A`) without requiring any ack.
    write(wt / "scratch.txt", "scratch\n")
    out = evo(
        ["run", "exp_0001", "--i-staged-new-files", "yes"], cwd=root
    )
    assert "COMMITTED exp_0001" in out.stdout, out.stdout

    graph = load_graph(root)
    commit_sha = graph["nodes"]["exp_0001"]["commit"]
    assert commit_sha
    committed = run(
        ["git", "show", "--name-only", "--pretty=", commit_sha], cwd=root
    ).stdout.splitlines()
    assert "scratch.txt" in committed, committed
    assert "agent.py" in committed, committed


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

        sdk_repo = temp_root / "sdk-repo"
        sdk_repo.mkdir()
        init_repo(sdk_repo)
        setup_sdk_repo(sdk_repo)
        try:
            test_sdk_result_file_flow(sdk_repo)
        finally:
            _shutdown_dashboard(sdk_repo)

        sdk_gate_repo = temp_root / "sdk-gate-repo"
        sdk_gate_repo.mkdir()
        init_repo(sdk_gate_repo)
        setup_sdk_repo_with_gate(sdk_gate_repo)
        try:
            test_sdk_gate_does_not_overwrite_result_file(sdk_gate_repo)
        finally:
            _shutdown_dashboard(sdk_gate_repo)

        sdk_gate_trace_repo = temp_root / "sdk-gate-trace-repo"
        sdk_gate_trace_repo.mkdir()
        init_repo(sdk_gate_trace_repo)
        setup_sdk_repo_with_gate_overlapping_trace(sdk_gate_trace_repo)
        try:
            test_sdk_gate_does_not_overwrite_benchmark_traces(sdk_gate_trace_repo)
        finally:
            _shutdown_dashboard(sdk_gate_trace_repo)

        worktree_strategy_repo = temp_root / "worktree-strategy-repo"
        worktree_strategy_repo.mkdir()
        init_repo(worktree_strategy_repo)
        setup_max_repo(worktree_strategy_repo)
        test_worktree_mode_commit_strategy_unchanged(worktree_strategy_repo)

        prune_repo = temp_root / "prune-repo"
        prune_repo.mkdir()
        init_repo(prune_repo)
        setup_max_repo(prune_repo)
        test_pruned_parent_rejects_new_children(prune_repo)

        notes_repo = temp_root / "notes-repo"
        notes_repo.mkdir()
        init_repo(notes_repo)
        setup_max_repo(notes_repo)
        test_scratchpad_aggregates_per_node_notes(notes_repo)

        done_repo = temp_root / "done-repo"
        done_repo.mkdir()
        init_repo(done_repo)
        setup_max_repo(done_repo)
        test_evo_done_writes_attempt_scoped_traces(done_repo)

        dashboard_repo = temp_root / "dashboard-repo"
        dashboard_repo.mkdir()
        init_repo(dashboard_repo)
        setup_max_repo(dashboard_repo)
        try:
            test_dashboard_traces_and_log_routes(dashboard_repo)
        finally:
            _shutdown_dashboard(dashboard_repo)

        # Live dispatch tests — gated by EVO_LIVE_TEST_CLAUDE=1.
        # Real claude -p / codex exec subprocesses, real LLM cost.
        if _os.environ.get("EVO_LIVE_TEST_CLAUDE") == "1":
            if _shutil.which(_os.environ.get("EVO_CLAUDE_BIN", "claude")) is None:
                print("dispatch live: skipped (claude not on PATH)")
            else:
                print("dispatch live: starting (real LLM calls; ~$0.30-$1 cost)")
                for fn in (
                    test_dispatch_ensure_explorer_spawns_and_persists,
                    test_dispatch_ensure_explorer_reuses_within_ttl,
                    test_dispatch_ensure_explorer_rebuilds_when_skill_changes,
                    test_dispatch_orchestrator_step01_claude,
                    test_dispatch_orchestrator_step01_codex,
                ):
                    sub = temp_root / fn.__name__
                    sub.mkdir()
                    init_repo(sub)
                    # dispatch tests need at least one commit (current_commit
                    # is called on the worktree); init_repo doesn't commit by
                    # default.
                    run(["git", "commit", "--allow-empty", "-m", "initial"], cwd=sub)
                    try:
                        fn(sub)
                    finally:
                        _shutdown_dashboard(sub)
        else:
            print("dispatch live: skipped (set EVO_LIVE_TEST_CLAUDE=1 to enable)")
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    print("E2E OK")


if __name__ == "__main__":
    main()
