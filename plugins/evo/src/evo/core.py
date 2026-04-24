from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .locking import advisory_lock

WORKSPACE_NAME = ".evo"
GRAPH_FILE = "graph.json"
CONFIG_FILE = "config.json"
ANNOTATIONS_FILE = "annotations.json"
INFRA_FILE = "infra_log.json"
META_FILE = "meta.json"
PROJECT_FILE = "project.md"
SCRATCHPAD_FILE = "scratchpad.md"
NOTES_FILE = "notes.md"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def repo_root(cwd: Path | None = None) -> Path:
    base = cwd or Path.cwd()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=base,
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip())


def evo_dir(root: Path) -> Path:
    """Top-level .evo/ container."""
    return root / WORKSPACE_NAME


def _meta_path(root: Path) -> Path:
    return evo_dir(root) / META_FILE


def _load_meta(root: Path) -> dict[str, Any]:
    path = _meta_path(root)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"active": None, "next_run": 0}


def _save_meta(root: Path, meta: dict[str, Any]) -> None:
    path = _meta_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, meta)


def workspace_path(root: Path) -> Path:
    """Path to the active run directory (e.g. .evo/run_0000/)."""
    meta = _load_meta(root)
    active = meta.get("active")
    if active:
        return evo_dir(root) / active
    # Legacy fallback: if no meta.json but .evo/config.json exists, treat .evo/ itself as workspace
    if (evo_dir(root) / CONFIG_FILE).exists():
        return evo_dir(root)
    return evo_dir(root)


def worktrees_path(root: Path) -> Path:
    return workspace_path(root) / "worktrees"


def experiments_path(root: Path) -> Path:
    return workspace_path(root) / "experiments"


def config_path(root: Path) -> Path:
    return workspace_path(root) / CONFIG_FILE


def graph_path(root: Path) -> Path:
    return workspace_path(root) / GRAPH_FILE


def annotations_path(root: Path) -> Path:
    return workspace_path(root) / ANNOTATIONS_FILE


def infra_path(root: Path) -> Path:
    return workspace_path(root) / INFRA_FILE


def project_path(root: Path) -> Path:
    # Top-level (not per-run) so it resolves without the active run ID.
    return evo_dir(root) / PROJECT_FILE


def scratchpad_path(root: Path) -> Path:
    return workspace_path(root) / SCRATCHPAD_FILE


def notes_path(root: Path) -> Path:
    return workspace_path(root) / NOTES_FILE


def lock_file_for(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


def ensure_workspace_dirs(root: Path) -> None:
    workspace = workspace_path(root)
    workspace.mkdir(parents=True, exist_ok=True)
    experiments_path(root).mkdir(parents=True, exist_ok=True)
    worktrees_path(root).mkdir(parents=True, exist_ok=True)


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=path.parent, encoding="utf-8") as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


DEFAULT_MAX_ATTEMPTS = 3


def default_config(root: Path, target: str, benchmark: str, metric: str, gate: str | None) -> dict[str, Any]:
    return {
        "repo_root": str(root),
        "workspace_dir": WORKSPACE_NAME,
        "worktrees_dir": "worktrees",
        "target": target,
        "benchmark": benchmark,
        "gate": gate,
        "metric": metric,
        "current_eval_epoch": 1,
        "comparison_blocked": False,
        "max_attempts": DEFAULT_MAX_ATTEMPTS,
        "initialized_at": utc_now(),
    }


def default_graph() -> dict[str, Any]:
    return {
        "root": "root",
        "next_id": 0,
        "nodes": {
            "root": {
                "id": "root",
                "parent": None,
                "children": [],
                "status": "root",
                "hypothesis": "synthetic root",
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "eval_epoch": None,
                "score": None,
                "branch": None,
                "worktree": None,
                "commit": None,
                "pruned_reason": None,
                "gates": [],
            }
        },
    }


def load_config(root: Path) -> dict[str, Any]:
    return load_json(config_path(root), {})


def save_config(root: Path, config: dict[str, Any]) -> None:
    path = config_path(root)
    with advisory_lock(lock_file_for(path)):
        atomic_write_json(path, config)


def load_graph(root: Path) -> dict[str, Any]:
    return load_json(graph_path(root), default_graph())


