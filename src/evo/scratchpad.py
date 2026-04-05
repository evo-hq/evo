from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .core import (
    best_committed_score,
    frontier_nodes,
    graph_path,
    infra_path,
    load_annotations,
    load_config,
    load_graph,
    notes_path,
    scratchpad_path,
)


def _truncate(text: str, limit: int = 240) -> str:
    compact = " ".join(text.strip().split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def build_scratchpad(root: Path) -> str:
    config = load_config(root)
    graph = load_graph(root)
    annotations = load_annotations(root).get("annotations", [])
    infra = json.loads(infra_path(root).read_text(encoding="utf-8")).get("events", []) if infra_path(root).exists() else []
    notes = notes_path(root).read_text(encoding="utf-8") if notes_path(root).exists() else ""
    metric = config.get("metric", "max")
    committed = [node for node in graph["nodes"].values() if node.get("status") == "committed"]
    discarded = [node for node in graph["nodes"].values() if node.get("status") == "discarded"]
    active = [node for node in graph["nodes"].values() if node.get("status") == "active"]
    best = best_committed_score(graph, metric)
    frontier = frontier_nodes(graph)
    recent = sorted(
        [node for node in graph["nodes"].values() if node["id"] != "root"],
        key=lambda item: item.get("updated_at", ""),
        reverse=True,
    )[:8]

    lines = [
        "# Scratchpad",
        "",
        "## Status",
        f"- Metric: `{metric}`",
        f"- Current eval epoch: `{config.get('current_eval_epoch', 1)}`",
        f"- Best score: `{best}`",
        f"- Total experiments: `{len(graph['nodes']) - 1}`",
        f"- Committed: `{len(committed)}`",
        f"- Discarded: `{len(discarded)}`",
        f"- Active workers: `{len(active)}`",
        "",
        "## Frontier",
    ]
    if frontier:
        for node in frontier[:10]:
            lines.append(f"- `{node['id']}` score=`{node.get('score')}` epoch=`{node.get('eval_epoch')}` {node.get('hypothesis','')}")
    else:
        lines.append("- No frontier nodes yet.")

    lines.extend(["", "## Recent Experiments"])
    if recent:
        for node in recent:
            lines.append(
                f"- `{node['id']}` `{node.get('status')}` score=`{node.get('score')}` {node.get('hypothesis','')}"
            )
    else:
        lines.append("- No experiments yet.")

    lines.extend(["", "## Annotations"])
    if annotations:
        for entry in annotations[-10:]:
            task = entry.get("task_id") or "global"
            lines.append(f"- task `{task}` / `{entry['experiment_id']}`: {_truncate(entry['analysis'])}")
    else:
        lines.append("- No annotations yet.")

    lines.extend(["", "## What Not To Try"])
    if discarded:
        for node in discarded[-10:]:
            lines.append(f"- `{node['id']}`: {_truncate(node.get('hypothesis', ''))}")
    else:
        lines.append("- No discarded hypotheses yet.")

    lines.extend(["", "## Infrastructure Log"])
    if infra:
        for event in infra[-8:]:
            suffix = " (breaking)" if event.get("breaking") else ""
            lines.append(f"- {event['timestamp']}: {event['message']}{suffix}")
    else:
        lines.append("- No infrastructure events yet.")

    lines.extend(["", "## Notes"])
    lines.append(_truncate(notes, limit=1200) if notes.strip() else "No notes yet.")
    lines.append("")
    return "\n".join(lines)


def write_scratchpad(root: Path) -> str:
    content = build_scratchpad(root)
    scratchpad_path(root).write_text(content, encoding="utf-8")
    return content
