from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .core import (
    ascii_tree,
    best_committed_node,
    best_committed_score,
    collect_gates_from_path,
    experiments_path,
    frontier_nodes,
    graph_path,
    infra_path,
    load_annotations,
    load_config,
    load_graph,
    notes_path,
    path_to_node,
    scratchpad_path,
)


def _truncate(text: str, limit: int = 240) -> str:
    compact = " ".join(text.strip().split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _diff_summary(root: Path, exp_id: str, attempt: int) -> str | None:
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
                files.append(parts[3].lstrip("b/"))
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    if not files:
        return None
    file_str = ", ".join(files[:3])
    if len(files) > 3:
        file_str += f" (+{len(files) - 3} more)"
    return f"{file_str} (+{added}/-{removed})"


def _group_annotations_by_task(annotations: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """Group annotations by task_id, keeping only the latest per task."""
    latest: dict[str, dict[str, Any]] = {}
    for entry in annotations:
        task = entry.get("task_id") or "global"
        existing = latest.get(task)
        if existing is None or entry.get("timestamp", "") >= existing.get("timestamp", ""):
            latest[task] = entry
    return sorted(latest.items(), key=lambda item: item[0])


def _dedup_discarded(discarded: list[dict[str, Any]], limit: int = 15) -> list[tuple[str, int]]:
    """Deduplicate discarded hypotheses by normalized text. Returns (hypothesis, count) pairs."""
    counts: dict[str, int] = {}
    display: dict[str, str] = {}
    for node in discarded:
        hyp = node.get("hypothesis", "")
        key = " ".join(hyp.lower().split())
        counts[key] = counts.get(key, 0) + 1
        display[key] = hyp  # keep the original casing from the latest
    sorted_items = sorted(counts.items(), key=lambda item: -item[1])
    return [(display[key], count) for key, count in sorted_items[:limit]]


def build_scratchpad(root: Path) -> str:
    config = load_config(root)
    graph = load_graph(root)
    annotations = load_annotations(root).get("annotations", [])
    infra = json.loads(infra_path(root).read_text(encoding="utf-8")).get("events", []) if infra_path(root).exists() else []
    notes = notes_path(root).read_text(encoding="utf-8") if notes_path(root).exists() else ""
    metric = config.get("metric", "max")
    committed = [node for node in graph["nodes"].values() if node.get("status") == "committed"]
    discarded = [node for node in graph["nodes"].values() if node.get("status") == "discarded"]
    evaluated = [node for node in graph["nodes"].values() if node.get("status") == "evaluated"]
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
        f"- Evaluated (awaiting decision): `{len(evaluated)}`",
        f"- Discarded: `{len(discarded)}`",
        f"- Active workers: `{len(active)}`",
    ]

    # Tree
    lines.extend(["", "## Tree", "```"])
    lines.append(ascii_tree(graph, metric))
    lines.extend(["```"])

    # Best path
    best_node = best_committed_node(graph, metric)
    if best_node and best_node["id"] != "root":
        chain = path_to_node(graph, best_node["id"])
        lines.extend(["", "## Best Path"])
        path_parts = []
        for node in chain:
            if node["id"] == "root":
                path_parts.append("root")
            else:
                score_str = f" ({node.get('score')})" if node.get("score") is not None else ""
                path_parts.append(f"{node['id']}{score_str}")
        lines.append(" -> ".join(path_parts))

    # Frontier
    lines.extend(["", "## Frontier"])
    if frontier:
        for node in frontier[:10]:
            lines.append(f"- `{node['id']}` score=`{node.get('score')}` epoch=`{node.get('eval_epoch')}` {node.get('hypothesis','')}")
    else:
        lines.append("- No frontier nodes yet.")

    if evaluated:
        lines.extend(["", "## Awaiting Decision"])
        lines.append("These nodes ran but neither committed nor discarded. Retry (edit + `evo run`) or abandon (`evo discard --reason`).")
        for node in evaluated:
            attempts = int(node.get("evaluated_attempts", 0))
            lines.append(
                f"- `{node['id']}` score=`{node.get('score')}` attempts=`{attempts}` "
                f"gate_failed=`{node.get('gate_failures') or []}` {node.get('hypothesis','')}"
            )

    # Gates
    # Show gates from root (always active) + any unique gates on frontier nodes
    root_gates = graph["nodes"].get("root", {}).get("gates", [])
    if root_gates or any(n.get("gates") for n in frontier):
        lines.extend(["", "## Gates"])
        if root_gates:
            for g in root_gates:
                lines.append(f"- `{g['name']}` (root): `{_truncate(g['command'], 120)}`")
        seen_names = {g["name"] for g in root_gates}
        for node in frontier[:10]:
            effective = collect_gates_from_path(graph, node["id"])
            for g in effective:
                if g["name"] not in seen_names:
                    seen_names.add(g["name"])
                    lines.append(f"- `{g['name']}` (from tree): `{_truncate(g['command'], 120)}`")

    # Recent experiments
    lines.extend(["", "## Recent Experiments"])
    if recent:
        for node in recent:
            lines.append(
                f"- `{node['id']}` `{node.get('status')}` score=`{node.get('score')}` {node.get('hypothesis','')}"
            )
    else:
        lines.append("- No experiments yet.")

    # Recent diffs
    recent_committed = [n for n in recent if n.get("status") == "committed" and n["id"] != "root"][:5]
    if recent_committed:
        lines.extend(["", "## Recent Diffs"])
        for node in recent_committed:
            summary = _diff_summary(root, node["id"], int(node.get("current_attempt", 0)))
            if summary:
                lines.append(f"- `{node['id']}`: {summary}")

    # Annotations grouped by task
    lines.extend(["", "## Annotations"])
    if annotations:
        grouped = _group_annotations_by_task(annotations)
        for task_id, entry in grouped:
            lines.append(f"- task `{task_id}` / `{entry['experiment_id']}`: {_truncate(entry['analysis'])}")
    else:
        lines.append("- No annotations yet.")

    # What Not To Try (deduplicated)
    lines.extend(["", "## What Not To Try"])
    if discarded:
        deduped = _dedup_discarded(discarded)
        for hyp, count in deduped:
            suffix = f" (x{count})" if count > 1 else ""
            lines.append(f"- {_truncate(hyp)}{suffix}")
    else:
        lines.append("- No discarded hypotheses yet.")

    # Infrastructure log
    lines.extend(["", "## Infrastructure Log"])
    if infra:
        for event in infra[-8:]:
            suffix = " (breaking)" if event.get("breaking") else ""
            # 0.3.0 frontier events shipped with key "at" and no "message"
            # (#22). Read tolerantly so workspaces upgrading to >=0.3.1 don't
            # KeyError on the pre-existing bad events still in their log.
            ts = event.get("timestamp") or event.get("at") or "?"
            msg = event.get("message") or f"{event.get('kind', '?')} event"
            lines.append(f"- {ts}: {msg}{suffix}")
    else:
        lines.append("- No infrastructure events yet.")

    # Notes
    lines.extend(["", "## Notes"])
    lines.append(_truncate(notes, limit=1200) if notes.strip() else "No notes yet.")
    lines.append("")
    return "\n".join(lines)


def write_scratchpad(root: Path) -> str:
    content = build_scratchpad(root)
    scratchpad_path(root).write_text(content, encoding="utf-8")
    return content