def save_graph(root: Path, graph: dict[str, Any]) -> None:
    path = graph_path(root)
    with advisory_lock(lock_file_for(path)):
        atomic_write_json(path, graph)


def _allocate_run(root: Path) -> str:
    """Allocate a new run ID and set it as active."""
    meta = _load_meta(root)
    run_id = f"run_{meta.get('next_run', 0):04d}"
    meta["next_run"] = meta.get("next_run", 0) + 1
    meta["active"] = run_id
    _save_meta(root, meta)
    return run_id


def list_runs(root: Path) -> list[dict[str, Any]]:
    """List all runs in the workspace."""
    meta = _load_meta(root)
    active = meta.get("active")
    runs = []
    evo = evo_dir(root)
    if not evo.exists():
        return runs
    for d in sorted(evo.iterdir()):
        if d.is_dir() and d.name.startswith("run_"):
            cfg_path = d / CONFIG_FILE
            cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
            runs.append({
                "id": d.name,
                "active": d.name == active,
                "target": cfg.get("target", ""),
                "created": cfg.get("created_at", ""),
            })
    return runs


def init_workspace(root: Path, target: str, benchmark: str, metric: str, gate: str | None) -> str:
    run_id = _allocate_run(root)
    ensure_workspace_dirs(root)
    config = default_config(root, target, benchmark, metric, gate)
    atomic_write_json(config_path(root), config)
    atomic_write_json(graph_path(root), default_graph())
    atomic_write_json(annotations_path(root), {"annotations": []})
    atomic_write_json(infra_path(root), {"events": []})
    if not project_path(root).exists():
        atomic_write_text(project_path(root), "# Project Understanding\n\n")
    if not notes_path(root).exists():
        atomic_write_text(notes_path(root), "# Notes\n\n")
    if not scratchpad_path(root).exists():
        atomic_write_text(scratchpad_path(root), "# Scratchpad\n\n")
    return run_id


def current_branch(root: Path) -> str:
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    branch = result.stdout.strip()
    if not branch:
        raise RuntimeError("Current repository is in detached HEAD state")
    return branch


