from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request, send_from_directory

from .core import (
    PRUNE_KIND_EXHAUSTED,
    PRUNE_KINDS,
    _load_meta,
    _save_meta,
    attempt_dir,
    attempt_outcome_path,
    attempt_traces_dir,
    best_committed_score,
    best_spine_ids,
    effective_status,
    evo_dir,
    experiments_dir_for,
    frontier_nodes,
    graph_path,
    infra_path,
    lock_file_for,
    list_runs,
    load_annotations,
    load_config,
    load_graph,
    lineage_invalidated_by,
    repo_root,
    runtime_env_summary,
    runtime_env_values_path,
    save_config,
    update_node,
)
from .core import utc_now
from .frontier_strategies import (
    DEFAULT_FRONTIER_STRATEGY,
    FRONTIER_STRATEGIES,
    pick as frontier_pick,
    resolve_from_config,
    validate_frontier_strategy,
)
from .scratchpad import build_scratchpad

STATIC_DIR = Path(__file__).parent / "static"
_SECRET_SUBSTRINGS = ("token", "secret", "password", "api_key")


def _redact_value(value: Any, *, key: str | None = None) -> Any:
    if key is not None and any(part in key.lower() for part in _SECRET_SUBSTRINGS):
        return "<redacted>"
    if isinstance(value, dict):
        return {k: _redact_value(v, key=k) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    return value


def _read_attempt_state(root: Path, exp_id: str, attempt: int) -> dict | None:
    path = attempt_dir(root, exp_id, attempt) / "attempt_state.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _latest_attempt_on_disk(root: Path, exp_id: str) -> int | None:
    exp_root = experiments_dir_for(root, exp_id) / "attempts"
    if not exp_root.exists():
        return None
    candidates = sorted(
        (int(p.name) for p in exp_root.iterdir() if p.is_dir() and p.name.isdigit()),
        reverse=True,
    )
    return candidates[0] if candidates else None


def _public_node(
    root: Path,
    node: dict[str, Any],
    *,
    workspace_config: dict[str, Any],
    graph: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from .backends import backend_spec_for_node, backend_state_key

    public = dict(node)
    if "backend_config" in public:
        public["backend_config"] = _redact_value(public.get("backend_config") or {})
    backend_name, backend_config = backend_spec_for_node(
        root,
        node,
        workspace_config=workspace_config,
    )
    resolved = {
        "name": backend_name,
        "config": _redact_value(backend_config),
        "source": "override" if node.get("backend") else "workspace-default",
        "state_key": (
            backend_state_key(backend_name, backend_config)
            if backend_name in {"pool", "remote"}
            else None
        ),
    }
    if backend_name == "remote":
        resolved["provider"] = backend_config.get("provider")
    public["resolved_backend"] = resolved
    public["checks"] = _checks_summary(root, node.get("id", ""))
    if graph is not None:
        public["effective_status"] = effective_status(graph, node)
        blocker = lineage_invalidated_by(graph, node.get("id", ""))
        public["lineage_blocked_by"] = blocker.get("id") if blocker else None
        public["lineage_blocked_reason"] = blocker.get("pruned_reason") if blocker else None

    if public.get("status") == "active":
        exp_id = node.get("id", "")
        attempt_n = _latest_attempt_on_disk(root, exp_id)
        if attempt_n is not None:
            state = _read_attempt_state(root, exp_id, attempt_n)
            if state is not None:
                public["active_attempt"] = {
                    "n": attempt_n,
                    "phase": state.get("phase"),
                    "status": state.get("status"),
                    "started_at": state.get("started_at"),
                    "updated_at": state.get("updated_at"),
                    "pid": state.get("pid"),
                }

    return public


def _checks_summary(root: Path, exp_id: str) -> dict[str, Any]:
    checks_root = experiments_dir_for(root, exp_id) / "checks"
    if not checks_root.exists():
        return {"count": 0, "latest": None}
    checks = []
    for path in sorted(checks_root.iterdir()):
        if not path.is_dir() or not path.name.isdigit():
            continue
        check_path = path / "check.json"
        gate_check_path = path / "gate_check.json"
        payload: dict[str, Any] = {"check": int(path.name), "status": "unknown", "kind": "unknown"}
        source_path = check_path if check_path.exists() else gate_check_path if gate_check_path.exists() else None
        if source_path is not None:
            try:
                payload.update(json.loads(source_path.read_text(encoding="utf-8")))
                if payload.get("kind") == "unknown":
                    payload["kind"] = "gate" if source_path == gate_check_path else "run"
            except Exception:
                payload["status"] = "unreadable"
        payload["artifact_path"] = f"checks/{path.name}"
        payload["trace_count"] = len(list((path / "traces").glob("*.json"))) if (path / "traces").exists() else 0
        payload["has_benchmark_log"] = (path / "benchmark.log").exists()
        checks.append(payload)
    latest = checks[-1] if checks else None
    return {"count": len(checks), "latest": latest}


def _pool_runtime_summary(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    from .backends import backend_state_key
    from .backends import pool_state

    state_key = backend_state_key("pool", config)
    try:
        state = pool_state.read_state(root, state_key)
    except FileNotFoundError:
        return {
            "kind": "pool",
            "state_key": state_key,
            "initialized": False,
            "slot_count": len(config.get("slots", []) or []),
            "leased_count": 0,
            "free_count": len(config.get("slots", []) or []),
            "slots": [],
        }
    slots = [dict(slot) for slot in state.get("slots", [])]
    leased_count = sum(1 for slot in slots if slot.get("leased_by"))
    return {
        "kind": "pool",
        "state_key": state_key,
        "initialized": True,
        "state_file": f"pool-{state_key}.json",
        "slot_count": len(slots),
        "leased_count": leased_count,
        "free_count": len(slots) - leased_count,
        "slots": slots,
    }


def _remote_runtime_summary(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    from .backends import backend_state_key
    from .backends import remote_state

    state_key = backend_state_key("remote", config)
    provider_config = dict(config.get("provider_config", {}) or {})
    try:
        state = remote_state.read_state(root, state_key)
    except FileNotFoundError:
        pool_size = provider_config.get("pool_size")
        return {
            "kind": "remote",
            "state_key": state_key,
            "initialized": False,
            "provider": config.get("provider"),
            "pool_size": None if pool_size in (None, "", "unbounded") else pool_size,
            "sandbox_count": 0,
            "leased_count": 0,
            "free_count": 0,
            "sandboxes": [],
        }
    sandboxes = [_redact_value(dict(sandbox)) for sandbox in state.get("sandboxes", [])]
    leased_count = sum(1 for sandbox in sandboxes if sandbox.get("leased_by"))
    pool_size = provider_config.get("pool_size")
    return {
        "kind": "remote",
        "state_key": state_key,
        "initialized": True,
        "state_file": f"remote-{state_key}.json",
        "provider": state.get("provider", config.get("provider")),
        "pool_size": None if pool_size in (None, "", "unbounded") else pool_size,
        "sandbox_count": len(sandboxes),
        "leased_count": leased_count,
        "free_count": len(sandboxes) - leased_count,
        "sandboxes": sandboxes,
    }


def _backend_runtime_summary(root: Path, name: str, config: dict[str, Any]) -> dict[str, Any]:
    if name == "pool":
        return _pool_runtime_summary(root, config)
    if name == "remote":
        return _remote_runtime_summary(root, config)
    return {"kind": "worktree", "state_key": None, "initialized": True}


def _module_available(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def _provider_readiness(config: dict[str, Any]) -> dict[str, Any]:
    remote_cfg = dict(config.get("execution_backend_config", {}) or {})
    provider = remote_cfg.get("provider")
    provider_config = dict(remote_cfg.get("provider_config", {}) or {})
    modal_auth_present = bool(os.environ.get("MODAL_TOKEN_ID")) or (Path.home() / ".modal.toml").exists()
    daytona_auth_present = bool(os.environ.get("DAYTONA_API_KEY"))
    aws_auth_present = bool(
        os.environ.get("AWS_ACCESS_KEY_ID")
        or os.environ.get("AWS_PROFILE")
        or os.environ.get("AWS_SESSION_TOKEN")
        or os.environ.get("AWS_SECRET_ACCESS_KEY")
    )
    azure_auth_present = _azure_auth_present()
    e2b_source = (
        "workspace-config"
        if provider == "e2b" and provider_config.get("api_key")
        else ("env" if os.environ.get("E2B_API_KEY") else "missing")
    )
    manual_cfg = provider_config if provider == "manual" else {}
    ssh_cfg = provider_config if provider == "ssh" else {}
    return {
        "modal": {
            "sdk_installed": _module_available("modal"),
            "auth_present": modal_auth_present,
            "auth_source": (
                "env"
                if os.environ.get("MODAL_TOKEN_ID")
                else ("modal.toml" if (Path.home() / ".modal.toml").exists() else "missing")
            ),
        },
        "e2b": {
            "sdk_installed": _module_available("e2b"),
            "auth_present": e2b_source != "missing",
            "auth_source": e2b_source,
        },
        "daytona": {
            "sdk_installed": _module_available("daytona"),
            "auth_present": daytona_auth_present,
            "auth_source": "env" if daytona_auth_present else "missing",
        },
        "aws": {
            "sdk_installed": _module_available("boto3"),
            "auth_present": aws_auth_present,
            "auth_source": (
                "env/profile"
                if aws_auth_present
                else "missing"
            ),
        },
        "azure": {
            "sdk_installed": all(
                _module_available(name)
                for name in (
                    "azure.identity",
                    "azure.mgmt.resource",
                    "azure.mgmt.network",
                    "azure.mgmt.compute",
                )
            ),
            "auth_present": azure_auth_present,
            "auth_source": (
                "env"
                if any(
                    os.environ.get(name)
                    for name in (
                        "AZURE_CLIENT_ID",
                        "AZURE_CLIENT_SECRET",
                        "AZURE_TENANT_ID",
                        "ARM_CLIENT_ID",
                        "ARM_CLIENT_SECRET",
                        "ARM_TENANT_ID",
                    )
                )
                else ("azure-cli" if _azure_cli_logged_in() else "missing")
            ),
        },
        "ssh": {
            "ssh_binary": shutil.which("ssh") is not None,
            "curl_binary": shutil.which("curl") is not None,
            "configured_host": ssh_cfg.get("host") if ssh_cfg else None,
            "configured_key": bool(ssh_cfg.get("key")) if ssh_cfg else False,
        },
        "manual": {
            "configured_base_url": manual_cfg.get("base_url") if manual_cfg else None,
            "configured_token": bool(manual_cfg.get("bearer_token")) if manual_cfg else False,
        },
    }


def _clean_provider_config(data: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in (data or {}).items():
        if isinstance(value, str):
            value = value.strip()
        if value in ("", None):
            continue
        cleaned[key] = value
    return cleaned


def _azure_cli_logged_in() -> bool:
    az = shutil.which("az")
    if az is None:
        return False
    # Use the resolved path: on Windows, `az` ships as `az.cmd` and a bare
    # `["az", ...]` call goes through CreateProcess which only matches
    # `az.exe`. shutil.which finds the .cmd; passing it directly works.
    # Still wrap in try/except so any other launcher failure (PATH races,
    # broken shims) just reports "not logged in" rather than 500ing the
    # /api/workspace endpoint.
    try:
        proc = subprocess.run(
            [az, "account", "show"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, FileNotFoundError):
        return False
    return proc.returncode == 0


def _azure_auth_present() -> bool:
    if any(
        os.environ.get(name)
        for name in (
            "AZURE_CLIENT_ID",
            "AZURE_CLIENT_SECRET",
            "AZURE_TENANT_ID",
            "ARM_CLIENT_ID",
            "ARM_CLIENT_SECRET",
            "ARM_TENANT_ID",
        )
    ):
        return True
    return _azure_cli_logged_in()


def _preserve_secret_fields(
    new_config: dict[str, Any],
    old_config: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(new_config)
    for key, value in (old_config or {}).items():
        if key in merged:
            continue
        if any(part in key.lower() for part in _SECRET_SUBSTRINGS) and value not in ("", None):
            merged[key] = value
    return merged


def _normalize_workspaces(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
    elif isinstance(value, list):
        items = [str(item).strip() for item in value]
    else:
        raise ValueError("workspaces must be a comma-separated string or array")
    workspaces = [item for item in items if item]
    for item in workspaces:
        if not Path(item).is_absolute():
            raise ValueError(f"pool workspace must be an absolute path: {item}")
    return workspaces


def _validate_and_save_execution_settings(root: Path, body: dict[str, Any]) -> dict[str, Any]:
    from .backends import (
        backend_spec_for_node,
        backend_spec_from_config,
        backend_state_key,
        load_backend,
    )
    from .backends import pool_state
    from .locking import advisory_lock

    backend_name = str(body.get("backend", "")).strip()
    if backend_name not in {"worktree", "pool", "remote"}:
        raise ValueError("backend must be one of: worktree, pool, remote")

    config = load_config(root)
    old_name, old_config = backend_spec_from_config(config)

    if backend_name == "worktree":
        backend_config: dict[str, Any] = {}
    elif backend_name == "pool":
        workspaces = _normalize_workspaces(body.get("workspaces"))
        if not workspaces:
            raise ValueError("pool backend requires at least one absolute workspace path")
        backend_config = {"slots": workspaces}
    else:
        provider = str(body.get("provider", "")).strip()
        if not provider:
            raise ValueError("remote backend requires a provider")
        provider_config = _clean_provider_config(body.get("provider_config") or {})
        if old_name == "remote" and old_config.get("provider") == provider:
            provider_config = _preserve_secret_fields(
                provider_config,
                dict(old_config.get("provider_config", {}) or {}),
            )
        backend_config = {"provider": provider, "provider_config": provider_config}

    with advisory_lock(lock_file_for(graph_path(root))):
        config = load_config(root)
        graph = load_graph(root)
        old_name, old_config = backend_spec_from_config(config)

        load_backend(
            root,
            explicit_name=backend_name,
            explicit_config=backend_config,
        )

        if (backend_name, backend_config) != (old_name, old_config):
            blocking: list[str] = []
            for node_id, node in graph["nodes"].items():
                if node_id == "root":
                    continue
                if node.get("status") not in {"pending", "active", "evaluated", "failed"}:
                    continue
                node_name, node_config = backend_spec_for_node(
                    root,
                    node,
                    workspace_config=config,
                )
                if (node_name, node_config) == (old_name, old_config):
                    blocking.append(node_id)
            if blocking:
                raise RuntimeError(
                    "cannot change workspace default backend while experiments "
                    f"with the old backend are still in flight: {', '.join(blocking)}"
                )

        config["execution_backend"] = backend_name
        if backend_config:
            config["execution_backend_config"] = backend_config
        else:
            config.pop("execution_backend_config", None)
        save_config(root, config)

        if backend_name == "pool":
            pool_state.init_state(
                root,
                list(backend_config.get("slots", [])),
                backend_state_key(backend_name, backend_config),
            )
    return _workspace_summary(root)


def _normalize_runtime_env_settings(body: dict[str, Any]) -> dict[str, Any]:
    inherit_shell = bool(body.get("inherit_shell", True))
    sources = []
    for raw in body.get("dotenv", []) or []:
        if not isinstance(raw, dict):
            continue
        path = str(raw.get("path", "")).strip()
        if not path:
            continue
        mode = str(raw.get("mode", "all")).strip()
        if mode not in {"all", "allow"}:
            raise ValueError("runtime env source mode must be 'all' or 'allow'")
        entry: dict[str, Any] = {"path": path, "mode": mode}
        if mode == "allow":
            keys_raw = raw.get("keys", [])
            if isinstance(keys_raw, str):
                keys = [item.strip() for item in keys_raw.split(",") if item.strip()]
            elif isinstance(keys_raw, list):
                keys = [str(item).strip() for item in keys_raw if str(item).strip()]
            else:
                raise ValueError("runtime env allow keys must be a list or comma-separated string")
            if not keys:
                raise ValueError("runtime env allow mode requires at least one key")
            entry["keys"] = keys
        sources.append(entry)
    return {"inherit_shell": inherit_shell, "dotenv": sources}


def _validate_and_save_runtime_env_settings(root: Path, body: dict[str, Any]) -> dict[str, Any]:
    runtime_env = _normalize_runtime_env_settings(body)
    config = load_config(root)
    config["runtime_env"] = runtime_env
    save_config(root, config)
    return _workspace_summary(root)


def _normalize_runtime_settings(body: dict[str, Any]) -> dict[str, str | None]:
    def clean(name: str) -> str | None:
        value = body.get(name)
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    return {
        "prepare": clean("prepare"),
        "before_run": clean("before_run"),
        "prefix": clean("prefix"),
    }


def _validate_and_save_runtime_settings(root: Path, body: dict[str, Any]) -> dict[str, Any]:
    config = load_config(root)
    config["runtime"] = _normalize_runtime_settings(body)
    save_config(root, config)
    return _workspace_summary(root)


def _normalize_runtime_variables(body: dict[str, Any]) -> dict[str, str]:
    variables: dict[str, str] = {}
    for raw in body.get("variables", []) or []:
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("key", "")).strip()
        if not key:
            continue
        if not key.replace("_", "A").isalnum() or key[0].isdigit():
            raise ValueError(f"invalid environment variable key: {key}")
        variables[key] = str(raw.get("value", ""))
    return variables


def _validate_and_save_runtime_variables(root: Path, body: dict[str, Any]) -> dict[str, Any]:
    from .core import atomic_write_json
    existing = {}
    if runtime_env_values_path(root).exists():
        try:
            existing_payload = json.loads(runtime_env_values_path(root).read_text(encoding="utf-8"))
            existing = dict(existing_payload.get("variables", {}) or {})
        except Exception:
            existing = {}
    variables = {} if body.get("replace") else existing
    for key in body.get("delete_keys", []) or []:
        variables.pop(str(key), None)
    variables.update(_normalize_runtime_variables(body))
    atomic_payload = {"variables": variables}

    atomic_write_json(runtime_env_values_path(root), atomic_payload)
    return _workspace_summary(root)


def _workspace_summary(root: Path) -> dict[str, Any]:
    from .backends import backend_spec_for_node, backend_spec_from_config

    config = load_config(root)
    graph = load_graph(root)
    host = _load_meta(root).get("host")
    default_name, default_config = backend_spec_from_config(config)
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for node in graph["nodes"].values():
        if node["id"] == "root":
            continue
        backend_name, backend_config = backend_spec_for_node(
            root,
            node,
            workspace_config=config,
        )
        key = (backend_name, json.dumps(backend_config or {}, sort_keys=True))
        entry = grouped.setdefault(
            key,
            {
                "name": backend_name,
                "config": dict(backend_config or {}),
                "node_ids": [],
                "active_node_ids": [],
            },
        )
        entry["node_ids"].append(node["id"])
        if node.get("status") == "active":
            entry["active_node_ids"].append(node["id"])
    default_key = (default_name, json.dumps(default_config or {}, sort_keys=True))
    grouped.setdefault(
        default_key,
        {
            "name": default_name,
            "config": dict(default_config or {}),
            "node_ids": [],
            "active_node_ids": [],
        },
    )

    backend_configs: list[dict[str, Any]] = []
    for (name, _), entry in grouped.items():
        cfg = entry["config"]
        backend_configs.append(
            {
                "name": name,
                "provider": cfg.get("provider") if name == "remote" else None,
                "config": _redact_value(cfg),
                "is_default": (name, cfg) == (default_name, default_config),
                "node_ids": sorted(entry["node_ids"]),
                "active_node_ids": sorted(entry["active_node_ids"]),
                "runtime": _backend_runtime_summary(root, name, cfg),
            }
        )
    backend_configs.sort(
        key=lambda item: (
            not item["is_default"],
            item["name"],
            item.get("provider") or "",
            json.dumps(item["config"], sort_keys=True),
        )
    )

    return {
        "project_name": config.get("project_name") or root.name,
        "project_name_source": "configured" if config.get("project_name") else "repo",
        "target": config.get("target", ""),
        "benchmark": config.get("benchmark", ""),
        "gate": config.get("gate"),
        "metric": config.get("metric", "max"),
        "host": host,
        "commit_strategy": config.get("commit_strategy", "all"),
        "frontier_strategy": config.get("frontier_strategy"),
        "runtime": {
            "prepare": (config.get("runtime") or {}).get("prepare"),
            "before_run": (config.get("runtime") or {}).get("before_run"),
            "prefix": (config.get("runtime") or {}).get("prefix"),
        },
        "runtime_env": runtime_env_summary(root, config),
        "keyfile_present": (root / ".evo" / "keyfile").exists(),
        "provider_readiness": _provider_readiness(config),
        "default_backend": {
            "name": default_name,
            "provider": default_config.get("provider") if default_name == "remote" else None,
            "config": _redact_value(default_config),
        },
        "backend_configs": backend_configs,
    }


def create_app(root: Path | None = None) -> Flask:
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    app.config["EVO_ROOT"] = str(root or repo_root())
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    @app.after_request
    def no_cache(response):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    def _root() -> Path:
        return Path(app.config["EVO_ROOT"])

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/api/stats")
    def stats():
        config = load_config(_root())
        graph = load_graph(_root())
        nodes = [node for node in graph["nodes"].values() if node["id"] != "root"]
        metric = config.get("metric", "max")
        effective_counts: dict[str, int] = {}
        for node in nodes:
            status = effective_status(graph, node)
            effective_counts[status] = effective_counts.get(status, 0) + 1
        baseline = None
        for node in graph["nodes"].values():
            if node.get("parent") == "root" and node.get("score") is not None:
                baseline = node["score"]
                break
        return jsonify(
            {
                "metric": metric,
                "project_name": config.get("project_name") or _root().name,
                "target": config.get("target", ""),
                "best_score": best_committed_score(graph, metric),
                "baseline_score": baseline,
                "total_experiments": len(nodes),
                "committed": effective_counts.get("committed", 0),
                "evaluated": effective_counts.get("evaluated", 0),
                "discarded": effective_counts.get("discarded", 0),
                "active": effective_counts.get("active", 0),
                "pending": effective_counts.get("pending", 0),
                "failed": effective_counts.get("failed", 0),
                "pruned": effective_counts.get("pruned", 0),
                "invalidated": effective_counts.get("invalidated", 0),
                "lineage_blocked": effective_counts.get("lineage_blocked", 0),
                "frontier": len(frontier_nodes(graph)),
                "eval_epoch": config.get("current_eval_epoch", 1),
            }
        )

    @app.get("/api/graph")
    def graph():
        root = _root()
        config = load_config(root)
        graph = load_graph(root)
        public_graph = dict(graph)
        public_graph["nodes"] = {
            node_id: _public_node(root, node, workspace_config=config, graph=graph)
            for node_id, node in graph["nodes"].items()
        }
        return jsonify(public_graph)

    @app.get("/api/tree")
    def tree():
        from .core import ascii_tree

        config = load_config(_root())
        return Response(ascii_tree(load_graph(_root()), config.get("metric", "max")), mimetype="text/plain")

    @app.get("/api/scatter")
    def scatter():
        graph = load_graph(_root())
        nodes = [
            {
                "id": node["id"],
                "score": node.get("score"),
                "status": node.get("status"),
                "epoch": node.get("eval_epoch"),
            }
            for node in graph["nodes"].values()
            if node["id"] != "root"
        ]
        return jsonify(nodes)

    @app.get("/api/node/<exp_id>")
    def node(exp_id: str):
        root = _root()
        config = load_config(root)
        graph = load_graph(root)
        return jsonify(_public_node(root, graph["nodes"][exp_id], workspace_config=config, graph=graph))

    @app.post("/api/node/<exp_id>/prune")
    def prune_node(exp_id: str):
        """Mark a node as pruned with a reason. Mirrors `evo prune <exp_id>`.

        Exhausted pruning removes a committed/evaluated leaf from the frontier
        without deleting the commit or disqualifying its score. Invalid pruning
        excludes the node and descendants from best/frontier/ship selection.
        """
        body = request.get_json(silent=True) or {}
        reason = (body.get("reason") or "").strip()
        if not reason:
            return jsonify({"error": "reason is required"}), 400
        prune_kind = (body.get("kind") or PRUNE_KIND_EXHAUSTED).strip()
        if prune_kind not in PRUNE_KINDS:
            return (
                jsonify({"error": "kind must be one of: exhausted, invalid"}),
                400,
            )

        root = _root()
        config = load_config(root)
        graph = load_graph(root)
        if exp_id not in graph["nodes"]:
            return jsonify({"error": f"unknown experiment {exp_id!r}"}), 404
        metric = str(config.get("metric", "max"))
        if (
            prune_kind == PRUNE_KIND_INVALID
            and exp_id in best_spine_ids(graph, metric)
            and not bool(body.get("yes"))
        ):
            return (
                jsonify(
                    {
                        "error": (
                            f"{exp_id} is on the current best valid spine. "
                            f"Confirm before invalidating it."
                        ),
                        "requires_yes": True,
                    }
                ),
                409,
            )
        current_status = graph["nodes"][exp_id].get("status")
        if current_status not in ("committed", "evaluated"):
            return (
                jsonify(
                    {
                        "error": (
                            f"only committed or evaluated nodes can be pruned; "
                            f"{exp_id} is {current_status!r}"
                        )
                    }
                ),
                409,
            )

        def _mark(current_node: dict, _graph: dict) -> None:
            current_node["status"] = "pruned"
            current_node["pruned_reason"] = reason
            current_node["prune_kind"] = prune_kind

        try:
            updated = update_node(root, exp_id, _mark)
        except (KeyError, RuntimeError) as exc:
            return jsonify({"error": str(exc)}), 400
        graph = load_graph(root)
        return jsonify(_public_node(root, updated, workspace_config=config, graph=graph))

    @app.get("/api/workspace")
    def workspace():
        return jsonify(_workspace_summary(_root()))

    @app.post("/api/workspace/execution")
    def workspace_execution():
        body = request.get_json(silent=True) or {}
        try:
            summary = _validate_and_save_execution_settings(_root(), body)
        except (ValueError, RuntimeError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(summary)

    @app.post("/api/workspace/runtime-env")
    def workspace_runtime_env():
        body = request.get_json(silent=True) or {}
        try:
            summary = _validate_and_save_runtime_env_settings(_root(), body)
        except (ValueError, RuntimeError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(summary)

    @app.post("/api/workspace/runtime")
    def workspace_runtime():
        body = request.get_json(silent=True) or {}
        try:
            summary = _validate_and_save_runtime_settings(_root(), body)
        except (ValueError, RuntimeError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(summary)

    @app.post("/api/workspace/runtime-variables")
    def workspace_runtime_variables():
        body = request.get_json(silent=True) or {}
        try:
            summary = _validate_and_save_runtime_variables(_root(), body)
        except (ValueError, RuntimeError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(summary)

    def _latest_attempt_n(root: Path, exp_id: str) -> int | None:
        """Return the highest attempt number on disk for an experiment, or
        None if no attempt directory exists yet. Traces and logs are
        attempt-scoped (`attempts/NNN/...`), so the dashboard always reads
        the most recent attempt."""
        exp_root = experiments_dir_for(root, exp_id) / "attempts"
        if not exp_root.exists():
            return None
        candidates = sorted(
            (int(p.name) for p in exp_root.iterdir() if p.is_dir() and p.name.isdigit()),
            reverse=True,
        )
        return candidates[0] if candidates else None

    @app.get("/api/node/<exp_id>/traces")
    def node_traces(exp_id: str):
        attempt = _latest_attempt_n(_root(), exp_id)
        payload: dict[str, dict] = {}
        if attempt is not None:
            traces_dir = attempt_traces_dir(_root(), exp_id, attempt)
            if traces_dir.exists():
                for path in sorted(traces_dir.glob("*.json")):
                    payload[path.name] = json.loads(path.read_text(encoding="utf-8"))
        return jsonify(payload)

    @app.get("/api/node/<exp_id>/traces/<task_id>")
    def node_task_trace(exp_id: str, task_id: str):
        attempt = _latest_attempt_n(_root(), exp_id)
        if attempt is None:
            return Response(json.dumps(None), status=404, mimetype="application/json")
        trace_path = attempt_traces_dir(_root(), exp_id, attempt) / f"task_{task_id}.json"
        if not trace_path.exists():
            return Response(json.dumps(None), status=404, mimetype="application/json")
        return jsonify(json.loads(trace_path.read_text(encoding="utf-8")))

    @app.get("/api/node/<exp_id>/log/<path:filename>")
    def node_log(exp_id: str, filename: str):
        # Resolve under experiments/<id>/. `path:` accepts forward-slashes
        # so callers can request `attempts/001/benchmark.log` directly.
        # Path traversal is constrained by anchoring under the experiment dir.
        exp_root = experiments_dir_for(_root(), exp_id).resolve()
        target = (exp_root / filename).resolve()
        try:
            target.relative_to(exp_root)
        except ValueError:
            return Response("", status=400, mimetype="text/plain")
        # Auto-redirect bare names (e.g. "benchmark.log") to the latest attempt.
        if not target.exists() and "/" not in filename:
            attempt = _latest_attempt_n(_root(), exp_id)
            if attempt is not None:
                target = attempt_dir(_root(), exp_id, attempt) / filename
        if not target.exists():
            return Response("", mimetype="text/plain")
        # Query params for incremental tailing:
        #   ?tail=N  -> return only the last N lines (cheap when N << file size).
        #   ?offset=M -> return bytes from offset M onward; pair with the
        #                "X-Log-Size" response header so the client can poll
        #                with offset=<prev-size> for a true append-only feed.
        try:
            tail = int(request.args.get("tail", "0"))
        except ValueError:
            tail = 0
        try:
            offset = int(request.args.get("offset", "0"))
        except ValueError:
            offset = 0
        size = target.stat().st_size
        if offset:
            with target.open("rb") as fh:
                fh.seek(min(offset, size))
                data = fh.read()
            text = data.decode("utf-8", errors="replace")
        elif tail > 0:
            # Cheap last-N-lines: read a bounded tail (4 KiB per line cap)
            # then split; avoids loading huge files for a short tail.
            window = min(size, max(tail * 4096, 65536))
            with target.open("rb") as fh:
                fh.seek(max(0, size - window))
                chunk = fh.read()
            text = chunk.decode("utf-8", errors="replace")
            lines = text.splitlines()
            text = "\n".join(lines[-tail:])
        else:
            text = target.read_text(encoding="utf-8")
        resp = Response(text, mimetype="text/plain")
        resp.headers["X-Log-Size"] = str(size)
        return resp

    @app.get("/api/node/<exp_id>/logs")
    def node_logs(exp_id: str):
        # List candidate log files in the latest attempt dir. Returns file
        # name + byte size so the client can pick one and poll incrementally.
        attempt = _latest_attempt_n(_root(), exp_id)
        if attempt is None:
            return jsonify({"attempt": None, "files": []})
        dirpath = attempt_dir(_root(), exp_id, attempt)
        files: list[dict[str, Any]] = []
        if dirpath.exists():
            # Surface .log and .out at the attempt root and one level under
            # logs/ (the convention training scripts typically use).
            patterns = ["*.log", "*.out", "*.jsonl", "logs/*.log", "logs/*.out", "logs/*.jsonl"]
            seen: set[Path] = set()
            for pat in patterns:
                for p in sorted(dirpath.glob(pat)):
                    if p in seen or not p.is_file():
                        continue
                    seen.add(p)
                    files.append({
                        "name": str(p.relative_to(dirpath)),
                        "size": p.stat().st_size,
                        "mtime": p.stat().st_mtime,
                    })
        return jsonify({"attempt": attempt, "files": files})

    @app.get("/api/node/<exp_id>/trackio")
    def node_trackio(exp_id: str):
        # Reads .trackio_url written by the training callback (see
        # posttrainbench-evo/scripts/trl_trackio_callback.py). File format:
        #   url=<space url>
        #   space_id=<owner>/<name>
        #   project=<project>
        #   run_name=<run>
        # If huggingface_hub + pandas + pyarrow are importable AND the
        # corresponding HF Dataset exists, also return the last ~60 rows
        # of the run's metrics for a sparkline. All optional -- if any
        # piece is missing, return what we have and let the client render
        # just the link.
        attempt = _latest_attempt_n(_root(), exp_id)
        if attempt is None:
            return jsonify({"url": None})
        marker = attempt_traces_dir(_root(), exp_id, attempt) / ".trackio_url"
        if not marker.exists():
            return jsonify({"url": None})
        meta: dict[str, str] = {}
        for line in marker.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                meta[k.strip()] = v.strip()
        url = meta.get("url")
        if not url:
            return jsonify({"url": None})
        result: dict[str, Any] = {
            "url": url,
            "space_id": meta.get("space_id"),
            "project": meta.get("project"),
            "run_name": meta.get("run_name"),
            "scalars": None,
        }
        space_id = meta.get("space_id")
        project = meta.get("project")
        run_name = meta.get("run_name")
        if space_id and project:
            try:
                from huggingface_hub import hf_hub_download
                import pandas as pd  # type: ignore
                dataset_id = f"{space_id}-dataset"
                pq_path = hf_hub_download(
                    repo_id=dataset_id,
                    repo_type="dataset",
                    filename=f"{project}.parquet",
                )
                df = pd.read_parquet(pq_path)
                if run_name:
                    df = df[df["run_name"] == run_name]
                # Take last 60 rows for the sparkline; collect numeric cols.
                df = df.sort_values("step").tail(60)
                scalars: dict[str, list[dict[str, float]]] = {}
                for col in df.columns:
                    if col in ("id", "timestamp", "run_name", "step"):
                        continue
                    series = df[col].dropna()
                    if series.empty:
                        continue
                    # Only keep numeric (loss/lr/grad_norm style) cols.
                    try:
                        vals = [float(x) for x in series.tolist()]
                    except (TypeError, ValueError):
                        continue
                    steps = df.loc[series.index, "step"].astype(int).tolist()
                    scalars[col] = [{"step": s, "value": v} for s, v in zip(steps, vals)]
                result["scalars"] = scalars
            except Exception:
                # Any failure (no deps, dataset missing, parquet schema
                # mismatch) -- just return the link, no sparkline.
                pass
        return jsonify(result)

    @app.get("/api/active")
    def active():
        graph = load_graph(_root())
        active_nodes = [node for node in graph["nodes"].values() if node.get("status") == "active"]
        return jsonify(active_nodes)

    @app.get("/api/scratchpad")
    def scratchpad():
        return Response(build_scratchpad(_root()), mimetype="text/plain")

    @app.get("/api/annotations")
    def annotations():
        return jsonify(load_annotations(_root()))

    @app.get("/api/runs")
    def runs():
        return jsonify(list_runs(_root()))

    @app.post("/api/runs/<run_id>/activate")
    def activate_run(run_id: str):
        run_dir = evo_dir(_root()) / run_id
        if not run_dir.exists():
            return jsonify({"error": f"run {run_id} not found"}), 404
        meta = _load_meta(_root())
        meta["active"] = run_id
        _save_meta(_root(), meta)
        return jsonify({"active": run_id})

    @app.get("/api/frontier-strategy")
    def get_frontier_strategy():
        config = load_config(_root())
        return jsonify({
            "registry": FRONTIER_STRATEGIES,
            "current": resolve_from_config(config),
            "default": DEFAULT_FRONTIER_STRATEGY,
        })

    @app.post("/api/frontier-strategy")
    def set_frontier_strategy():
        body = request.get_json(silent=True) or {}
        try:
            normalized = validate_frontier_strategy(body)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        config = load_config(_root())
        config["frontier_strategy"] = normalized
        save_config(_root(), config)
        return jsonify(normalized)

    @app.get("/api/frontier")
    def frontier():
        """Strategy-ranked frontier nodes — same logic as `evo frontier`, minus
        the infra_log append. The dashboard polls this on every tick, so we
        skip the persistence step (the CLI uses it for actual selection events).

        Optional ?seed=N pins stochastic strategies for reproducible polls.
        Default is seed=0 so the rail doesn't shuffle between refreshes."""
        root = _root()
        config = load_config(root)
        graph = load_graph(root)
        raw_nodes = frontier_nodes(graph)
        summaries = [
            {
                "id": n["id"],
                "score": n.get("score"),
                "eval_epoch": n.get("eval_epoch"),
                "hypothesis": n.get("hypothesis"),
            }
            for n in raw_nodes
        ]
        strategy = resolve_from_config(config)
        # pareto_per_task needs per-attempt outcome files for task-level scores.
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
        seed_q = request.args.get("seed", default=0, type=int)
        try:
            ranked, seed_used = frontier_pick(
                summaries, strategy, metric, outcomes=outcomes, seed=seed_q,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        payload = {
            "strategy": strategy,
            "generated_at": utc_now(),
            "picks": ranked,
            "all_ids": [n["id"] for n in raw_nodes],
        }
        if strategy["kind"] in {"epsilon_greedy", "softmax", "pareto_per_task"}:
            payload["seed"] = seed_used
        return jsonify(payload)

    @app.post("/api/direct")
    def api_direct():
        """Queue an `evo direct` directive from the dashboard.

        Body: {"text": str, "from_exp_id"?: str, "exp_id"?: str}

        Without `exp_id`, the event is workspace-scoped (broadcast to orchestrators).
        With `exp_id`, the event is routed to that specific subagent.
        If `from_exp_id` is set on a workspace broadcast, the text is wrapped to
        instruct the orchestrator to spawn a child of that node.
        """
        from .inject import marker, queue
        from .inject.registry import list_active_sessions

        body = request.get_json(silent=True) or {}
        text = (body.get("text") or "").strip()
        if not text:
            return jsonify({"error": "text is required"}), 400

        target_exp = body.get("exp_id")
        from_exp = body.get("from_exp_id")
        root = _root()

        if target_exp:
            event_id = queue.append_exp_event(root, target_exp, text)
            # Fan out to per-session markers for any registered subagent
            # whose exp_id matches. See cmd_direct in cli.py for the
            # rationale — the Rust/host gates key on sid, not exp_id, so
            # touching markers/<exp_id>.flag alone wakes zero drains.
            sessions = list_active_sessions(root)
            woken = 0
            for sess in sessions:
                if sess.get("exp_id") != target_exp:
                    continue
                sid = sess.get("session_id")
                if not sid:
                    continue
                marker.touch(root, sid)
                woken += 1
            # Keep the legacy exp_id-keyed marker for back-compat.
            marker.touch(root, target_exp)
            return jsonify({
                "event_id": event_id,
                "kind": "targeted",
                "exp_id": target_exp,
                "fanout": woken,
            })

        mode = (body.get("mode") or "spawn").strip().lower()
        if from_exp:
            if mode == "retry":
                text = (
                    f"Run another attempt of {from_exp}. "
                    f"Notes from dashboard: {text}"
                )
            else:
                text = (
                    f"Start a new experiment from {from_exp}. "
                    f"Notes from dashboard: {text}"
                )
        event_id = queue.append_workspace_event(root, text)
        delivered = 0
        skipped_subagent = 0
        skipped_unengaged = 0
        # Engagement filter mirrors cli.py::cmd_direct — see that module
        # for HOSTS_WITH_ENGAGEMENT. Imported here to keep the set in
        # one place.
        from .cli import HOSTS_WITH_ENGAGEMENT
        for sess in list_active_sessions(root):
            if sess.get("exp_id"):
                skipped_subagent += 1
                continue
            if (
                sess.get("host") in HOSTS_WITH_ENGAGEMENT
                and not sess.get("has_evo_engaged")
            ):
                skipped_unengaged += 1
                continue
            sid = sess.get("session_id")
            if not sid:
                continue
            marker.touch(root, sid)
            delivered += 1
        return jsonify({
            "event_id": event_id,
            "kind": "broadcast",
            "fanout": delivered,
            "skipped_unengaged": skipped_unengaged,
            "skipped_subagent": skipped_subagent,
            "from_exp_id": from_exp,
        })

    return app


def main() -> None:
    import os
    port = int(os.environ.get("EVO_DASHBOARD_PORT", "8080"))
    host = os.environ.get("EVO_DASHBOARD_HOST", "127.0.0.1")
    app = create_app()
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
