from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .core import (
    append_annotation,
    append_infra_event,
    append_note,
    ascii_tree,
    atomic_write_json,
    compare_scores,
    config_path,
    current_branch,
    delete_discarded_experiment,
    experiment_log_path,
    experiment_result_path,
    experiments_dir_for,
    fill_command_template,
    frontier_nodes,
    graph_path,
    init_workspace,
    load_annotations,
    load_config,
    load_graph,
    lock_file_for,
    mark_comparison_blocked,
    maybe_commit_worktree,
    node_target_path,
    notes_path,
    parse_score,
    project_path,
    relative_target,
    repo_root,
    reset_runtime_state,
    save_config,
    update_node,
    utc_now,
    worktrees_path,
    workspace_path,
    allocate_experiment,
    remove_worktree_only,
    render_git_diff,
)
from .locking import advisory_lock
from .scratchpad import write_scratchpad


def _require_workspace(root: Path) -> tuple[dict, dict]:
    config = load_config(root)
    if not config:
        raise RuntimeError("workspace is not initialized; run `uv run evo init ...` first")
    return config, load_graph(root)


def _read_node(root: Path, exp_id: str) -> dict:
    graph = load_graph(root)
    try:
        return graph["nodes"][exp_id]
    except KeyError as exc:
        raise RuntimeError(f"unknown experiment: {exp_id}") from exc


def _resolve_parent_score(graph: dict, parent_id: str) -> float | None:
    if parent_id == "root":
        return None
    parent = graph["nodes"][parent_id]
    return parent.get("score")


def _update_graph_and_write(root: Path, graph: dict) -> None:
    with advisory_lock(lock_file_for(graph_path(root))):
        atomic_write_json(graph_path(root), graph)


def cmd_init(args: argparse.Namespace) -> int:
    root = repo_root()
    if args.metric not in {"max", "min"}:
        raise RuntimeError("--metric must be `max` or `min`")
    init_workspace(root, target=args.target, benchmark=args.benchmark, metric=args.metric, gate=args.gate)
    write_scratchpad(root)
    print(f"Initialized evo workspace at {workspace_path(root)}")
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    root = repo_root()
    config, graph = _require_workspace(root)
    if args.parent not in graph["nodes"]:
        raise RuntimeError(f"unknown parent: {args.parent}")
    node = allocate_experiment(root, parent_id=args.parent, hypothesis=args.message)
    target = node_target_path(root, config, node)
    print(json.dumps({"id": node["id"], "worktree": node["worktree"], "target": str(target)}, indent=2))
    return 0