def current_commit(path: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def git_branch_exists(root: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=root,
        check=False,
    )
    return result.returncode == 0


def relative_target(config: dict[str, Any]) -> str:
    return config["target"]


def node_target_path(root: Path, config: dict[str, Any], node: dict[str, Any]) -> Path:
    return Path(node["worktree"]) / relative_target(config)


def experiments_dir_for(root: Path, exp_id: str) -> Path:
    return experiments_path(root) / exp_id


def experiment_result_path(root: Path, exp_id: str) -> Path:
    # Overwritten on every evo run; reflects only the latest attempt.
    return experiments_dir_for(root, exp_id) / "result.json"


def attempt_dir(root: Path, exp_id: str, attempt: int) -> Path:
    return experiments_dir_for(root, exp_id) / "attempts" / f"{attempt:03d}"


def attempt_log_path(root: Path, exp_id: str, attempt: int, filename: str) -> Path:
    return attempt_dir(root, exp_id, attempt) / filename


def attempt_traces_dir(root: Path, exp_id: str, attempt: int) -> Path:
    return attempt_dir(root, exp_id, attempt) / "traces"


def attempt_outcome_path(root: Path, exp_id: str, attempt: int) -> Path:
    return attempt_dir(root, exp_id, attempt) / "outcome.json"


def experiment_log_path(root: Path, exp_id: str, filename: str) -> Path:
    return experiments_dir_for(root, exp_id) / filename


def load_annotations(root: Path) -> dict[str, Any]:
    return load_json(annotations_path(root), {"annotations": []})


def append_annotation(root: Path, exp_id: str, task_id: str | None, analysis: str) -> dict[str, Any]:
    path = annotations_path(root)
    with advisory_lock(lock_file_for(path)):
        data = load_json(path, {"annotations": []})
        entry = {
            "experiment_id": exp_id,
            "task_id": task_id,
            "analysis": analysis,
            "timestamp": utc_now(),
        }
        data.setdefault("annotations", []).append(entry)
        atomic_write_json(path, data)
        return entry


def append_infra_event(root: Path, message: str, breaking: bool) -> dict[str, Any]:
    path = infra_path(root)
    with advisory_lock(lock_file_for(path)):
        data = load_json(path, {"events": []})
        event = {
            "message": message,
            "breaking": breaking,
            "timestamp": utc_now(),
        }
        data.setdefault("events", []).append(event)
        atomic_write_json(path, data)
        return event


def append_note(root: Path, content: str) -> None:
    path = notes_path(root)
    with advisory_lock(lock_file_for(path)):
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        existing += content
        if not existing.endswith("\n"):
            existing += "\n"
        atomic_write_text(path, existing)


def allocate_experiment(root: Path, parent_id: str, hypothesis: str) -> dict[str, Any]:
    gpath = graph_path(root)
    with advisory_lock(lock_file_for(gpath)):
        graph = load_json(gpath, default_graph())
        nodes = graph["nodes"]
        if parent_id not in nodes:
            raise KeyError(f"Unknown parent experiment: {parent_id}")
        next_id = graph.get("next_id", 0)
        exp_id = f"exp_{next_id:04d}"
        graph["next_id"] = next_id + 1

        meta = _load_meta(root)
        run_id = meta.get("active", "run")
        branch = f"evo/{run_id}/{exp_id}"
        worktree = worktrees_path(root) / exp_id
        parent = nodes[parent_id]
        start_point = current_branch(root) if parent_id == "root" else parent["branch"]
        if not start_point:
            raise RuntimeError(f"Parent {parent_id} does not have a branch to fork from")

        # A freshly allocated experiment ID should be collision-free. If a stale
        # branch or prunable worktree exists for that ID, clean it up here so a
        # partial prior run does not block new allocation.
        if worktree.exists():
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=root, check=False)
            shutil.rmtree(worktree, ignore_errors=True)
        subprocess.run(["git", "worktree", "prune"], cwd=root, check=False)
        if git_branch_exists(root, branch):
            subprocess.run(["git", "branch", "-D", branch], cwd=root, check=False)

        subprocess.run(
            ["git", "worktree", "add", "-b", branch, str(worktree), start_point],
            cwd=root,
            check=True,
        )

        # Propagate project.md into the worktree so it's accessible even
        # though it's not committed to git.
        project_src = project_path(root)
        if project_src.exists():
            worktree_evo = worktree / WORKSPACE_NAME
            worktree_evo.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(project_src), str(worktree_evo / PROJECT_FILE))

        node = {
            "id": exp_id,
            "parent": parent_id,
            "children": [],
            "status": "pending",
            "hypothesis": hypothesis,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "eval_epoch": load_config(root).get("current_eval_epoch", 1),
            "score": None,
            "branch": branch,
            "worktree": str(worktree),
            "commit": current_commit(worktree),
            "pruned_reason": None,
            "benchmark_result": None,
            "gate_result": None,
            "gates": [],
            "current_attempt": 0,
        }
        nodes[exp_id] = node
        nodes[parent_id].setdefault("children", []).append(exp_id)
        atomic_write_json(gpath, graph)
        experiments_dir_for(root, exp_id).mkdir(parents=True, exist_ok=True)
        return node


def remove_worktree_only(root: Path, node: dict[str, Any]) -> None:
    worktree = Path(node["worktree"])
    if worktree.exists():
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=root, check=False)
    if worktree.exists():
        shutil.rmtree(worktree, ignore_errors=True)
    subprocess.run(["git", "worktree", "prune"], cwd=root, check=False)


def delete_discarded_experiment(root: Path, node: dict[str, Any]) -> None:
    remove_worktree_only(root, node)
    branch = node["branch"]
    subprocess.run(["git", "branch", "-D", branch], cwd=root, check=False)


