"""Inline instrumentation for Python benchmarks. Paste into the benchmark
and call `log_task()` per task + `write_result()` once at the end.

Contract:
- Reads EVO_TRACES_DIR, EVO_EXPERIMENT_ID, EVO_RESULT_PATH from env.
- Writes traces/task_<id>.json per task.
- Writes the final result JSON to EVO_RESULT_PATH, or stdout if unset.
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
_RESULT_PATH = os.environ.get("EVO_RESULT_PATH")
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
    in the final result JSON for downstream selection strategies.
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
    """Write the final score JSON to $EVO_RESULT_PATH (or stdout if unset)
    and return the score. The return lets callers gate on --min-score
    without recomputing the aggregate.
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
    payload = json.dumps(result, indent=2)
    if _RESULT_PATH:
        target = Path(_RESULT_PATH)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Claim + tmp+rename: duplicate writers fail-fast; crash mid-publish
        # leaves an empty file (caught by load_result) not a partial write.
        try:
            os.close(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            raise RuntimeError(
                f"{target} already exists; only one write_result() per attempt"
            ) from None
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, target)
    else:
        print(payload)
    return score
