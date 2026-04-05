from __future__ import annotations

import json
from pathlib import Path

from flask import Flask, Response, jsonify

from .core import (
    best_committed_score,
    experiments_dir_for,
    frontier_nodes,
    infra_path,
    load_annotations,
    load_config,
    load_graph,
    notes_path,
    repo_root,
    scratchpad_path,
)
from .scratchpad import write_scratchpad


def create_app(root: Path | None = None) -> Flask:
    app = Flask(__name__)
    app.config["EVO_ROOT"] = str(root or repo_root())

    def _root() -> Path:
        return Path(app.config["EVO_ROOT"])

    @app.get("/")
    def index() -> str:
        return """<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>evo</title>
    <style>
      body { font-family: sans-serif; margin: 24px; background: #f7f4ea; color: #1f1f1f; }
      .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
      pre, code { background: #fffdf7; border: 1px solid #d8d0bf; padding: 12px; overflow: auto; }
      .chips { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 24px; }
      .chip { background: #1f1f1f; color: #fff; padding: 8px 12px; border-radius: 999px; }
    </style>
  </head>
  <body>
    <h1>evo</h1>
    <div class="chips" id="stats"></div>
    <div class="grid">
      <section>
        <h2>Tree</h2>
        <pre id="tree"></pre>
      </section>
      <section>
        <h2>Scratchpad</h2>
        <pre id="scratchpad"></pre>
      </section>
    </div>
    <script>
      async function refresh() {
        const [stats, tree, scratchpad] = await Promise.all([
          fetch('/api/stats').then(r => r.json()),
          fetch('/api/tree').then(r => r.text()),
          fetch('/api/scratchpad').then(r => r.text())
        ]);
        document.getElementById('stats').innerHTML = `
          <div class="chip">metric: ${stats.metric}</div>
          <div class="chip">best: ${stats.best_score ?? 'n/a'}</div>
          <div class="chip">experiments: ${stats.total_experiments}</div>
          <div class="chip">committed: ${stats.committed}</div>
          <div class="chip">discarded: ${stats.discarded}</div>
          <div class="chip">active: ${stats.active}</div>
        `;
        document.getElementById('tree').textContent = tree;
        document.getElementById('scratchpad').textContent = scratchpad;
      }
      refresh();
      setInterval(refresh, 5000);
    </script>
  </body>
</html>"""

    @app.get("/api/stats")
    def stats():
        config = load_config(_root())
        graph = load_graph(_root())
        nodes = [node for node in graph["nodes"].values() if node["id"] != "root"]
        metric = config.get("metric", "max")
        return jsonify(
            {
                "metric": metric,
                "best_score": best_committed_score(graph, metric),
                "total_experiments": len(nodes),
                "committed": sum(1 for node in nodes if node.get("status") == "committed"),
                "discarded": sum(1 for node in nodes if node.get("status") == "discarded"),
                "active": sum(1 for node in nodes if node.get("status") == "active"),
                "failed": sum(1 for node in nodes if node.get("status") == "failed"),
                "pruned": sum(1 for node in nodes if node.get("status") == "pruned"),
                "frontier": len(frontier_nodes(graph)),
            }
        )

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

    return app


def main() -> None:
    app = create_app()
    app.run(host="127.0.0.1", port=8080, debug=False)


if __name__ == "__main__":
    main()