def reset_runtime_state(root: Path) -> None:
    """Remove the active run's worktrees, branches, and directory."""
    meta = _load_meta(root)
    run_id = meta.get("active")
    workspace = workspace_path(root)
    wt_dir = worktrees_path(root)
    subprocess.run(["git", "worktree", "prune"], cwd=root, check=False)
    if wt_dir.exists():
        for path in sorted(wt_dir.iterdir()):
            if path.is_dir():
                subprocess.run(["git", "worktree", "remove", "--force", str(path)], cwd=root, check=False)
                shutil.rmtree(path, ignore_errors=True)
    subprocess.run(["git", "worktree", "prune"], cwd=root, check=False)
    # Only delete branches for this run (evo/<run_id>/*)
    branch_prefix = f"refs/heads/evo/{run_id}/" if run_id else "refs/heads/evo/"
    result = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", branch_prefix],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    for branch in [line.strip() for line in result.stdout.splitlines() if line.strip()]:
        subprocess.run(["git", "branch", "-D", branch], cwd=root, check=False)
    shutil.rmtree(workspace, ignore_errors=True)


def git_status_porcelain(path: Path) -> str:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def maybe_commit_worktree(node: dict[str, Any], hypothesis: str) -> str | None:
    worktree = Path(node["worktree"])
    if not git_status_porcelain(worktree).strip():
        return current_commit(worktree)
    subprocess.run(["git", "add", "-A"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "commit", "-m", f"evo: {node['id']} {hypothesis}"],
        cwd=worktree,
        check=True,
    )
    return current_commit(worktree)


def render_git_diff(root: Path, parent_ref: str, worktree: Path, relative_path: str) -> str:
    result = subprocess.run(
        ["git", "diff", parent_ref, "--", relative_path],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def fill_command_template(template: str, *, target: Path, worktree: Path) -> str:
    # Only replace the two documented placeholders so benchmark commands can
    # freely contain JSON or Python dict literals without escaping braces.
    return template.replace("{target}", str(target)).replace("{worktree}", str(worktree))


def parse_score(stdout: str, traces_dir: str | None = None) -> tuple[float, dict[str, Any] | None]:
    # First try to read from the result file in traces directory (more reliable)
    if traces_dir:
        result_file = Path(traces_dir) / "result.json"
        if result_file.exists():
            try:
                content = result_file.read_text(encoding="utf-8").strip()
                if content:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict) and "score" in parsed:
                        return float(parsed["score"]), parsed
            except (json.JSONDecodeError, OSError):
                pass  # Fall back to parsing stdout
    
    # Fall back to original stdout parsing for backward compatibility
    stripped = stdout.strip()
    if not stripped:
        raise ValueError("Benchmark output was empty")
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict) and "score" in parsed:
            return float(parsed["score"]), parsed
        if isinstance(parsed, (int, float)):
            return float(parsed), None
    except json.JSONDecodeError:
        pass

    for line in reversed([line.strip() for line in stripped.splitlines() if line.strip()]):
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict) and "score" in parsed:
                return float(parsed["score"]), parsed
            if isinstance(parsed, (int, float)):
                return float(parsed), None
        except json.JSONDecodeError:
            match = re.search(r"\bscore\b\s*[:=]\s*(-?\d+(?:\.\d+)?)", line, re.IGNORECASE)
            if match:
                return float(match.group(1)), None
            plain = re.fullmatch(r"-?\d+(?:\.\d+)?", line)
            if plain:
                return float(plain.group(0)), None
    raise ValueError("Could not parse benchmark score from output")


def compare_scores(metric: str, candidate: float, parent: float | None) -> bool:
    if parent is None:
        return True
    if metric == "max":
        return candidate >= parent
    if metric == "min":
        return candidate <= parent
    raise ValueError(f"Unknown metric: {metric}")


def best_committed_score(graph: dict[str, Any], metric: str, epoch: int | None = None) -> float | None:
    scores: list[float] = []
    for node in graph["nodes"].values():
        if node.get("status") != "committed":
            continue
        if node.get("score") is None:
            continue
        if epoch is not None and node.get("eval_epoch") != epoch:
            continue
        scores.append(float(node["score"]))
    if not scores:
        return None
    return max(scores) if metric == "max" else min(scores)


def best_committed_node(graph: dict[str, Any], metric: str) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for node in graph["nodes"].values():
        if node.get("status") != "committed" or node.get("score") is None:
            continue
        if best is None:
            best = node
        elif metric == "max" and float(node["score"]) > float(best["score"]):
            best = node
        elif metric == "min" and float(node["score"]) < float(best["score"]):
            best = node
    return best


