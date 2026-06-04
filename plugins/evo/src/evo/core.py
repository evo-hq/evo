from __future__ import annotations

import json
import os
import re
import secrets
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
KEYFILE_NAME = "keyfile"
RUNTIME_ENV_VALUES_FILE = "runtime_env_values.json"

SUPPORTED_HOSTS = frozenset({
    "claude-code",
    "codex",
    "cursor",
    "opencode",
    "openclaw",
    "hermes",
    "pi",
    "generic",
})

# Hosts that support evo dispatch's fork-cache mechanism. Other hosts use
# their native parallel-Task primitive — see plugins/evo/skills/optimize/SKILL.md.
DISPATCH_HOSTS = frozenset({"claude-code"})


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


def get_host(root: Path) -> str | None:
    """Return the orchestrator host recorded for this workspace, or None
    if the workspace pre-dates the host-signature field. Existing commands
    that don't need fork-cache should tolerate a None return."""
    return _load_meta(root).get("host")


def set_host(root: Path, host: str) -> None:
    """Set the orchestrator host for this workspace. Validates against
    SUPPORTED_HOSTS and rejects unknown values."""
    if host not in SUPPORTED_HOSTS:
        allowed = ", ".join(sorted(SUPPORTED_HOSTS))
        raise RuntimeError(f"unknown host '{host}'; supported: {allowed}")
    meta = _load_meta(root)
    meta["host"] = host
    _save_meta(root, meta)


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


def runtime_env_values_path(root: Path) -> Path:
    return workspace_path(root) / RUNTIME_ENV_VALUES_FILE


def keyfile_path(root: Path) -> Path:
    return evo_dir(root) / KEYFILE_NAME


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


