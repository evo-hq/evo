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
    """Collects logs and eval results, then emits a final score.

    Two separate concerns:

    - **log(task_id, data)** -- observability. Append anything (str, dict,
      whatever) as the task runs. Called many times per task.
    - **report(task_id, score)** -- evaluation. Record the final score for
      a task. Called once per task.

    Usage::

        from evo_sdk import Run

        with Run() as run:
            run.log("0", "starting task")
            run.log("0", {"role": "user", "content": "hello"})
            run.log("0", {"role": "assistant", "content": "hi"})
            run.report("0", score=1.0, summary="completed")
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
        self._logs: dict[str, list[Any]] = {}
        self._lock = threading.Lock()
        self._started_at = _utc_now()
        self._finished = False

    def log(self, task_id: str, data: Any) -> None:
        """Append a log entry to a task. Can be called many times.

        *data* can be anything -- a string, a dict, a number. The SDK
        doesn't interpret it; it's stored as-is in the trace's ``log``
        array.
        """
        task_id = str(task_id)
        with self._lock:
            self._logs.setdefault(task_id, []).append(data)

    def report(
        self,
        task_id: str,
        score: float,
        *,
        status: str | None = None,
        pass_threshold: float = 0.5,
        summary: str | None = None,
        failure_reason: str | None = None,
        cost: dict[str, Any] | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
        artifacts: dict[str, str] | None = None,
        **extra: Any,
    ) -> None:
        """Record the eval result for a task and write its trace.

        This flushes any accumulated ``log()`` entries for this task into
        the trace file alongside the eval fields.
        """
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
            logs = self._logs.get(task_id)
            if logs:
                trace["log"] = list(logs)

        self._backend.write_trace(trace)

    def finish(self, *, score: float | None = None) -> dict[str, Any]:
        """Emit the final result to stdout and return it.

        If *score* is not provided, computes the mean of all reported tasks.
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