def path_to_node(graph: dict[str, Any], node_id: str) -> list[dict[str, Any]]:
    """Return the chain of nodes from root to the given node."""
    nodes = graph["nodes"]
    chain: list[dict[str, Any]] = []
    current: str | None = node_id
    while current is not None:
        chain.append(nodes[current])
        current = nodes[current].get("parent")
    chain.reverse()
    return chain


def frontier_nodes(graph: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = graph["nodes"]
    result = []
    for node in nodes.values():
        if node.get("status") != "committed":
            continue
        if node.get("pruned_reason"):
            continue
        children = [nodes[cid] for cid in node.get("children", []) if cid in nodes]
        if any(child.get("status") in {"committed", "active"} for child in children):
            continue
        result.append(node)
    return sorted(result, key=lambda item: item["id"])


def ascii_tree(graph: dict[str, Any], metric: str) -> str:
    nodes = graph["nodes"]

    def label(node: dict[str, Any]) -> str:
        parts = [node["id"], node.get("status", "unknown")]
        if node.get("score") is not None:
            parts.append(f"score={node['score']}")
        if node.get("eval_epoch") is not None:
            parts.append(f"epoch={node['eval_epoch']}")
        if node.get("pruned_reason"):
            parts.append("pruned")
        if node.get("gates"):
            parts.append(f"gates={len(node['gates'])}")
        if node.get("hypothesis") and node["id"] != "root":
            parts.append(node["hypothesis"])
        return " ".join(parts)

    lines: list[str] = []

    def walk(node_id: str, prefix: str = "", is_last: bool = True) -> None:
        node = nodes[node_id]
        if node_id == "root":
            lines.append(label(node))
        else:
            connector = "└── " if is_last else "├── "
            lines.append(prefix + connector + label(node))
        children = sorted(node.get("children", []))
        for index, child_id in enumerate(children):
            extension = "" if node_id == "root" else ("    " if is_last else "│   ")
            walk(child_id, prefix + extension, index == len(children) - 1)

    walk("root")
    return "\n".join(lines)


def update_node(root: Path, exp_id: str, mutator) -> dict[str, Any]:
    gpath = graph_path(root)
    with advisory_lock(lock_file_for(gpath)):
        graph = load_json(gpath, default_graph())
        node = graph["nodes"][exp_id]
        mutator(node, graph)
        node["updated_at"] = utc_now()
        atomic_write_json(gpath, graph)
        return node


def collect_gates_from_path(graph: dict[str, Any], node_id: str) -> list[dict[str, str]]:
    """Walk from root to node_id, collecting all gates. Returns deduplicated list."""
    chain = path_to_node(graph, node_id)
    seen_names: set[str] = set()
    gates: list[dict[str, str]] = []
    for node in chain:
        for gate in node.get("gates", []):
            if gate["name"] not in seen_names:
                seen_names.add(gate["name"])
                gates.append(gate)
    return gates


def add_gate(root: Path, exp_id: str, name: str, command: str) -> dict[str, str]:
    """Add a named gate to a node. Returns the gate entry."""
    gate_entry = {"name": name, "command": command, "added_at": utc_now()}

    def _add(current_node: dict, _graph: dict) -> None:
        existing = current_node.setdefault("gates", [])
        for g in existing:
            if g["name"] == name:
                raise ValueError(f"gate '{name}' already exists on {exp_id}")
        existing.append(gate_entry)

    update_node(root, exp_id, _add)
    return gate_entry


def remove_gate(root: Path, exp_id: str, name: str) -> None:
    """Remove a gate from a node by name."""

    def _remove(current_node: dict, _graph: dict) -> None:
        existing = current_node.get("gates", [])
        updated = [g for g in existing if g["name"] != name]
        if len(updated) == len(existing):
            raise ValueError(f"gate '{name}' not found on {exp_id}")
        current_node["gates"] = updated

    update_node(root, exp_id, _remove)


def mark_comparison_blocked(root: Path, blocked: bool) -> dict[str, Any]:
    path = config_path(root)
    with advisory_lock(lock_file_for(path)):
        config = load_json(path, {})
        config["comparison_blocked"] = blocked
        if blocked:
            config["comparison_blocked_since"] = utc_now()
        else:
            config.pop("comparison_blocked_since", None)
        atomic_write_json(path, config)
        return config
