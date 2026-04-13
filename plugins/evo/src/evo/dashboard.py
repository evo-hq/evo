from __future__ import annotations

import json
from pathlib import Path

from flask import Flask, Response, jsonify, send_from_directory

from .core import (
    _load_meta,
    _save_meta,
    best_committed_score,
    evo_dir,
    experiments_dir_for,
    frontier_nodes,
    infra_path,
    list_runs,
    load_annotations,
    load_config,
    load_graph,
    notes_path,
    repo_root,
    scratchpad_path,
)
from .scratchpad import write_scratchpad

STATIC_DIR = Path(__file__).parent / "static"


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
        return jsonify(load_graph(_root()))

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
        return jsonify(load_graph(_root())["nodes"][exp_id])

    @app.get("/api/node/<exp_id>/traces")
    def node_traces(exp_id: str):
        traces_dir = experiments_dir_for(_root(), exp_id) / "traces"
        payload = {}
        if traces_dir.exists():
            for path in sorted(traces_dir.glob("*.json")):
                payload[path.name] = json.loads(path.read_text(encoding="utf-8"))
        return jsonify(payload)

    @app.get("/api/node/<exp_id>/traces/<task_id>")
    def node_task_trace(exp_id: str, task_id: str):
        trace_path = experiments_dir_for(_root(), exp_id) / "traces" / f"task_{task_id}.json"
        return jsonify(json.loads(trace_path.read_text(encoding="utf-8")))

    @app.get("/api/node/<exp_id>/log/<filename>")
    def node_log(exp_id: str, filename: str):
        path = experiments_dir_for(_root(), exp_id) / filename
        if not path.exists():
            return Response("", mimetype="text/plain")
        return Response(path.read_text(encoding="utf-8"), mimetype="text/plain")

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

    return app


def main() -> None:
    import os
    port = int(os.environ.get("EVO_DASHBOARD_PORT", "8080"))
    app = create_app()
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