def atomic_write_bytes(path: Path, data: bytes, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        tmp_path = Path(tmp_name)
        if mode is not None:
            os.chmod(tmp_path, mode)
        tmp_path.replace(path)
        if mode is not None:
            os.chmod(path, mode)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def ensure_workspace_keyfile(root: Path) -> Path:
    path = keyfile_path(root)
    if not path.exists():
        atomic_write_bytes(path, secrets.token_bytes(32), mode=0o600)
    else:
        os.chmod(path, 0o600)
    return path


DEFAULT_MAX_ATTEMPTS = 3


def default_config(
    root: Path,
    target: str,
    benchmark: str,
    metric: str,
    gate: str | None,
    project_name: str | None = None,
) -> dict[str, Any]:
    # Import here to avoid a circular import at module load.
    from .frontier_strategies import DEFAULT_FRONTIER_STRATEGY
    return {
        "repo_root": str(root),
        "workspace_dir": WORKSPACE_NAME,
        "worktrees_dir": "worktrees",
        "project_name": project_name or root.name,
        "target": target,
        "benchmark": benchmark,
        "gate": gate,
        "metric": metric,
        "current_eval_epoch": 1,
        "comparison_blocked": False,
        "max_attempts": DEFAULT_MAX_ATTEMPTS,
        "frontier_strategy": DEFAULT_FRONTIER_STRATEGY,
        "runtime_env": {
            "inherit_shell": True,
            "dotenv": [],
        },
        "initialized_at": utc_now(),
    }


def default_graph() -> dict[str, Any]:
    return {
        "root": "root",
        "next_id": 0,
        "workspace_notes": [],
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


def add_workspace_note(root: Path, text: str) -> dict[str, Any]:
    """Write a workspace-level note (not tied to any experiment).
    Returns the new record."""
    gpath = graph_path(root)
    with advisory_lock(lock_file_for(gpath)):
        graph = load_json(gpath, default_graph())
        graph.setdefault("workspace_notes", [])
        entry = {
            "text": text,
            "timestamp": utc_now(),
            "exp_id": None,
        }
        graph["workspace_notes"].append(entry)
        atomic_write_json(gpath, graph)
        return entry


def list_all_notes(graph: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every note in the graph (workspace + per-node), sorted by
    timestamp descending. Each record includes its `exp_id` (None for
    workspace notes) so callers can render a flat list without re-walking."""
    out: list[dict[str, Any]] = []
    for entry in graph.get("workspace_notes", []) or []:
        copy = dict(entry)
        copy.setdefault("exp_id", None)
        out.append(copy)
    for node in graph["nodes"].values():
        if node.get("id") == "root":
            continue
        for entry in node.get("notes", []):
            copy = dict(entry)
            copy.setdefault("exp_id", node["id"])
            out.append(copy)
    out.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    return out


def load_config(root: Path) -> dict[str, Any]:
    return load_json(config_path(root), {})


def save_config(root: Path, config: dict[str, Any]) -> None:
    path = config_path(root)
    with advisory_lock(lock_file_for(path)):
        atomic_write_json(path, config)


_DOTENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse the simple dotenv subset evo supports for runtime env forwarding."""
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not _DOTENV_KEY_RE.match(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        elif len(value) >= 2 and value[0] == value[-1] == '"':
            value = bytes(value[1:-1], "utf-8").decode("unicode_escape")
        else:
            match = re.search(r"\s+#", value)
            if match:
                value = value[:match.start()].rstrip()
        values[key] = value
    return values


def _dotenv_path(root: Path, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = root / path
    return path


def resolve_runtime_env(root: Path, config: dict[str, Any]) -> dict[str, str]:
    """Resolve benchmark/gate runtime env fresh for each attempt.

    Config stores source metadata only; values are loaded from the orchestrator
    process environment and configured dotenv files when this function runs.
    """
    runtime_env = dict(config.get("runtime_env") or {})
    resolved: dict[str, str] = {}
    if runtime_env.get("inherit_shell", True):
        resolved.update(os.environ)

    for source in runtime_env.get("dotenv", []) or []:
        if not isinstance(source, dict):
            continue
        raw_path = str(source.get("path") or "")
        if not raw_path:
            continue
        path = _dotenv_path(root, raw_path)
        if not path.exists():
            raise RuntimeError(f"runtime dotenv file not found: {raw_path} ({path})")
        parsed = parse_dotenv(path.read_text(encoding="utf-8"))
        mode = source.get("mode", "all")
        if mode == "all":
            resolved.update(parsed)
        elif mode == "allow":
            allowed = [str(k) for k in source.get("keys", []) or []]
            for key in allowed:
                if key in parsed:
                    resolved[key] = parsed[key]
        else:
            raise RuntimeError(f"unknown runtime dotenv mode for {raw_path}: {mode!r}")
    values = load_json(runtime_env_values_path(root), {"variables": {}})
    runtime_variables = values.get("variables", {}) if isinstance(values, dict) else {}
    if isinstance(runtime_variables, dict):
        resolved.update({str(key): str(value) for key, value in runtime_variables.items()})
    return resolved


def _redact_env_preview(value: str) -> str:
    if value == "":
        return "<empty>"
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}...{value[-2:]}"


def runtime_env_summary(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Return redacted runtime env metadata for CLI/UI display."""
    runtime_env = dict(config.get("runtime_env") or {})
    sources = []
    key_names: set[str] = set(os.environ) if runtime_env.get("inherit_shell", True) else set()
    dotenv_key_previews: dict[str, str] = {}
    for source in runtime_env.get("dotenv", []) or []:
        if not isinstance(source, dict):
            continue
        raw_path = str(source.get("path") or "")
        path = _dotenv_path(root, raw_path) if raw_path else root
        entry = {
            "path": raw_path,
            "mode": source.get("mode", "all"),
            "keys": list(source.get("keys", []) or []),
            "exists": path.exists(),
        }
        if path.exists():
            parsed = parse_dotenv(path.read_text(encoding="utf-8"))
            if entry["mode"] == "all":
                entry["resolved_keys"] = sorted(parsed)
                entry["key_previews"] = {
                    key: _redact_env_preview(parsed[key])
                    for key in sorted(parsed)
                }
                dotenv_key_previews.update(entry["key_previews"])
                key_names.update(parsed)
            else:
                allowed = [str(k) for k in source.get("keys", []) or []]
                present = sorted(k for k in allowed if k in parsed)
                entry["resolved_keys"] = present
                entry["key_previews"] = {
                    key: _redact_env_preview(parsed[key])
                    for key in present
                }
                dotenv_key_previews.update(entry["key_previews"])
                key_names.update(present)
        sources.append(entry)
    values = load_json(runtime_env_values_path(root), {"variables": {}})
    runtime_variables = values.get("variables", {}) if isinstance(values, dict) else {}
    variable_previews = {
        str(key): _redact_env_preview(str(value))
        for key, value in sorted((runtime_variables or {}).items())
    } if isinstance(runtime_variables, dict) else {}
    key_names.update(variable_previews)
    return {
        "inherit_shell": runtime_env.get("inherit_shell", True),
        "dotenv": sources,
        "resolved_key_count": len(key_names),
        "resolved_keys": sorted(key_names),
        "configured_key_previews": dict(sorted(dotenv_key_previews.items())),
        "runtime_variable_previews": variable_previews,
    }


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


def init_workspace(
    root: Path,
    target: str,
    benchmark: str,
    metric: str,
    gate: str | None,
    host: str | None = None,
    commit_strategy: str = "all",
    project_name: str | None = None,
) -> str:
    if commit_strategy not in ("all", "tracked-only"):
        raise RuntimeError(
            f"commit_strategy must be 'all' or 'tracked-only', got {commit_strategy!r}"
        )
    run_id = _allocate_run(root)
    ensure_workspace_dirs(root)
    config = default_config(root, target, benchmark, metric, gate, project_name=project_name)
    config["execution_backend"] = "worktree"
    config["commit_strategy"] = commit_strategy
    atomic_write_json(config_path(root), config)
    atomic_write_json(graph_path(root), default_graph())
    atomic_write_json(annotations_path(root), {"annotations": []})
    atomic_write_json(infra_path(root), {"events": []})
    ensure_workspace_keyfile(root)
    if not project_path(root).exists():
        atomic_write_text(project_path(root), "# Project Understanding\n\n")
    if host is not None:
        set_host(root, host)
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


def parse_diff_patch(root: Path, exp_id: str, attempt: int) -> dict[str, Any] | None:
    """Parse experiments/<exp_id>/attempts/NNN/diff.patch into structured data.

    Returns {"files": [str], "added": int, "removed": int} or None if the patch
    is missing, empty, or contains no diff headers. Used by both the scratchpad
    diff-summary line and outcome.json's `change_files` field.
    """
    if attempt <= 0:
        return None
    patch = experiments_path(root) / exp_id / "attempts" / f"{attempt:03d}" / "diff.patch"
    if not patch.exists():
        return None
    content = patch.read_text(encoding="utf-8")
    if not content.strip():
        return None
    files: list[str] = []
    added = 0
    removed = 0
    for line in content.splitlines():
        if line.startswith("diff --git"):
            parts = line.split()
            if len(parts) >= 4:
                files.append(parts[3].removeprefix("b/"))
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    if not files:
        return None
    return {"files": files, "added": added, "removed": removed}


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


def make_infra_event(event_kind: str, event_message: str, **fields: Any) -> dict[str, Any]:
    """Build a canonical infra-log event.

    Keep required schema fields in one helper so event producers do not drift
    from what scratchpad/dashboard readers expect.
    """
    reserved = {"kind", "message", "timestamp"}
    overlap = reserved.intersection(fields)
    if overlap:
        names = ", ".join(sorted(overlap))
        raise ValueError(f"infra event fields may not override: {names}")
    event_kind = event_kind.strip()
    event_message = event_message.strip()
    if not event_kind:
        raise ValueError("infra event kind is required")
    if not event_message:
        raise ValueError("infra event message is required")
    return {
        "kind": event_kind,
        "message": event_message,
        "timestamp": utc_now(),
        **fields,
    }


def append_infra_event(root: Path, message: str, breaking: bool) -> dict[str, Any]:
    path = infra_path(root)
    with advisory_lock(lock_file_for(path)):
        data = load_json(path, {"events": []})
        event = make_infra_event("infra", message, breaking=breaking)
        data.setdefault("events", []).append(event)
        atomic_write_json(path, data)
        return event


def allocate_experiment(
    root: Path,
    parent_id: str,
    hypothesis: str,
    backend_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Allocate a new experiment node and its workspace.

    Graph mutation (ID generation, parent linking, status init) stays here;
    workspace creation is delegated to the configured backend.
    """
    from .backends import AllocateCtx, backend_spec_from_config, load_backend

    gpath = graph_path(root)
    with advisory_lock(lock_file_for(gpath)):
        graph = load_json(gpath, default_graph())
        nodes = graph["nodes"]
        if parent_id not in nodes:
            raise KeyError(f"Unknown parent experiment: {parent_id}")
        parent = nodes[parent_id]
        if parent.get("status") == "pruned":
            raise RuntimeError(
                f"Cannot allocate child of pruned parent {parent_id}. "
                f"Pruning marks a branch as unpromising; branch elsewhere "
                f"or re-status the parent if you want to continue here."
            )
        next_id = graph.get("next_id", 0)
        exp_id = f"exp_{next_id:04d}"
        graph["next_id"] = next_id + 1

        meta = _load_meta(root)
        run_id = meta.get("active", "run")
        branch = f"evo/{run_id}/{exp_id}"
        start_point = current_branch(root) if parent_id == "root" else parent["branch"]
        if not start_point:
            raise RuntimeError(f"Parent {parent_id} does not have a branch to fork from")

        # parent_commit is the canonical frozen reference for backends that
        # need a commit hash (PoolBackend's `git checkout -B <branch> <hash>`).
        # For non-root parents we trust what was recorded when the parent was
        # committed -- in pool mode the parent's branch lives in a slot, not
        # in the main repo, so a rev-parse against `start_point` from cwd=root
        # would fail. Root children fall back to rev-parsing the main repo's
        # current branch.
        if parent_id == "root":
            parent_commit = subprocess.run(
                ["git", "rev-parse", start_point],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        else:
            parent_commit = parent.get("commit")
            if not parent_commit:
                raise RuntimeError(
                    f"Parent {parent_id} has no recorded commit; cannot allocate child"
                )

        workspace_config = load_config(root)
        if backend_override is None:
            backend_name, backend_config = backend_spec_from_config(workspace_config)
        else:
            backend_name = backend_override["name"]
            backend_config = dict(backend_override.get("config") or {})

        backend = load_backend(
            root,
            explicit_name=backend_name,
            explicit_config=backend_config,
        )
        result = backend.allocate(
            AllocateCtx(
                root=root,
                exp_id=exp_id,
                parent_node=parent if parent_id != "root" else None,
                parent_commit=parent_commit,
                parent_ref=start_point,
                branch=branch,
                hypothesis=hypothesis,
            )
        )

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
            "backend": backend_name,
            "backend_config": backend_config,
            "branch": result.branch,
            "worktree": str(result.worktree),
            "commit": result.commit,
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


def remove_worktree_only(root: Path, node: dict[str, Any]) -> bool:
    """Remove the worktree directory only (no branch deletion). Used by `evo gc`.

    Returns True if the backend actually freed disk-side state, False if it
    was a no-op (pool mode -- slot directories are user-owned). Callers use
    this to avoid reporting freed-resource counts that don't match reality.
    """
    from .backends import DiscardCtx, load_backend

    return load_backend(root, node=node).gc(DiscardCtx(root=root, node=node))


def delete_discarded_experiment(root: Path, node: dict[str, Any]) -> None:
    """Full cleanup: remove worktree and delete branch. Used by `evo discard`."""
    from .backends import DiscardCtx, load_backend

    load_backend(root, node=node).discard(DiscardCtx(root=root, node=node))


def reset_runtime_state(root: Path) -> None:
    """Remove the active run's worktrees, branches, and directory."""
    from .backends import load_backend

    load_backend(root).reset_all(root)


def git_status_porcelain(path: Path) -> str:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def maybe_commit_worktree(
    node: dict[str, Any],
    hypothesis: str,
    commit_strategy: str = "all",
    executor: Any = None,
) -> str | None:
    """Stage and commit the experiment's edits inside its workspace.

    `executor` (a `WorkspaceExecutor`) routes git invocations either as
    local subprocess (worktree/pool backends) or through sandbox-agent
    (remote backend). Default `None` preserves backwards-compat for any
    caller that hasn't been updated to pass one (resolves to local).
    """
    if commit_strategy not in ("all", "tracked-only"):
        raise RuntimeError(
            f"commit_strategy must be 'all' or 'tracked-only', got {commit_strategy!r}"
        )
    if executor is None:
        from .workspace_executor import LocalExecutor
        executor = LocalExecutor()

    worktree = Path(node["worktree"])
    status = executor.run(["git", "status", "--porcelain"], cwd=worktree)
    if status.exit_code != 0:
        raise RuntimeError(
            f"git status --porcelain failed in {worktree}: {status.stderr[:500]}"
        )
    if not status.stdout.strip():
        # No changes; report the current HEAD as the commit so callers
        # don't need to special-case the no-op path.
        head = executor.run(["git", "rev-parse", "HEAD"], cwd=worktree)
        return head.stdout.strip() if head.exit_code == 0 else None

    add_args = ["-A"] if commit_strategy == "all" else ["-u"]
    add = executor.run(["git", "add", *add_args], cwd=worktree)
    if add.exit_code != 0:
        raise RuntimeError(f"git add failed: {add.stderr[:500]}")

    # In tracked-only mode the index may still be empty after `git add -u`
    # if the only changes were untracked files. `git commit` would fail
    # with "nothing to commit"; treat as a no-op.
    diff_check = executor.run(
        ["git", "diff", "--cached", "--quiet"], cwd=worktree,
    )
    if diff_check.exit_code == 0:
        head = executor.run(["git", "rev-parse", "HEAD"], cwd=worktree)
        return head.stdout.strip() if head.exit_code == 0 else None

    commit_result = executor.run(
        ["git", "commit", "-m", f"evo: {node['id']} {hypothesis}"],
        cwd=worktree,
    )
    if commit_result.exit_code != 0:
        raise RuntimeError(f"git commit failed: {commit_result.stderr[:500]}")
    head = executor.run(["git", "rev-parse", "HEAD"], cwd=worktree)
    return head.stdout.strip() if head.exit_code == 0 else None


def render_git_diff(
    root: Path,
    parent_ref: str,
    worktree: Path,
    relative_path: str,
    executor: Any = None,
) -> str:
    """`git diff <parent_ref> -- <path>` against the experiment's worktree.

    `executor` (a `WorkspaceExecutor`) routes the call appropriately for
    the active backend. Default `None` resolves to a local executor for
    backwards-compat with callers that haven't been updated.
    """
    if executor is None:
        from .workspace_executor import LocalExecutor
        executor = LocalExecutor()
    result = executor.run(
        ["git", "diff", parent_ref, "--", relative_path],
        cwd=worktree,
    )
    if result.exit_code != 0:
        raise RuntimeError(
            f"git diff {parent_ref} -- {relative_path} failed: "
            f"{result.stderr[:500]}"
        )
    return result.stdout


def fill_command_template(template: str, *, target: Path, worktree: Path) -> str:
    # Only replace the two documented placeholders so benchmark commands can
    # freely contain JSON or Python dict literals without escaping braces.
    return template.replace("{target}", str(target)).replace("{worktree}", str(worktree))


def load_result(result_path: Path, stdout: str) -> tuple[float, dict[str, Any] | None]:
    """Read score from the result file if present (strict), else parse stdout.

    File present means a writer claimed this attempt. Empty / malformed /
    missing-'score' all raise; the stdout fallback only applies when the
    file is absent.
    """
    if result_path.exists():
        if result_path.stat().st_size == 0:
            raise ValueError(f"{result_path} is empty (benchmark crashed mid-publish)")
        try:
            parsed = json.loads(result_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{result_path} is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict) or "score" not in parsed:
            raise ValueError(f"{result_path} missing 'score' field: {parsed!r}")
        return float(parsed["score"]), parsed
    return parse_score(stdout)


def parse_score(stdout: str) -> tuple[float, dict[str, Any] | None]:
    """Strict legacy fallback: stdout must be one JSON object with 'score'."""
    stripped = stdout.strip()
    if not stripped:
        raise ValueError("Benchmark output was empty")
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        preview = stripped[:200] + ("..." if len(stripped) > 200 else "")
        raise ValueError(
            f"Benchmark stdout is not a single JSON object ({exc}); got: {preview!r}"
        ) from exc
    if not isinstance(parsed, dict) or "score" not in parsed:
        raise ValueError(f"Benchmark stdout JSON missing 'score' field: {parsed!r}")
    return float(parsed["score"]), parsed


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


def add_gate(
    root: Path, exp_id: str, name: str, command: str, *, phase: str = "post",
) -> dict[str, str]:
    """Add a named gate to a node. Returns the gate entry.

    `phase` is one of:
      - "pre"  : runs before the benchmark. A failure aborts the run with
                 no benchmark fired — saves spend on cheap-detectable
                 issues (e.g. cheat checks, file-hash checks).
      - "post" : runs after the benchmark. Needs benchmark output to
                 evaluate (e.g. score regression, output schema validity).
                 Default; preserves pre-phase-split behavior.
    """
    if phase not in ("pre", "post"):
        raise ValueError(f"phase must be 'pre' or 'post', got {phase!r}")
    gate_entry = {
        "name": name, "command": command, "phase": phase, "added_at": utc_now(),
    }

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
