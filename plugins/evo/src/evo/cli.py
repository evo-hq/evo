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
    SUPPORTED_HOSTS,
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
    get_host,
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
    load_result,
    parse_score,
    path_to_node,
    project_path,
    relative_target,
    remove_gate,
    repo_root,
    reset_runtime_state,
    save_config,
    set_host,
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
    if args.host not in SUPPORTED_HOSTS:
        allowed = ", ".join(sorted(SUPPORTED_HOSTS))
        raise RuntimeError(f"--host must be one of: {allowed}")

    # Backend + workspaces validation. Strict: --backend pool requires
    # --workspaces; --backend worktree (or omitted) refuses --workspaces.
    backend_name = args.backend
    workspaces_raw = args.workspaces
    workspaces: list[str] = []
    if workspaces_raw:
        workspaces = [p.strip() for p in workspaces_raw.split(",") if p.strip()]
    if backend_name == "pool":
        if not workspaces:
            raise RuntimeError("--backend pool requires --workspaces /a,/b,/c")
        _validate_pool_slots(root, workspaces)
    else:  # worktree (default)
        if workspaces:
            raise RuntimeError(
                "--workspaces is only valid with --backend pool. "
                "Did you mean: --backend pool --workspaces ...?"
            )

    if args.commit_strategy is not None:
        commit_strategy = args.commit_strategy
    elif backend_name == "pool":
        commit_strategy = "tracked-only"
    else:
        commit_strategy = "all"

    run_id = init_workspace(
        root,
        target=args.target,
        benchmark=args.benchmark,
        metric=args.metric,
        gate=args.gate,
        host=args.host,
        execution_backend=backend_name,
        workspaces=workspaces if backend_name == "pool" else None,
        commit_strategy=commit_strategy,
    )
    if args.instrumentation_mode:
        meta_file = evo_dir(root) / "meta.json"
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        meta["instrumentation_mode"] = args.instrumentation_mode
        atomic_write_json(meta_file, meta)
    write_scratchpad(root)
    _start_dashboard_background(root, port=args.port)
    backend_msg = f" (backend={backend_name}, slots={len(workspaces)})" if backend_name == "pool" else ""
    print(
        f"Initialized evo workspace {run_id} at {workspace_path(root)} "
        f"(host={args.host}, commit_strategy={commit_strategy}){backend_msg}"
    )
    return 0


def _validate_pool_slots(root: Path, slot_paths: list[str]) -> None:
    """Sanity-check each pool slot at init time.

    Each path must exist, be a git working tree, and share an `origin`
    remote URL with the main repo. Slots are also canonicalized
    (`Path.resolve`) to detect duplicates, symlink aliases, the main repo
    itself, and overlapping/nested slot directories -- all of which would
    cause two experiments to share one physical checkout.

    Raises RuntimeError with a per-slot diagnostic on any failure.
    """
    main_origin = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    repo_canonical = root.resolve()

    canonical: list[Path] = []
    for idx, path_str in enumerate(slot_paths):
        slot = Path(path_str)
        if not slot.is_absolute():
            raise RuntimeError(f"--workspaces[{idx}] must be absolute: {path_str}")
        if not slot.exists():
            raise RuntimeError(f"--workspaces[{idx}] does not exist: {slot}")

        slot_canonical = slot.resolve()

        # Reject the main repo itself: leasing it would let the next
        # `evo new` run `git checkout -B evo/...` against the user's working
        # branch -- silent data loss.
        if slot_canonical == repo_canonical:
            raise RuntimeError(
                f"--workspaces[{idx}] resolves to the main repo ({slot_canonical}). "
                f"A pool slot must be a separate clone; using main would let evo "
                f"check out experiment branches over your working tree."
            )

        if not (slot_canonical / ".git").exists():
            raise RuntimeError(
                f"--workspaces[{idx}] is not a git working tree: {slot_canonical}"
            )
        if main_origin:
            slot_origin = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=slot_canonical,
                check=False,
                capture_output=True,
                text=True,
            ).stdout.strip()
            if slot_origin and slot_origin != main_origin:
                raise RuntimeError(
                    f"--workspaces[{idx}] origin mismatch: {slot_origin} vs main {main_origin}"
                )

        # Reject duplicates and overlap. Two slot paths must not resolve to
        # the same directory, and one slot must not be nested inside another.
        for j, prior in enumerate(canonical):
            if slot_canonical == prior:
                raise RuntimeError(
                    f"--workspaces[{idx}] ({slot_canonical}) is a duplicate of "
                    f"--workspaces[{j}] (same canonical path or symlink alias). "
                    f"Each slot must be a distinct directory."
                )
            try:
                slot_canonical.relative_to(prior)
                raise RuntimeError(
                    f"--workspaces[{idx}] ({slot_canonical}) is nested inside "
                    f"--workspaces[{j}] ({prior}). Pool slots cannot overlap."
                )
            except ValueError:
                pass
            try:
                prior.relative_to(slot_canonical)
                raise RuntimeError(
                    f"--workspaces[{idx}] ({slot_canonical}) contains "
                    f"--workspaces[{j}] ({prior}). Pool slots cannot overlap."
                )
            except ValueError:
                pass
        canonical.append(slot_canonical)