def _run_command(command: str, cwd: Path, env: dict[str, str], stdout_path: Path, stderr_path: Path, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        shell=True,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if stdout_path == stderr_path:
        combined = (result.stdout or "")
        if result.stderr:
            if combined and not combined.endswith("\n"):
                combined += "\n"
            combined += result.stderr
        stdout_path.write_text(combined, encoding="utf-8")
    else:
        stdout_path.write_text(result.stdout or "", encoding="utf-8")
        stderr_path.write_text(result.stderr or "", encoding="utf-8")
    return result


def _finalize_result(root: Path, exp_id: str, node: dict, score: float | None, status: str, extra: dict | None = None) -> None:
    payload = {
        "experiment_id": exp_id,
        "score": score,
        "status": status,
        "timestamp": utc_now(),
        "eval_epoch": node.get("eval_epoch"),
    }
    if extra:
        payload.update(extra)
    atomic_write_json(experiment_result_path(root, exp_id), payload)


def _block_if_epoch_requires_baseline(root: Path, parent_id: str, no_compare: bool) -> None:
    if no_compare:
        return
    config = load_config(root)
    if config.get("comparison_blocked") and parent_id != "root":
        raise RuntimeError("comparison is blocked for the current eval epoch until a new root baseline is committed")


def cmd_run(args: argparse.Namespace) -> int:
    root = repo_root()
    config, graph = _require_workspace(root)
    node = _read_node(root, args.exp_id)
    _block_if_epoch_requires_baseline(root, node["parent"], no_compare=False)

    def _mark_active(current_node: dict, _graph: dict) -> None:
        current_node["status"] = "active"

    update_node(root, args.exp_id, _mark_active)

    worktree = Path(node["worktree"])
    target = node_target_path(root, config, node)
    exp_dir = experiments_dir_for(root, args.exp_id)
    traces_dir = exp_dir / "traces"
    traces_dir.mkdir(parents=True, exist_ok=True)
    benchmark_log = experiment_log_path(root, args.exp_id, "benchmark.log")
    benchmark_err = experiment_log_path(root, args.exp_id, "benchmark_err.log")
    gate_log = experiment_log_path(root, args.exp_id, "gate.log")
    metric = config["metric"]
    parent_score = _resolve_parent_score(graph, node["parent"])

    benchmark_cmd = fill_command_template(config["benchmark"], target=target, worktree=worktree)
    env = os.environ.copy()
    env["EVO_TRACES_DIR"] = str(traces_dir)

    try:
        try:
            bench = _run_command(benchmark_cmd, cwd=worktree, env=env, stdout_path=benchmark_log, stderr_path=benchmark_err, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError("benchmark_timeout")

        if bench.returncode != 0:
            raise RuntimeError(f"benchmark_exit_{bench.returncode}")

        score, parsed = parse_score(bench.stdout)

        parent_ref = current_branch(root) if node["parent"] == "root" else _read_node(root, node["parent"])["branch"]
        diff_text = render_git_diff(root, parent_ref, worktree, relative_target(config))
        experiment_log_path(root, args.exp_id, "diff.patch").write_text(diff_text, encoding="utf-8")

        gate_passed = True
        if config.get("gate"):
            gate_cmd = fill_command_template(config["gate"], target=target, worktree=worktree)
            try:
                gate = _run_command(gate_cmd, cwd=worktree, env=env, stdout_path=gate_log, stderr_path=gate_log, timeout=args.timeout)
            except subprocess.TimeoutExpired:
                raise RuntimeError("gate_timeout")
            gate_passed = gate.returncode == 0

        keep = compare_scores(metric, score, parent_score) and gate_passed
        if keep:
            commit = maybe_commit_worktree(node, node.get("hypothesis", "experiment"))

            def _mark_committed(current_node: dict, _graph: dict) -> None:
                current_node["status"] = "committed"
                current_node["score"] = score
                current_node["commit"] = commit
                current_node["benchmark_result"] = parsed
                current_node["gate_result"] = gate_passed

            update_node(root, args.exp_id, _mark_committed)
            if config.get("comparison_blocked") and node["parent"] == "root":
                mark_comparison_blocked(root, False)
            _finalize_result(root, args.exp_id, node, score, "committed", {"commit": commit})
            write_scratchpad(root)
            delta = "" if parent_score is None else f" ({'+' if metric == 'max' else ''}{score - parent_score:.4f} vs parent)"
            print(f"COMMITTED {args.exp_id} {score}{delta}")
            return 0

        def _mark_discarded(current_node: dict, _graph: dict) -> None:
            current_node["status"] = "discarded"
            current_node["score"] = score
            current_node["benchmark_result"] = parsed
            current_node["gate_result"] = gate_passed

        update_node(root, args.exp_id, _mark_discarded)
        _finalize_result(root, args.exp_id, node, score, "discarded")
        delete_discarded_experiment(root, node)
        write_scratchpad(root)
        print(f"DISCARDED {args.exp_id} {score}")
        return 0
    except Exception as exc:  # noqa: BLE001
        def _mark_failed(current_node: dict, _graph: dict) -> None:
            current_node["status"] = "failed"

        update_node(root, args.exp_id, _mark_failed)
        _finalize_result(root, args.exp_id, node, None, "failed", {"error": str(exc)})
        write_scratchpad(root)
        print(f"FAILED {args.exp_id} {exc}")
        return 1


def _record_done_result(root: Path, args: argparse.Namespace) -> int:
    config, graph = _require_workspace(root)
    node = _read_node(root, args.exp_id)
    if args.traces:
        traces_dir = experiments_dir_for(root, args.exp_id) / "traces"
        traces_dir.mkdir(parents=True, exist_ok=True)
        source = Path(args.traces)
        if source.is_dir():
            for path in source.iterdir():
                if path.is_file():
                    shutil.copy2(path, traces_dir / path.name)
    if args.no_compare:
        def _mark_failed(current_node: dict, _graph: dict) -> None:
            current_node["status"] = "failed"
            current_node["score"] = args.score
        update_node(root, args.exp_id, _mark_failed)
        _finalize_result(root, args.exp_id, node, args.score, "failed", {"recorded_only": True})
        write_scratchpad(root)
        print(f"RECORDED {args.exp_id} score={args.score} (no compare)")
        return 0

    _block_if_epoch_requires_baseline(root, node["parent"], no_compare=False)
    parent_score = _resolve_parent_score(graph, node["parent"])
    metric = config["metric"]
    keep = compare_scores(metric, args.score, parent_score)
    if config.get("comparison_blocked") and node["parent"] == "root":
        mark_comparison_blocked(root, False)
    status = "committed" if keep else "discarded"

    def _mark(current_node: dict, _graph: dict) -> None:
        current_node["status"] = status
        current_node["score"] = args.score

    update_node(root, args.exp_id, _mark)
    _finalize_result(root, args.exp_id, node, args.score, status, {"recorded_only": True})
    if status == "discarded":
        delete_discarded_experiment(root, node)
    write_scratchpad(root)
    print(f"{status.upper()} {args.exp_id} {args.score}")
    return 0


def cmd_done(args: argparse.Namespace) -> int:
    return _record_done_result(repo_root(), args)


def cmd_discard(args: argparse.Namespace) -> int:
    root = repo_root()
    node = _read_node(root, args.exp_id)

    def _mark(current_node: dict, _graph: dict) -> None:
        current_node["status"] = "discarded"
        current_node["discard_reason"] = args.reason

    update_node(root, args.exp_id, _mark)
    _finalize_result(root, args.exp_id, node, node.get("score"), "discarded", {"reason": args.reason})
    delete_discarded_experiment(root, node)
    write_scratchpad(root)
    print(f"DISCARDED {args.exp_id}: {args.reason}")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    root = repo_root()

    def _mark(current_node: dict, _graph: dict) -> None:
        if current_node.get("status") != "committed":
            raise RuntimeError("only committed nodes can be pruned")
        current_node["status"] = "pruned"
        current_node["pruned_reason"] = args.reason

    update_node(root, args.exp_id, _mark)
    write_scratchpad(root)
    print(f"PRUNED {args.exp_id}: {args.reason}")
    return 0


def cmd_gc(args: argparse.Namespace) -> int:
    root = repo_root()
    graph = load_graph(root)
    removed = []
    for node in graph["nodes"].values():
        if node["id"] == "root":
            continue
        if node.get("status") not in {"committed", "failed", "pruned"}:
            continue
        children = [graph["nodes"][cid] for cid in node.get("children", []) if cid in graph["nodes"]]
        if any(child.get("status") == "active" for child in children):
            continue
        worktree = Path(node["worktree"])
        if worktree.exists():
            remove_worktree_only(root, node)
            removed.append(node["id"])
    print(json.dumps({"removed": removed}, indent=2))
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    if not args.yes:
        raise RuntimeError("reset is destructive; re-run with --yes")
    root = repo_root()
    reset_runtime_state(root)
    print("Reset evo runtime state")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    root = repo_root()
    config, graph = _require_workspace(root)
    metric = config["metric"]
    nodes = [node for node in graph["nodes"].values() if node["id"] != "root"]
    committed = [node for node in nodes if node.get("status") == "committed"]
    best = None
    if committed:
        scores = [float(node["score"]) for node in committed if node.get("score") is not None]
        best = max(scores) if metric == "max" else min(scores)
    print(
        f"metric={metric} epoch={config.get('current_eval_epoch', 1)} "
        f"experiments={len(nodes)} committed={sum(1 for n in nodes if n.get('status') == 'committed')} "
        f"discarded={sum(1 for n in nodes if n.get('status') == 'discarded')} "
        f"failed={sum(1 for n in nodes if n.get('status') == 'failed')} "
        f"active={sum(1 for n in nodes if n.get('status') == 'active')} best={best}"
    )
    return 0


def cmd_tree(args: argparse.Namespace) -> int:
    root = repo_root()
    config, graph = _require_workspace(root)
    print(ascii_tree(graph, config["metric"]))
    return 0


def cmd_frontier(args: argparse.Namespace) -> int:
    root = repo_root()
    _config, graph = _require_workspace(root)
    nodes = [
        {
            "id": node["id"],
            "score": node.get("score"),
            "epoch": node.get("eval_epoch"),
            "hypothesis": node.get("hypothesis"),
        }
        for node in frontier_nodes(graph)
    ]
    print(json.dumps(nodes, indent=2))
    return 0


def cmd_scratchpad(args: argparse.Namespace) -> int:
    root = repo_root()
    print(write_scratchpad(root))
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    root = repo_root()
    if args.filename:
        path = experiments_dir_for(root, args.exp_id) / args.filename
        print(path.read_text(encoding="utf-8"))
        return 0
    print(json.dumps(_read_node(root, args.exp_id), indent=2))
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    root = repo_root()
    target = experiments_dir_for(root, args.exp_id) / "diff.patch"
    print(target.read_text(encoding="utf-8") if target.exists() else "")
    return 0


def cmd_traces(args: argparse.Namespace) -> int:
    root = repo_root()
    traces_dir = experiments_dir_for(root, args.exp_id) / "traces"
    if args.task:
        path = traces_dir / f"task_{args.task}.json"
        print(path.read_text(encoding="utf-8"))
        return 0
    payload = {}
    for path in sorted(traces_dir.glob("*.json")):
        payload[path.name] = json.loads(path.read_text(encoding="utf-8"))
    print(json.dumps(payload, indent=2))
    return 0


def cmd_annotate(args: argparse.Namespace) -> int:
    root = repo_root()
    entry = append_annotation(root, args.exp_id, args.task, args.analysis)
    write_scratchpad(root)
    print(json.dumps(entry, indent=2))
    return 0


def cmd_annotations(args: argparse.Namespace) -> int:
    root = repo_root()
    entries = load_annotations(root).get("annotations", [])
    if args.task:
        entries = [entry for entry in entries if entry.get("task_id") == args.task]
    if args.exp:
        entries = [entry for entry in entries if entry.get("experiment_id") == args.exp]
    print(json.dumps(entries, indent=2))
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    root = repo_root()
    payload = sys.stdin.read()
    path = experiments_dir_for(root, args.exp_id) / args.filename
    path.write_text(payload, encoding="utf-8")
    print(str(path))
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    root = repo_root()

    def _mutate(current_node: dict, _graph: dict) -> None:
        current_node.setdefault("tags", [])
        current_node.setdefault("notes", [])
        if args.tag:
            if args.tag not in current_node["tags"]:
                current_node["tags"].append(args.tag)
        if args.note:
            current_node["notes"].append({"text": args.note, "timestamp": utc_now()})

    node = update_node(root, args.exp_id, _mutate)
    write_scratchpad(root)
    print(json.dumps(node, indent=2))
    return 0


def cmd_infra(args: argparse.Namespace) -> int:
    root = repo_root()
    event = append_infra_event(root, args.message, args.breaking)
    if args.breaking:
        config = load_config(root)
        config["current_eval_epoch"] = int(config.get("current_eval_epoch", 1)) + 1
        config["comparison_blocked"] = True
        save_config(root, config)
    write_scratchpad(root)
    print(json.dumps(event, indent=2))
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    from .dashboard import create_app

    app = create_app(repo_root())
    app.run(host="127.0.0.1", port=args.port, debug=False)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evo")
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init")
    init_p.add_argument("--target", required=True)
    init_p.add_argument("--benchmark", required=True)
    init_p.add_argument("--metric", required=True, choices=["max", "min"])
    init_p.add_argument("--gate")
    init_p.set_defaults(func=cmd_init)

    new_p = sub.add_parser("new")
    new_p.add_argument("--parent", required=True)
    new_p.add_argument("-m", "--message", required=True)
    new_p.set_defaults(func=cmd_new)

    run_p = sub.add_parser("run")
    run_p.add_argument("exp_id")
    run_p.add_argument("--timeout", type=int, default=1800)
    run_p.set_defaults(func=cmd_run)

    done_p = sub.add_parser("done")
    done_p.add_argument("exp_id")
    done_p.add_argument("--score", type=float, required=True)
    done_p.add_argument("--traces")
    done_p.add_argument("--no-compare", action="store_true")
    done_p.set_defaults(func=cmd_done)

    discard_p = sub.add_parser("discard")
    discard_p.add_argument("exp_id")
    discard_p.add_argument("--reason", required=True)
    discard_p.set_defaults(func=cmd_discard)

    prune_p = sub.add_parser("prune")
    prune_p.add_argument("exp_id")
    prune_p.add_argument("--reason", required=True)
    prune_p.set_defaults(func=cmd_prune)

    gc_p = sub.add_parser("gc")
    gc_p.set_defaults(func=cmd_gc)

    reset_p = sub.add_parser("reset")
    reset_p.add_argument("--yes", action="store_true")
    reset_p.set_defaults(func=cmd_reset)

    status_p = sub.add_parser("status")
    status_p.set_defaults(func=cmd_status)

    tree_p = sub.add_parser("tree")
    tree_p.set_defaults(func=cmd_tree)

    frontier_p = sub.add_parser("frontier")
    frontier_p.set_defaults(func=cmd_frontier)

    scratchpad_p = sub.add_parser("scratchpad")
    scratchpad_p.set_defaults(func=cmd_scratchpad)

    get_p = sub.add_parser("get")
    get_p.add_argument("exp_id")
    get_p.add_argument("filename", nargs="?")
    get_p.set_defaults(func=cmd_get)

    diff_p = sub.add_parser("diff")
    diff_p.add_argument("exp_id")
    diff_p.set_defaults(func=cmd_diff)

    traces_p = sub.add_parser("traces")
    traces_p.add_argument("exp_id")
    traces_p.add_argument("task", nargs="?")
    traces_p.set_defaults(func=cmd_traces)

    annotate_p = sub.add_parser("annotate")
    annotate_p.add_argument("exp_id")
    annotate_p.add_argument("task", nargs="?")
    annotate_p.add_argument("analysis")
    annotate_p.set_defaults(func=cmd_annotate)

    annotations_p = sub.add_parser("annotations")
    annotations_p.add_argument("--task")
    annotations_p.add_argument("--exp")
    annotations_p.set_defaults(func=cmd_annotations)

    log_p = sub.add_parser("log")
    log_p.add_argument("exp_id")
    log_p.add_argument("filename")
    log_p.set_defaults(func=cmd_log)

    set_p = sub.add_parser("set")
    set_p.add_argument("exp_id")
    set_p.add_argument("--tag")
    set_p.add_argument("--note")
    set_p.set_defaults(func=cmd_set)

    infra_p = sub.add_parser("infra")
    infra_p.add_argument("-m", "--message", required=True)
    infra_p.add_argument("--breaking", action="store_true")
    infra_p.set_defaults(func=cmd_infra)

    dashboard_p = sub.add_parser("dashboard")
    dashboard_p.add_argument("--port", type=int, default=8080)
    dashboard_p.set_defaults(func=cmd_dashboard)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        rc = args.func(args)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    sys.exit(rc)


if __name__ == "__main__":
    main()
