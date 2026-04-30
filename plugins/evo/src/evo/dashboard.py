from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request, send_from_directory

from .core import (
    _load_meta,
    _save_meta,
    attempt_dir,
    attempt_traces_dir,
    best_committed_score,
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
    notes_path,
    repo_root,
    save_config,
    scratchpad_path,
)
from .frontier_strategies import (
    DEFAULT_FRONTIER_STRATEGY,
    FRONTIER_STRATEGIES,
    resolve_from_config,
    validate_frontier_strategy,
)
from .scratchpad import write_scratchpad

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


def _public_node(
    root: Path,
    node: dict[str, Any],
    *,
    workspace_config: dict[str, Any],
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
    return public


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
    cloudflare_auth_present = bool(os.environ.get("SANDBOX_API_KEY")) or bool(provider_config.get("api_key"))
    cloudflare_url_present = bool(os.environ.get("SANDBOX_API_URL")) or bool(provider_config.get("api_url"))
    hetzner_auth_present = bool(os.environ.get("HCLOUD_TOKEN")) or bool(provider_config.get("token"))
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
        "cloudflare": {
            "bridge_configured": cloudflare_url_present,
            "auth_present": cloudflare_auth_present,
            "auth_source": (
                "workspace-config"
                if provider == "remote" and provider_config.get("api_key")
                else ("env" if os.environ.get("SANDBOX_API_KEY") else "missing")
            ),
        },
        "hetzner": {
            "sdk_installed": _module_available("hcloud"),
            "auth_present": hetzner_auth_present,
            "auth_source": (
                "env"
                if os.environ.get("HCLOUD_TOKEN")
                else ("workspace-config" if provider_config.get("token") else "missing")
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
        "target": config.get("target", ""),
        "benchmark": config.get("benchmark", ""),
        "gate": config.get("gate"),
        "metric": config.get("metric", "max"),
        "host": host,
        "commit_strategy": config.get("commit_strategy", "all"),
        "frontier_strategy": config.get("frontier_strategy"),
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
        baseline = None
        for node in graph["nodes"].values():
            if node.get("parent") == "root" and node.get("score") is not None:
                baseline = node["score"]
                break
        return jsonify(
            {
                "metric": metric,
                "target": config.get("target", ""),
                "best_score": best_committed_score(graph, metric),
                "baseline_score": baseline,
                "total_experiments": len(nodes),
                "committed": sum(1 for node in nodes if node.get("status") == "committed"),
                "discarded": sum(1 for node in nodes if node.get("status") == "discarded"),
                "active": sum(1 for node in nodes if node.get("status") == "active"),
                "failed": sum(1 for node in nodes if node.get("status") == "failed"),
                "pruned": sum(1 for node in nodes if node.get("status") == "pruned"),
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
            node_id: _public_node(root, node, workspace_config=config)
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
        return jsonify(_public_node(root, load_graph(root)["nodes"][exp_id], workspace_config=config))

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
        return Response(target.read_text(encoding="utf-8"), mimetype="text/plain")

    @app.get("/api/active")
    def active():
        graph = load_graph(_root())
        active_nodes = [node for node in graph["nodes"].values() if node.get("status") == "active"]
        return jsonify(active_nodes)

    @app.get("/api/scratchpad")
    def scratchpad():
        return Response(write_scratchpad(_root()), mimetype="text/plain")

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

    return app


def main() -> None:
    import os
    port = int(os.environ.get("EVO_DASHBOARD_PORT", "8080"))
    app = create_app()
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