def cmd_host(args: argparse.Namespace) -> int:
    root = repo_root()
    action = args.host_action
    if action == "show":
        host = get_host(root)
        print(host if host else "<not set>")
        return 0
    if action == "set":
        set_host(root, args.value)
        print(f"host set to {args.value}")
        return 0
    raise RuntimeError(f"unknown host action: {action}")


# ---------------------------------------------------------------------------
# evo workspace -- pool slot inspection and stale-lease release
# ---------------------------------------------------------------------------


def cmd_workspace_status(args: argparse.Namespace) -> int:
    """Show pool slot occupancy. Errors clearly when pool mode is not active."""
    root = repo_root()
    config = load_config(root)
    if config.get("execution_backend") != "pool":
        print(
            f"ERROR: workspace subcommand only applies in pool mode "
            f"(execution_backend={config.get('execution_backend', 'worktree')!r}).",
            file=sys.stderr,
        )
        return 1

    from .backends import pool_state

    state = pool_state.read_state(root)
    commit_strategy = config.get("commit_strategy", "all")
    if args.json:
        print(json.dumps({**state, "commit_strategy": commit_strategy}, indent=2))
        return 0

    print(f"commit_strategy: {commit_strategy}")
    # Human-readable table. Lease is keyed by experiment status, not by the
    # transient `evo new` PID -- a lease persists across the multi-process
    # lifecycle (allocate, dispatch, run) until the experiment commits or is
    # discarded. PID is recorded for diagnostics only.
    rows = []
    rows.append(("SLOT", "PATH", "LEASED BY", "BRANCH"))
    for slot in state["slots"]:
        lease = slot.get("leased_by")
        if lease is None:
            leased_by = "(idle)"
        else:
            leased_by = f"{lease['exp_id']} (pid {lease['pid']})"
        last_branch = slot.get("last_branch") or "(never leased)"
        rows.append((str(slot["id"]), slot["path"], leased_by, f"last: {last_branch}"))
    widths = [max(len(r[c]) for r in rows) for c in range(4)]
    for row in rows:
        print(
            "  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row))
        )
    return 0


def cmd_workspace_release(args: argparse.Namespace) -> int:
    """Manually clear a slot's lease. For stale-lease recovery only."""
    root = repo_root()
    config = load_config(root)
    if config.get("execution_backend") != "pool":
        print("ERROR: workspace subcommand only applies in pool mode.", file=sys.stderr)
        return 1
    from .backends import pool_state

    target = args.slot_id
    with pool_state.locked_state(root) as state:
        slot = next((s for s in state["slots"] if s["id"] == target), None)
        if slot is None:
            print(f"ERROR: slot {target} not in pool", file=sys.stderr)
            return 1
        if slot.get("leased_by") is None:
            print(f"slot {target} ({slot['path']}) was already free")
            return 0
        prior = slot["leased_by"]
        slot["leased_by"] = None
    print(
        f"released slot {target} ({slot['path']}); was held by "
        f"{prior['exp_id']} (pid {prior['pid']})"
    )
    return 0


# ---------------------------------------------------------------------------
# evo dispatch — fork-cache child spawning (claude-code only)
# ---------------------------------------------------------------------------


def _forks_dir(root: Path) -> Path:
    """Per-run forks directory, under the active run dir. `evo reset` blows
    away the whole run dir so fork job state doesn't leak across resets."""
    return workspace_path(root) / "forks"


