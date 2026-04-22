"""Inline instrumentation template for Python benchmarks.

Use this when the user declines the `evo-agent` SDK. Paste the helper into
the benchmark script and call `log_task()` + `write_result()` in place of
the SDK's `Run` context manager. Zero new dependencies.

Contract (same as the SDK):
- Read EVO_TRACES_DIR and EVO_EXPERIMENT_ID from the environment.
- Write task_<id>.json files into EVO_TRACES_DIR as each task finishes.
- Print a single JSON object with a "score" field to stdout at the end.
- All other output goes to stderr.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_TRACES_DIR = Path(os.environ["EVO_TRACES_DIR"]) if os.environ.get("EVO_TRACES_DIR") else None
_EXPERIMENT_ID = os.environ.get("EVO_EXPERIMENT_ID", "unknown")
_SCORES: dict[str, float] = {}
_TASK_META: dict[str, dict[str, Any]] = {}
_STARTED_AT = datetime.now(timezone.utc).isoformat(timespec="seconds")

if _TRACES_DIR:
    _TRACES_DIR.mkdir(parents=True, exist_ok=True)


def log_task(
    task_id: str,
    score: float,
    *,
    summary: str | None = None,
    failure_reason: str | None = None,
    log: list[Any] | None = None,
    direction: str | None = None,
    **extra: Any,
) -> None:
    """Record the result for one task. Writes task_<id>.json immediately.

    *direction* is "max" (higher is better, default) or "min" (lower is
    better, e.g. latency). Only set it when this task's direction differs
    from the benchmark's top-level `--metric`. Propagates to `tasks_meta`
    in the final stdout JSON for downstream selection strategies.
    """
    task_id = str(task_id)
    if direction is not None and direction not in ("max", "min"):
        raise ValueError(f"direction must be 'max' or 'min', got {direction!r}")
    _SCORES[task_id] = score
    if direction is not None:
        _TASK_META[task_id] = {"direction": direction}
    if _TRACES_DIR is None:
        return
    trace: dict[str, Any] = {
        "experiment_id": _EXPERIMENT_ID,
        "task_id": task_id,
        "status": "passed" if score >= 0.5 else "failed",
        "score": score,
        "ended_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if direction is not None:
        trace["direction"] = direction
    if summary is not None:
        trace["summary"] = summary
    if failure_reason is not None:
        trace["failure_reason"] = failure_reason
    if log is not None:
        trace["log"] = log
    trace.update(extra)
    (_TRACES_DIR / f"task_{task_id}.json").write_text(
        json.dumps(trace, indent=2), encoding="utf-8"
    )


def write_result(score: float | None = None) -> float:
    """Emit the final score JSON to stdout and return the score.

    The return value lets callers implement --min-score gate logic without
    recomputing the aggregate.
    """
    if score is None:
        score = sum(_SCORES.values()) / len(_SCORES) if _SCORES else 0.0
    score = round(score, 4)
    result = {
        "score": score,
        "tasks": dict(_SCORES),
        "started_at": _STARTED_AT,
        "ended_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if _TASK_META:
        result["tasks_meta"] = {k: dict(v) for k, v in _TASK_META.items()}
    print(json.dumps(result, indent=2))
    return score
