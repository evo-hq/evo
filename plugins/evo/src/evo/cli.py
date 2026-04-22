from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import DISTRIBUTION_NAME, __version__
from .core import (
    add_gate,
    append_annotation,
    append_infra_event,
    append_note,
    ascii_tree,
    atomic_write_json,
    attempt_dir,
    attempt_log_path,
    attempt_outcome_path,
    attempt_traces_dir,
    collect_gates_from_path,
    compare_scores,
    config_path,
    current_branch,
    delete_discarded_experiment,
    evo_dir,
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
    path_to_node,
    project_path,
    relative_target,
    remove_gate,
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


def _pick_free_port(preferred: int, max_tries: int = 20) -> int:
    """Find a free TCP port on 127.0.0.1, starting from *preferred* and
    incrementing by 1 on collision. Raises if nothing free in *max_tries*."""
    import socket
    for offset in range(max_tries):
        port = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(
        f"no free port in range {preferred}..{preferred + max_tries - 1}"
    )


def _start_dashboard_background(root: Path, port: int = 8080) -> None:
    """Start the dashboard as a background process.

    Probes for a free port starting at *port* (auto-increments on collision),
    writes the actual port to .evo/dashboard.port, and prints a clickable URL.
    """
    pid_file = evo_dir(root) / "dashboard.pid"
    port_file = evo_dir(root) / "dashboard.port"

    # If already running, surface the existing URL instead of starting a second.
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            existing = port_file.read_text().strip() if port_file.exists() else str(port)
            print(f"Dashboard live: http://127.0.0.1:{existing} (pid {pid})")
            return
        except (OSError, ValueError):
            pid_file.unlink(missing_ok=True)

    actual_port = _pick_free_port(port)

    env = os.environ.copy()
    env["EVO_DASHBOARD_PORT"] = str(actual_port)

    proc = subprocess.Popen(
        [sys.executable, "-m", "evo.dashboard"],
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )
    pid_file.write_text(str(proc.pid))
    port_file.write_text(str(actual_port))
    note = "" if actual_port == port else f" (port {port} busy, bumped to {actual_port})"
    print(f"Dashboard live: http://127.0.0.1:{actual_port} (pid {proc.pid}){note}")


def cmd_init(args: argparse.Namespace) -> int:
    root = repo_root()
    if args.metric not in {"max", "min"}:
        raise RuntimeError("--metric must be `max` or `min`")
    run_id = init_workspace(root, target=args.target, benchmark=args.benchmark, metric=args.metric, gate=args.gate)
    if args.instrumentation_mode:
        meta_file = evo_dir(root) / "meta.json"
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        meta["instrumentation_mode"] = args.instrumentation_mode
        atomic_write_json(meta_file, meta)
    write_scratchpad(root)
    _start_dashboard_background(root, port=args.port)
    print(f"Initialized evo workspace {run_id} at {workspace_path(root)}")
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


def _write_attempt_outcome(
    root: Path,
    exp_id: str,
    attempt: int,
    outcome: str,
    *,
    node: dict,
    started_at: str,
    score: float | None = None,
    benchmark: dict | None = None,
    gates: list[dict] | None = None,
    error: str | None = None,
    commit: str | None = None,
    parent_score: float | None = None,
    metric: str | None = None,
) -> None:
    finished = utc_now()
    payload = {
        "experiment_id": exp_id,
        "attempt": attempt,
        "outcome": outcome,
        "hypothesis": node.get("hypothesis"),
        "parent_id": node.get("parent"),
        "parent_score": parent_score,
        "metric": metric,
        "score": score,
        "started_at": started_at,
        "finished_at": finished,
        "benchmark": benchmark,
        "gates": gates or [],
        "error": error,
        "commit": commit,
    }
    atomic_write_json(attempt_outcome_path(root, exp_id, attempt), payload)


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
    if node.get("status") not in (None, "pending", "active", "evaluated", "failed"):
        print(f"ERROR: {args.exp_id} has status '{node['status']}' -- cannot run again", file=sys.stderr)
        return 1
    _block_if_epoch_requires_baseline(root, node["parent"], no_compare=False)

    max_attempts = int(config.get("max_attempts", 3))
    evaluated_attempts = int(node.get("evaluated_attempts", 0))
    if evaluated_attempts >= max_attempts:
        print(
            f"ERROR: {args.exp_id} exhausted {evaluated_attempts}/{max_attempts} attempts. "
            f"Discard with `evo discard {args.exp_id} --reason \"...\"` or branch elsewhere.",
            file=sys.stderr,
        )
        return 1

    # Bumped even on failed runs so NNN subdirs never collide.
    attempt_n = int(node.get("current_attempt", 0)) + 1
    started_at = utc_now()

    def _mark_active(current_node: dict, _graph: dict) -> None:
        current_node["status"] = "active"
        current_node["current_attempt"] = attempt_n

    update_node(root, args.exp_id, _mark_active)

    worktree = Path(node["worktree"])
    target = node_target_path(root, config, node)
    exp_dir = experiments_dir_for(root, args.exp_id)
    a_dir = attempt_dir(root, args.exp_id, attempt_n)
    a_dir.mkdir(parents=True, exist_ok=True)
    traces_dir = attempt_traces_dir(root, args.exp_id, attempt_n)
    traces_dir.mkdir(parents=True, exist_ok=True)
    benchmark_log = a_dir / "benchmark.log"
    benchmark_err = a_dir / "benchmark_err.log"
    metric = config["metric"]
    parent_score = _resolve_parent_score(graph, node["parent"])

    benchmark_cmd = fill_command_template(config["benchmark"], target=target, worktree=worktree)
    env = os.environ.copy()
    env["EVO_TRACES_DIR"] = str(traces_dir)
    env["EVO_WORKTREE"] = str(worktree)
    env["EVO_EXPERIMENT_ID"] = args.exp_id
    env["EVO_ATTEMPT"] = str(attempt_n)

    # Captured before the benchmark runs so it survives crashes too.
    parent_ref = current_branch(root) if node["parent"] == "root" else _read_node(root, node["parent"])["branch"]
    diff_text = render_git_diff(root, parent_ref, worktree, relative_target(config))
    (a_dir / "diff.patch").write_text(diff_text, encoding="utf-8")

    gate_records: list[dict] = []
    benchmark_record: dict | None = None

    try:
        try:
            bench = _run_command(benchmark_cmd, cwd=root, env=env, stdout_path=benchmark_log, stderr_path=benchmark_err, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError("benchmark_timeout")

        if bench.returncode != 0:
            benchmark_record = {"command": benchmark_cmd, "returncode": bench.returncode, "result": None}
            raise RuntimeError(f"benchmark_exit_{bench.returncode}")

        score, parsed = parse_score(bench.stdout)
        benchmark_record = {"command": benchmark_cmd, "returncode": 0, "result": parsed}

        gate_passed = True
        gate_failures: list[str] = []

        # Collect inherited gates from the tree path (root -> parent)
        inherited_gates = collect_gates_from_path(graph, node["parent"])
        # Also include the legacy --gate from config as an implicit gate
        if config.get("gate"):
            inherited_gates.insert(0, {"name": "_init_gate", "command": config["gate"]})

        gate_origins: dict[str, str] = {}
        for chain_node in path_to_node(graph, node["parent"]):
            for g in chain_node.get("gates", []):
                gate_origins.setdefault(g["name"], chain_node["id"])

        for g in inherited_gates:
            gate_cmd = fill_command_template(g["command"], target=target, worktree=worktree)
            gate_log_file = a_dir / f"gate_{g['name']}.log"
            try:
                gate_result = _run_command(gate_cmd, cwd=root, env=env, stdout_path=gate_log_file, stderr_path=gate_log_file, timeout=args.timeout)
            except subprocess.TimeoutExpired:
                gate_records.append({
                    "name": g["name"],
                    "from": gate_origins.get(g["name"], "config"),
                    "command": gate_cmd,
                    "passed": False,
                    "returncode": None,
                    "error": "gate_timeout",
                })
                raise RuntimeError(f"gate_timeout:{g['name']}")
            passed = gate_result.returncode == 0
            gate_records.append({
                "name": g["name"],
                "from": gate_origins.get(g["name"], "config"),
                "command": gate_cmd,
                "passed": passed,
                "returncode": gate_result.returncode,
            })
            if not passed:
                gate_failures.append(g["name"])
                gate_passed = False

        if gate_failures:
            print(f"GATE_FAILED {' '.join(gate_failures)}")

        keep = compare_scores(metric, score, parent_score) and gate_passed
        if keep:
            commit = maybe_commit_worktree(node, node.get("hypothesis", "experiment"))

            def _mark_committed(current_node: dict, _graph: dict) -> None:
                current_node["status"] = "committed"
                current_node["score"] = score
                current_node["commit"] = commit
                current_node["benchmark_result"] = parsed
                current_node["gate_result"] = gate_passed
                current_node["gate_failures"] = gate_failures

            update_node(root, args.exp_id, _mark_committed)
            if config.get("comparison_blocked") and node["parent"] == "root":
                mark_comparison_blocked(root, False)
            _finalize_result(root, args.exp_id, node, score, "committed", {"commit": commit})
            _write_attempt_outcome(
                root, args.exp_id, attempt_n, "committed",
                node=node, started_at=started_at, score=score,
                benchmark=benchmark_record, gates=gate_records,
                commit=commit, parent_score=parent_score, metric=metric,
            )
            write_scratchpad(root)
            delta = "" if parent_score is None else f" ({'+' if metric == 'max' else ''}{score - parent_score:.4f} vs parent)"
            print(f"COMMITTED {args.exp_id} {score}{delta}")
            return 0

        def _mark_evaluated(current_node: dict, _graph: dict) -> None:
            current_node["status"] = "evaluated"
            current_node["score"] = score
            current_node["benchmark_result"] = parsed
            current_node["gate_result"] = gate_passed
            current_node["gate_failures"] = gate_failures
            current_node["evaluated_attempts"] = int(current_node.get("evaluated_attempts", 0)) + 1

        update_node(root, args.exp_id, _mark_evaluated)
        _finalize_result(root, args.exp_id, node, score, "evaluated")
        _write_attempt_outcome(
            root, args.exp_id, attempt_n, "evaluated",
            node=node, started_at=started_at, score=score,
            benchmark=benchmark_record, gates=gate_records,
            parent_score=parent_score, metric=metric,
        )
        write_scratchpad(root)
        remaining = max_attempts - (evaluated_attempts + 1)
        suffix = f" ({remaining} attempts remaining)" if remaining > 0 else " (no attempts remaining -- retry blocked)"
        reason = []
        if not gate_passed:
            reason.append(f"gate_failed={','.join(gate_failures)}")
        if not compare_scores(metric, score, parent_score):
            reason.append(f"score_regressed (parent={parent_score})")
        print(f"EVALUATED {args.exp_id} score={score} {' '.join(reason)}{suffix}")
        return 0
    except Exception as exc:  # noqa: BLE001
        # Try to salvage score from traces written before failure
        salvaged_score = None
        salvaged_result = None
        try:
            trace_files = sorted(traces_dir.glob("*.json"))
            if trace_files:
                task_scores = {}
                for tf in trace_files:
                    t = json.loads(tf.read_text(encoding="utf-8"))
                    task_scores[t["task_id"]] = t.get("score", 0.0)
                if task_scores:
                    salvaged_score = round(sum(task_scores.values()) / len(task_scores), 4)
                    salvaged_result = {"score": salvaged_score, "tasks": task_scores}
        except Exception:
            pass

        error_msg = str(exc)

        def _mark_failed(current_node: dict, _graph: dict) -> None:
            current_node["status"] = "failed"
            current_node["error"] = error_msg
            if salvaged_score is not None:
                current_node["score"] = salvaged_score
                current_node["benchmark_result"] = salvaged_result

        update_node(root, args.exp_id, _mark_failed)
        _finalize_result(root, args.exp_id, node, salvaged_score, "failed", {"error": str(exc)})
        _write_attempt_outcome(
            root, args.exp_id, attempt_n, "failed",
            node=node, started_at=started_at, score=salvaged_score,
            benchmark=benchmark_record, gates=gate_records,
            error=error_msg, parent_score=parent_score, metric=metric,
        )
        write_scratchpad(root)
        print(f"FAILED {args.exp_id} {exc}")
        return 1


def _record_done_result(root: Path, args: argparse.Namespace) -> int:
    config, graph = _require_workspace(root)
    node = _read_node(root, args.exp_id)
    if node.get("status") not in (None, "pending", "active", "evaluated", "failed"):
        print(f"ERROR: {args.exp_id} has status '{node['status']}' -- cannot record again", file=sys.stderr)
        return 1
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
    status = "committed" if keep else "evaluated"

    def _mark(current_node: dict, _graph: dict) -> None:
        current_node["status"] = status
        current_node["score"] = args.score
        if status == "evaluated":
            current_node["evaluated_attempts"] = int(current_node.get("evaluated_attempts", 0)) + 1

    update_node(root, args.exp_id, _mark)
    _finalize_result(root, args.exp_id, node, args.score, status, {"recorded_only": True})
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


def _stop_dashboard(root: Path) -> None:
    """Stop the background dashboard if running."""
    pid_file = evo_dir(root) / "dashboard.pid"
    port_file = evo_dir(root) / "dashboard.port"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 15)  # SIGTERM
        except (OSError, ValueError):
            pass
        pid_file.unlink(missing_ok=True)
    port_file.unlink(missing_ok=True)


def cmd_reset(args: argparse.Namespace) -> int:
    if not args.yes:
        raise RuntimeError("reset is destructive; re-run with --yes")
    root = repo_root()
    _stop_dashboard(root)
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
        f"evaluated={sum(1 for n in nodes if n.get('status') == 'evaluated')} "
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


def _format_frontier_help() -> str:
    from . import frontier_strategies as fs
    lines = [
        "evo frontier -- return frontier nodes (committed leaves) ranked by a selection strategy.",
        "",
        "Usage:",
        "  evo frontier                                    # use configured strategy",
        "  evo frontier --strategy <kind>                   # override for this call only",
        "  evo frontier --strategy <kind> --params '<json>' # override with custom params",
        "  evo frontier --seed <int>                        # pin rng for reproducible stochastic picks",
        "  evo frontier --help-strategies                   # this text",
        "",
        "Strategy is read from `.evo/config.json` under `frontier_strategy`.",
        "Set it once via the dashboard's strategy panel (top bar) or by editing the config directly.",
        "Every call appends an event to `.evo/infra_log.json` with kind=frontier.",
        "",
        "Available strategies:",
        "",
    ]
    for kind, spec in fs.FRONTIER_STRATEGIES.items():
        lines.append(f"  {kind}  -- {spec['label']}")
        lines.append(f"    {spec['description']}")
        if spec["params"]:
            lines.append("    params:")
            for p in spec["params"]:
                lines.append(
                    f"      {p['name']} ({p['type']}, {p['min']}..{p['max']}, default {p['default']})"
                    f"  -- {p['label']}"
                )
        else:
            lines.append("    params: none")
        lines.append("")
    lines.append("Output envelope: {\"strategy\": {...}, \"generated_at\": \"...\", \"nodes\": [...], \"seed\": <int>}")
    lines.append("Each node carries: id, score, eval_epoch (as \"epoch\"), hypothesis, rank.")
    return "\n".join(lines)


def cmd_frontier(args: argparse.Namespace) -> int:
    from . import frontier_strategies as fs
    if getattr(args, "help_strategies", False):
        print(_format_frontier_help())
        return 0
    root = repo_root()
    config, graph = _require_workspace(root)

    raw_nodes = frontier_nodes(graph)
    # Normalize each node to the minimal shape pickers/logs consume.
    summaries = [
        {
            "id": n["id"],
            "score": n.get("score"),
            "eval_epoch": n.get("eval_epoch"),
            "hypothesis": n.get("hypothesis"),
        }
        for n in raw_nodes
    ]

    # Resolve strategy: CLI overrides > config > default.
    strategy = fs.resolve_from_config(config)
    if getattr(args, "strategy", None):
        params = strategy["params"]
        if getattr(args, "params", None):
            try:
                params = json.loads(args.params)
            except json.JSONDecodeError as exc:
                print(f"ERROR: --params must be JSON: {exc}", file=sys.stderr)
                return 1
        strategy = fs.validate_frontier_strategy({"kind": args.strategy, "params": params})

    # Load per-experiment outcomes for strategies that need per-task vectors.
    outcomes: dict[str, dict] = {}
    if strategy["kind"] == "pareto_per_task":
        for n in raw_nodes:
            attempt = n.get("current_attempt")
            if not attempt:
                continue
            path = attempt_outcome_path(root, n["id"], int(attempt))
            if path.exists():
                try:
                    outcomes[n["id"]] = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    pass

    metric = config.get("metric", "max")
    try:
        ranked, seed_used = fs.pick(
            summaries, strategy, metric,
            outcomes=outcomes,
            seed=args.seed if getattr(args, "seed", None) is not None else None,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    envelope = {
        "strategy": strategy,
        "generated_at": utc_now(),
        "nodes": ranked,
    }
    # Seed only included when the strategy is stochastic, to keep deterministic
    # runs noise-free.
    if strategy["kind"] in {"epsilon_greedy", "softmax", "pareto_per_task"}:
        envelope["seed"] = seed_used

    fs.append_frontier_log(root, strategy, [n["id"] for n in ranked],
                           seed=envelope.get("seed"))

    print(json.dumps(envelope, indent=2))
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
    graph = load_graph(root)
    if args.exp_id not in graph["nodes"]:
        raise RuntimeError(f"unknown experiment: {args.exp_id}")
    node = dict(graph["nodes"][args.exp_id])
    node["own_gates"] = node.get("gates", [])
    node["gates"] = collect_gates_from_path(graph, args.exp_id)
    print(json.dumps(node, indent=2))
    return 0


def cmd_path(args: argparse.Namespace) -> int:
    root = repo_root()
    _config, graph = _require_workspace(root)
    if args.exp_id not in graph["nodes"]:
        raise RuntimeError(f"unknown experiment: {args.exp_id}")
    chain = path_to_node(graph, args.exp_id)
    for node in chain:
        score_str = f"  score={node['score']}" if node.get("score") is not None else ""
        hyp = f"  {node.get('hypothesis', '')}" if node["id"] != "root" else ""
        prefix = "  -> " if node["id"] != "root" else ""
        print(f"{prefix}{node['id']}{score_str}{hyp}")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    root = repo_root()
    if args.other_id is None:
        node = _read_node(root, args.exp_id)
        attempt = int(node.get("current_attempt", 0))
        if attempt == 0:
            print("")
            return 0
        target = attempt_log_path(root, args.exp_id, attempt, "diff.patch")
        print(target.read_text(encoding="utf-8") if target.exists() else "")
        return 0
    config, graph = _require_workspace(root)
    node_a = _read_node(root, args.exp_id)
    node_b = _read_node(root, args.other_id)
    ref_a = node_a.get("commit") or node_a.get("branch")
    ref_b = node_b.get("commit") or node_b.get("branch")
    if not ref_a or not ref_b:
        raise RuntimeError("both experiments must have a commit or branch to diff")
    result = subprocess.run(
        ["git", "diff", ref_a, ref_b, "--", relative_target(config)],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    print(result.stdout)
    return 0


def cmd_traces(args: argparse.Namespace) -> int:
    root = repo_root()
    node = _read_node(root, args.exp_id)
    attempt = int(node.get("current_attempt", 0))
    if attempt == 0:
        if args.task:
            print("")
        else:
            print("{}")
        return 0
    traces_dir = attempt_traces_dir(root, args.exp_id, attempt)
    if args.task:
        path = traces_dir / f"task_{args.task}.json"
        print(path.read_text(encoding="utf-8"))
        return 0
    payload = {}
    if traces_dir.exists():
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


def cmd_gate(args: argparse.Namespace) -> int:
    root = repo_root()
    _config, graph = _require_workspace(root)

    if args.gate_action == "add":
        entry = add_gate(root, args.exp_id, args.name, args.command)
        write_scratchpad(root)
        print(json.dumps(entry, indent=2))
        return 0

    if args.gate_action == "remove":
        remove_gate(root, args.exp_id, args.name)
        write_scratchpad(root)
        print(f"Removed gate '{args.name}' from {args.exp_id}")
        return 0

    if args.gate_action == "list":
        gates = collect_gates_from_path(graph, args.exp_id)
        # Annotate each gate with the node it came from
        node_gates_map: dict[str, str] = {}
        for node in path_to_node(graph, args.exp_id):
            for g in node.get("gates", []):
                node_gates_map[g["name"]] = node["id"]
        output = []
        for g in gates:
            output.append({
                "name": g["name"],
                "command": g["command"],
                "from": node_gates_map.get(g["name"], "unknown"),
            })
        print(json.dumps(output, indent=2))
        return 0

    return 1


def cmd_dashboard(args: argparse.Namespace) -> int:
    from .dashboard import create_app

    root = repo_root()
    actual_port = _pick_free_port(args.port)
    (evo_dir(root) / "dashboard.port").write_text(str(actual_port))
    note = "" if actual_port == args.port else f" (port {args.port} busy, bumped to {actual_port})"
    print(f"Dashboard live: http://127.0.0.1:{actual_port}{note}", flush=True)
    app = create_app(root)
    app.run(host="127.0.0.1", port=actual_port, debug=False)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evo")
    # Format includes the distribution name so skill checks can distinguish
    # this binary from unrelated `evo` packages on PATH.
    parser.add_argument(
        "--version",
        action="version",
        version=f"{DISTRIBUTION_NAME} {__version__}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init")
    init_p.add_argument("--target", required=True)
    init_p.add_argument("--benchmark", required=True)
    init_p.add_argument("--metric", required=True, choices=["max", "min"])
    init_p.add_argument("--gate")
    init_p.add_argument("--instrumentation-mode", choices=["sdk", "inline"])
    init_p.add_argument("--port", type=int, default=8080)
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

    frontier_p = sub.add_parser(
        "frontier",
        help="list frontier nodes ranked by the configured strategy",
        description="Return frontier nodes ranked by the configured strategy. "
                    "Run `evo frontier --help-strategies` for full descriptions of each strategy and its params.",
    )
    frontier_p.add_argument("--strategy",
                            help="override configured strategy (run --help-strategies for options)")
    frontier_p.add_argument("--params", help="JSON params for the overridden strategy, e.g. '{\"k\": 5}'")
    frontier_p.add_argument("--seed", type=int, help="rng seed for stochastic strategies (default: fresh, logged)")
    frontier_p.add_argument("--help-strategies", dest="help_strategies", action="store_true",
                            help="print detailed description of each strategy and its params, then exit")
    frontier_p.set_defaults(func=cmd_frontier)

    scratchpad_p = sub.add_parser("scratchpad")
    scratchpad_p.set_defaults(func=cmd_scratchpad)

    get_p = sub.add_parser("get")
    get_p.add_argument("exp_id")
    get_p.add_argument("filename", nargs="?")
    get_p.set_defaults(func=cmd_get)

    path_p = sub.add_parser("path")
    path_p.add_argument("exp_id")
    path_p.set_defaults(func=cmd_path)

    diff_p = sub.add_parser("diff")
    diff_p.add_argument("exp_id")
    diff_p.add_argument("other_id", nargs="?")
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

    gate_p = sub.add_parser("gate")
    gate_sub = gate_p.add_subparsers(dest="gate_action", required=True)

    gate_add_p = gate_sub.add_parser("add")
    gate_add_p.add_argument("exp_id")
    gate_add_p.add_argument("--name", required=True)
    gate_add_p.add_argument("--command", required=True)
    gate_add_p.set_defaults(func=cmd_gate)

    gate_list_p = gate_sub.add_parser("list")
    gate_list_p.add_argument("exp_id")
    gate_list_p.set_defaults(func=cmd_gate)

    gate_remove_p = gate_sub.add_parser("remove")
    gate_remove_p.add_argument("exp_id")
    gate_remove_p.add_argument("--name", required=True)
    gate_remove_p.set_defaults(func=cmd_gate)

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