def _job_dir(root: Path, exp_id: str) -> Path:
    return _forks_dir(root) / exp_id


def _job_meta_path(root: Path, exp_id: str) -> Path:
    return _job_dir(root, exp_id) / "meta.json"


def _job_status_path(root: Path, exp_id: str) -> Path:
    return _job_dir(root, exp_id) / "status"


def _job_result_path(root: Path, exp_id: str) -> Path:
    return _job_dir(root, exp_id) / "result.json"


def _write_status(root: Path, exp_id: str, status: str) -> None:
    p = _job_status_path(root, exp_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(status + "\n", encoding="utf-8")


def _read_status(root: Path, exp_id: str) -> str:
    p = _job_status_path(root, exp_id)
    if not p.exists():
        return "<missing>"
    return p.read_text(encoding="utf-8").strip()


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # pid exists but isn't ours — treat as alive
        return True
    except OSError:
        # Windows: os.kill(pid, 0) on a dead pid raises generic OSError
        # (errno EINVAL from OpenProcess), not ProcessLookupError.
        return False


def _settle_job(root: Path, exp_id: str) -> str:
    """Read meta+status; if status=running but pid is dead, transition to
    'done' (or 'error' if the experiment node ended up failed). Idempotent.
    Returns the final status string."""
    status = _read_status(root, exp_id)
    if status not in ("running",):
        return status
    meta_p = _job_meta_path(root, exp_id)
    if not meta_p.exists():
        return status
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    pid = int(meta.get("pid", 0) or 0)
    if pid and _is_pid_alive(pid):
        return "running"
    # Subprocess gone — settle the job.
    new_status = "done"
    # Check the experiment's actual outcome to color the status.
    try:
        result_p = experiment_result_path(root, exp_id)
        if result_p.exists():
            outcome = json.loads(result_p.read_text(encoding="utf-8"))
            if outcome.get("status") in ("failed",):
                new_status = "error"
    except Exception:  # noqa: BLE001
        pass
    _write_status(root, exp_id, new_status)
    # Stamp the child's session_id onto the experiment node so future
    # dispatches of THIS node's children can lineage-fork from it. For
    # foreground dispatches the session_id is already in meta (set when
    # spawn_child returned). For background dispatches we extract it from
    # the captured stdout log here, on the done transition.
    try:
        sid = meta.get("child_session_id")
        if not sid and meta.get("stdout_log"):
            sid = _extract_session_id_from_log(Path(meta["stdout_log"]))
        if sid:
            _stamp_session_id(root, exp_id, sid)
    except Exception:  # noqa: BLE001
        pass
    # Materialize result.json combining meta + outcome (best-effort).
    try:
        result_summary = {
            "exp_id": exp_id,
            "parent_id": meta.get("parent_id"),
            "host": meta.get("host"),
            "started_at": meta.get("started_at"),
            "ended_at": utc_now(),
            "status": new_status,
        }
        if result_p.exists():
            result_summary["outcome"] = json.loads(result_p.read_text(encoding="utf-8"))
        atomic_write_json(_job_result_path(root, exp_id), result_summary)
    except Exception:  # noqa: BLE001
        pass
    return new_status


def _extract_session_id_from_log(log_path: Path) -> str | None:
    """Parse a `claude -p --output-format json` stdout log for session_id.
    Tolerates both single-JSON and JSONL formats. Used when settling a
    background dispatch -- foreground already has session_id in meta."""
    if not log_path.exists():
        return None
    try:
        text = log_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    # Try single JSON value first.
    try:
        parsed = json.loads(text)
        events = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        events = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    for ev in events:
        if isinstance(ev, dict) and ev.get("type") == "system" and ev.get("subtype") == "init":
            sid = ev.get("session_id")
            if sid:
                return sid
    for ev in events:
        if isinstance(ev, dict) and ev.get("type") == "result":
            sid = ev.get("session_id")
            if sid:
                return sid
    return None


def _stamp_session_id(root: Path, exp_id: str, session_id: str) -> None:
    """Persist child session_id on the experiment node for lineage forking.
    Idempotent: setting the same value is a no-op."""

    def _apply(node: dict, _graph: dict) -> None:
        if node.get("session_id") != session_id:
            node["session_id"] = session_id
            node["session_runtime"] = "claude-code"

    update_node(root, exp_id, _apply)


def cmd_dispatch(args: argparse.Namespace) -> int:
    """Top-level `evo dispatch` dispatcher — routes to the action handler."""
    action = args.dispatch_action
    if action == "run":
        return _cmd_dispatch_run(args)
    if action == "wait":
        return _cmd_dispatch_wait(args)
    if action == "list":
        return _cmd_dispatch_list(args)
    if action == "status":
        return _cmd_dispatch_status(args)
    if action == "kill":
        return _cmd_dispatch_kill(args)
    raise RuntimeError(f"unknown dispatch action: {action}")


def _cmd_dispatch_run(args: argparse.Namespace) -> int:
    from .dispatch import (
        DispatchNotSupportedError,
        ExplorerSpawnError,
        dispatch_child,
    )
    root = repo_root()
    _require_workspace(root)  # surface the standard "not initialized" error
    try:
        out = dispatch_child(
            root,
            parent_id=args.parent,
            brief=args.message,
            budget=args.budget,
            explore_context=args.explore_context,
            refresh_explorer=args.refresh_explorer,
            background=args.background,
            job_dir_factory=lambda exp_id: _job_dir(root, exp_id),
        )
    except DispatchNotSupportedError as exc:
        # User-facing guidance, exit 2 to distinguish from generic failure.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except ExplorerSpawnError as exc:
        print(f"ERROR: explorer spawn failed: {exc}", file=sys.stderr)
        return 1

    exp_id = out["exp_id"]
    # Write meta + status atomically before returning so that wait/status
    # commands see consistent state even if the orchestrator races.
    meta = {
        "exp_id": exp_id,
        "parent_id": out["parent_id"],
        "host": "claude-code",
        "explorer_session_id": out["explorer_session_id"],
        "lineage": out.get("lineage", False),
        "child_session_id": out.get("session_id"),
        "worktree": out["worktree"],
        "brief": args.message,
        "budget": args.budget,
        "background": out.get("background", False),
        "started_at": out.get("started_at", utc_now()),
        "pid": out.get("pid"),
        "stdout_log": out.get("stdout_log"),
        "stderr_log": out.get("stderr_log"),
    }
    atomic_write_json(_job_meta_path(root, exp_id), meta)

    if args.background:
        _write_status(root, exp_id, "running")
        print(json.dumps({"job_id": exp_id, "exp_id": exp_id, "pid": out.get("pid"),
                          "explorer_session_id": out["explorer_session_id"]}))
        return 0

    # Foreground: dispatch_child has already returned with exit_code/usage.
    # Write status=running first so _settle_job has something to transition;
    # without it _settle_job sees "<missing>" and short-circuits, leaving
    # `evo dispatch list/status` reporting <missing> for completed foreground
    # jobs.
    _write_status(root, exp_id, "running")
    _settle_job(root, exp_id)
    print(json.dumps({
        "job_id": exp_id,
        "exp_id": exp_id,
        "exit_code": out.get("exit_code"),
        "session_id": out.get("session_id"),
        "explorer_session_id": out["explorer_session_id"],
        "lineage": out.get("lineage", False),
        "usage": out.get("usage", {}),
    }, indent=2))
    return 0 if out.get("exit_code", 1) == 0 else 1


def _cmd_dispatch_wait(args: argparse.Namespace) -> int:
    import time
    root = repo_root()
    _require_workspace(root)

    if args.job_ids:
        targets = list(args.job_ids)
    else:
        # All currently-running jobs in this workspace.
        targets = []
        if _forks_dir(root).exists():
            for child in _forks_dir(root).iterdir():
                if child.is_dir() and _read_status(root, child.name) == "running":
                    targets.append(child.name)

    if not targets:
        print("no running jobs")
        return 0

    pending = set(targets)
    poll_interval = float(os.environ.get("EVO_DISPATCH_WAIT_INTERVAL", "0.25"))
    rc = 0
    while pending:
        for exp_id in list(pending):
            status = _settle_job(root, exp_id)
            if status not in ("running",):
                pending.discard(exp_id)
                row = {"exp_id": exp_id, "status": status}
                # Pull a score / outcome line if available
                try:
                    rp = experiment_result_path(root, exp_id)
                    if rp.exists():
                        out = json.loads(rp.read_text(encoding="utf-8"))
                        row["outcome_status"] = out.get("status")
                        row["score"] = out.get("score")
                except Exception:  # noqa: BLE001
                    pass
                if not args.quiet:
                    print(json.dumps(row))
                if status == "error":
                    rc = 1
        if pending:
            time.sleep(poll_interval)
    return rc


def _cmd_dispatch_list(args: argparse.Namespace) -> int:
    root = repo_root()
    _require_workspace(root)
    rows: list[dict] = []
    if _forks_dir(root).exists():
        for child in sorted(_forks_dir(root).iterdir()):
            if not child.is_dir():
                continue
            exp_id = child.name
            status = _settle_job(root, exp_id)  # opportunistically advances stale entries
            meta_p = _job_meta_path(root, exp_id)
            if not meta_p.exists():
                continue
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            if args.running and status != "running":
                continue
            rows.append({
                "exp_id": exp_id,
                "status": status,
                "parent_id": meta.get("parent_id"),
                "started_at": meta.get("started_at"),
                "brief": (meta.get("brief") or "")[:60],
                "pid": meta.get("pid"),
            })
    if args.recent:
        rows = rows[-args.recent:]
    print(json.dumps(rows, indent=2))
    return 0


def _cmd_dispatch_status(args: argparse.Namespace) -> int:
    root = repo_root()
    _require_workspace(root)
    exp_id = args.job_id
    if not _job_dir(root, exp_id).exists():
        print(f"ERROR: no fork job for {exp_id}", file=sys.stderr)
        return 1
    status = _settle_job(root, exp_id)
    meta_p = _job_meta_path(root, exp_id)
    meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.exists() else {}
    out = {
        "exp_id": exp_id,
        "status": status,
        "meta": meta,
    }
    rp = _job_result_path(root, exp_id)
    if rp.exists():
        out["result"] = json.loads(rp.read_text(encoding="utf-8"))
    print(json.dumps(out, indent=2))
    return 0


def _cmd_dispatch_kill(args: argparse.Namespace) -> int:
    import signal
    root = repo_root()
    _require_workspace(root)
    exp_id = args.job_id
    meta_p = _job_meta_path(root, exp_id)
    if not meta_p.exists():
        print(f"ERROR: no fork job for {exp_id}", file=sys.stderr)
        return 1
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    pid = int(meta.get("pid") or 0)
    if not pid or not _is_pid_alive(pid):
        print(f"job {exp_id} not running")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    _write_status(root, exp_id, "killed")
    print(f"sent SIGTERM to pid {pid}; status=killed")
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

    # Shisa-kanko ack for tracked-only mode: when the worktree has any
    # untracked, non-gitignored files, the agent must affirm with
    # --i-staged-new-files that they have either staged any new source files
    # or intentionally left them out. The check runs before _mark_active so
    # an inadmissible run does not mutate node state.
    commit_strategy = config.get("commit_strategy", "all")
    if commit_strategy == "tracked-only":
        worktree = Path(node["worktree"])
        untracked_proc = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=True,
        )
        untracked = [line for line in untracked_proc.stdout.splitlines() if line.strip()]
        ack = getattr(args, "i_staged_new_files", None)
        if untracked and ack != "yes":
            print(
                f"ERROR: {args.exp_id} cannot run: commit_strategy=tracked-only and "
                f"{len(untracked)} untracked file(s) in worktree {worktree}:",
                file=sys.stderr,
            )
            for path in untracked:
                print(f"  {path}", file=sys.stderr)
            print(
                "\nFor each file: if it's a new source file, `git add` it. "
                "If it's warm state (build artifacts, deps, weights), leave "
                "it untracked -- it will persist in the slot but stay out of "
                "the experiment commit. Then re-run with "
                "`--i-staged-new-files yes`.",
                file=sys.stderr,
            )
            if ack is not None and ack != "yes":
                print(
                    f"\n(--i-staged-new-files received value {ack!r}; the only "
                    f"accepted value is 'yes'. This is intentional -- it forces "
                    f"a second deliberate affirmation that the staging step ran.)",
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
    result_path = a_dir / "result.json"
    metric = config["metric"]
    parent_score = _resolve_parent_score(graph, node["parent"])

    benchmark_cmd = fill_command_template(config["benchmark"], target=target, worktree=worktree)
    env = os.environ.copy()
    env["EVO_TRACES_DIR"] = str(traces_dir)
    env["EVO_WORKTREE"] = str(worktree)
    env["EVO_EXPERIMENT_ID"] = args.exp_id
    env["EVO_ATTEMPT"] = str(attempt_n)
    env["EVO_RESULT_PATH"] = str(result_path)

    # Captured before the benchmark runs so it survives crashes too.
    # Use parent commit hash rather than branch ref. In pool mode the parent's
    # branch lives in whichever slot ran it, not necessarily this slot;
    # commit hashes are stable across slots once fetched.
    if node["parent"] == "root":
        parent_ref = current_branch(root)
    else:
        parent_node = _read_node(root, node["parent"])
        parent_ref = parent_node.get("commit") or parent_node["branch"]
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

        score, parsed = load_result(result_path, bench.stdout)
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

        # Strip EVO_* so an SDK-using gate can't clobber result.json or
        # the benchmark's task_*.json traces.
        gate_env = {k: v for k, v in env.items() if not k.startswith("EVO_")}

        for g in inherited_gates:
            gate_cmd = fill_command_template(g["command"], target=target, worktree=worktree)
            gate_log_file = a_dir / f"gate_{g['name']}.log"
            try:
                gate_result = _run_command(gate_cmd, cwd=root, env=gate_env, stdout_path=gate_log_file, stderr_path=gate_log_file, timeout=args.timeout)
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
            commit = maybe_commit_worktree(
                node,
                node.get("hypothesis", "experiment"),
                commit_strategy=commit_strategy,
            )

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
            # Release the workspace lease on transition into `committed`.
            # Worktree backend: no-op. Pool backend: returns the slot to the
            # free queue. Failed and evaluated transitions retain the lease.
            from .backends import DiscardCtx as _DCtx, load_backend as _lb
            committed_node = dict(node)
            committed_node["status"] = "committed"
            committed_node["commit"] = commit
            _lb(root).release_lease(_DCtx(root=root, node=committed_node))
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
    if status == "committed":
        from .backends import DiscardCtx as _DCtx, load_backend as _lb
        _lb(root).release_lease(_DCtx(root=root, node={**node, "status": "committed"}))
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
        if not worktree.exists():
            continue
        # Only report nodes whose backend actually freed something. In pool
        # mode the slot directory always exists (user-owned) and gc is a
        # no-op, so reporting the node as 'removed' would be misleading.
        if remove_worktree_only(root, node):
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
    init_p.add_argument(
        "--host",
        required=True,
        choices=sorted(SUPPORTED_HOSTS),
        help="orchestrator runtime (claude-code/codex/opencode/openclaw/hermes/generic). "
             "Determines whether `evo dispatch` is available; other commands ignore it.",
    )
    init_p.add_argument(
        "--backend",
        default="worktree",
        choices=["worktree", "pool"],
        help="execution backend (default: worktree). `pool` leases user-provided "
             "pre-built workspaces instead of creating a fresh git worktree per "
             "experiment. Requires --workspaces.",
    )
    init_p.add_argument(
        "--workspaces",
        help="comma-separated absolute paths to pre-built workspace directories "
             "(required with --backend pool). Concurrent experiments cap at the "
             "pool size.",
    )
    init_p.add_argument(
        "--commit-strategy",
        choices=["all", "tracked-only"],
        default=None,
        help="commit policy for `evo run`. Default: 'tracked-only' for "
             "--backend pool (preserves untracked warm state across leases), "
             "'all' for worktree. Override only if you know why.",
    )
    init_p.add_argument("--port", type=int, default=8080)
    init_p.set_defaults(func=cmd_init)

    host_p = sub.add_parser(
        "host",
        help="show or set the orchestrator host signature",
    )
    host_sub = host_p.add_subparsers(dest="host_action", required=True)
    host_show_p = host_sub.add_parser("show", help="print the current workspace host")
    host_show_p.set_defaults(func=cmd_host)
    host_set_p = host_sub.add_parser("set", help="update the workspace host")
    host_set_p.add_argument("value", choices=sorted(SUPPORTED_HOSTS))
    host_set_p.set_defaults(func=cmd_host)

    workspace_p = sub.add_parser(
        "workspace",
        help="inspect or release pool slots (pool mode only)",
    )
    workspace_sub = workspace_p.add_subparsers(dest="workspace_action", required=True)
    workspace_status_p = workspace_sub.add_parser("status", help="show pool slot occupancy")
    workspace_status_p.add_argument("--json", action="store_true", help="emit JSON")
    workspace_status_p.set_defaults(func=cmd_workspace_status)
    workspace_release_p = workspace_sub.add_parser(
        "release", help="manually clear a stale lease"
    )
    workspace_release_p.add_argument("slot_id", type=int)
    workspace_release_p.set_defaults(func=cmd_workspace_release)

    new_p = sub.add_parser("new")
    new_p.add_argument("--parent", required=True)
    new_p.add_argument("-m", "--message", required=True)
    new_p.set_defaults(func=cmd_new)

    run_p = sub.add_parser("run")
    run_p.add_argument("exp_id")
    run_p.add_argument("--timeout", type=int, default=1800)
    run_p.add_argument(
        "--i-staged-new-files",
        dest="i_staged_new_files",
        default=None,
        metavar="yes",
        help="declarative ack (shisa-kanko): agent must pass exactly "
             "`--i-staged-new-files yes` to affirm it has `git add`'d any new "
             "source files in the worktree, leaving warm state untracked. "
             "Required in tracked-only commit mode when the worktree has "
             "untracked, non-gitignored files. No-op in commit_strategy=all.",
    )
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

    dispatch_p = sub.add_parser(
        "dispatch",
        help="spawn a child fork from a parent's cached explorer session (claude-code only)",
        description=(
            "Allocate a new experiment under a parent and run a fork-session child "
            "inheriting the parent's explorer KV cache. Available on host=claude-code "
            "only; other hosts use their native parallel-Task primitive."
        ),
    )
    dispatch_sub = dispatch_p.add_subparsers(dest="dispatch_action", required=True)

    dispatch_run_p = dispatch_sub.add_parser(
        "run",
        help="allocate an experiment and run one fork child against it",
    )
    dispatch_run_p.add_argument("--parent", required=True, help="parent experiment id")
    dispatch_run_p.add_argument("-m", "--message", required=True, help="brief / hypothesis (free-form text)")
    dispatch_run_p.add_argument("--budget", type=int, default=3, help="iteration budget for the child (default 3)")
    dispatch_run_p.add_argument(
        "--explore-context",
        default=None,
        help="optional hint for the explorer's read pass; only used when explorer is being built",
    )
    dispatch_run_p.add_argument(
        "--refresh-explorer",
        action="store_true",
        help="force rebuild of the explorer for this parent even if cache is valid",
    )
    dispatch_run_p.add_argument(
        "--background",
        action="store_true",
        help="return immediately with job_id; use `evo dispatch wait` to block",
    )
    dispatch_run_p.set_defaults(func=cmd_dispatch)

    dispatch_wait_p = dispatch_sub.add_parser(
        "wait",
        help="block until specified jobs finish; if none given, wait on all running",
    )
    dispatch_wait_p.add_argument("job_ids", nargs="*", help="exp_id of each job to wait on")
    dispatch_wait_p.add_argument("--quiet", action="store_true", help="suppress per-job completion rows")
    dispatch_wait_p.set_defaults(func=cmd_dispatch)

    dispatch_list_p = dispatch_sub.add_parser("list", help="list dispatch jobs")
    dispatch_list_p.add_argument("--running", action="store_true", help="only show running jobs")
    dispatch_list_p.add_argument("--recent", type=int, default=None, help="trim to N most recent rows")
    dispatch_list_p.set_defaults(func=cmd_dispatch)

    dispatch_status_p = dispatch_sub.add_parser("status", help="show one job's full state")
    dispatch_status_p.add_argument("job_id", help="exp_id of the job")
    dispatch_status_p.set_defaults(func=cmd_dispatch)

    dispatch_kill_p = dispatch_sub.add_parser("kill", help="SIGTERM a running job")
    dispatch_kill_p.add_argument("job_id")
    dispatch_kill_p.set_defaults(func=cmd_dispatch)

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
