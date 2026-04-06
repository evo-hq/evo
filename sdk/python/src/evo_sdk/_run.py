"""Run -- benchmark reporting context."""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Any

from ._backend import Backend, default_backend


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Run:
    """Collects per-task results and emits a final score.

    Usage::

        from evo_sdk import Run

        with Run() as run:
            run.log_task("0", score=1.0, events=[...])
            run.log_task("1", score=0.0, failure_reason="wrong_action")
        # finish() called automatically, prints score JSON to stdout
    """

    def __init__(
        self,
        *,
        experiment_id: str | None = None,
        backend: Backend | None = None,
    ) -> None:
        self._experiment_id = (
            experiment_id
            or os.environ.get("EVO_EXPERIMENT_ID")
            or "unknown"
        )
        self._backend = backend or default_backend()
        self._backend.setup(
            traces_dir=os.environ.get("EVO_TRACES_DIR"),
            experiment_id=self._experiment_id,
        )
        self._tasks: dict[str, float] = {}
        self._lock = threading.Lock()
        self._started_at = _utc_now()
        self._finished = False

    def log_task(
        self,
        task_id: str,
        score: float,
        *,
        status: str | None = None,
        pass_threshold: float = 0.5,
        summary: str | None = None,
        failure_reason: str | None = None,
        events: list[dict[str, Any]] | None = None,
        cost: dict[str, Any] | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
        artifacts: dict[str, str] | None = None,
        **extra: Any,
    ) -> None:
        """Record a single task result and write its trace immediately."""
        task_id = str(task_id)
        if status is None:
            status = "passed" if score >= pass_threshold else "failed"

        trace: dict[str, Any] = {
            "experiment_id": self._experiment_id,
            "task_id": task_id,
            "status": status,
            "score": score,
        }
        if summary is not None:
            trace["summary"] = summary
        if failure_reason is not None:
            trace["failure_reason"] = failure_reason
        if events is not None:
            trace["events"] = events
        if cost is not None:
            trace["cost"] = cost
        if started_at is not None:
            trace["started_at"] = started_at
        if ended_at is not None:
            trace["ended_at"] = ended_at
        if artifacts is not None:
            trace["artifacts"] = artifacts
        if extra:
            trace.update(extra)

        with self._lock:
            self._tasks[task_id] = score

        self._backend.write_trace(trace)

    def finish(self, *, score: float | None = None) -> dict[str, Any]:
        """Emit the final result to stdout and return it.

        If *score* is not provided, computes the mean of all logged tasks.
        """
        if self._finished:
            return {}
        self._finished = True

        if score is None:
            if not self._tasks:
                score = 0.0
            else:
                score = sum(self._tasks.values()) / len(self._tasks)

        result: dict[str, Any] = {
            "score": round(score, 4),
            "tasks": dict(self._tasks),
        }
        self._backend.emit_result(result)
        return result

    # -- context manager --------------------------------------------------

    def __enter__(self) -> Run:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if exc_type is None and not self._finished:
            self.finish()
