"""Run -- benchmark reporting context."""

from __future__ import annotations

import os
import threading
import weakref
from datetime import datetime, timezone
from typing import Any

from ._backend import Backend, default_backend


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Belt-and-suspenders against concurrent in-process Runs for the same
# experiment. The harness-level guard (evo run's PID stamp) catches the
# common case where a second `evo run` is invoked while the first driver
# is alive; this registry catches the rarer in-process variant where a
# single benchmark accidentally instantiates two Run() objects with the
# same EVO_EXPERIMENT_ID (e.g. test-runner re-import, shared eval loop).
# Without it, both Run instances would write the same EVO_RESULT_PATH;
# the second would raise via emit_result's O_EXCL, but only after both
# benchmarks finished and discovered the collision at the very end.
_ACTIVE_RUNS: set[str] = set()
_ACTIVE_RUNS_LOCK = threading.Lock()


def _release_active_run(experiment_id: str) -> None:
    """Release a registry slot. Wired via weakref.finalize so it runs even
    if the Run is gc'd without finish()/__exit__ (e.g. an exception path
    that leaks the object). Idempotent — finish()/__exit__ also discard."""
    with _ACTIVE_RUNS_LOCK:
        _ACTIVE_RUNS.discard(experiment_id)


class Run:
    """Collects logs and eval results, then emits a final score.

    Two separate concerns:

    - **log(task_id, data)** -- observability. Append anything (str, dict,
      whatever) as the task runs. Called many times per task.
    - **report(task_id, score)** -- evaluation. Record the final score for
      a task. Called once per task.

    Usage::

        from evo_agent import Run

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
        with _ACTIVE_RUNS_LOCK:
            if self._experiment_id in _ACTIVE_RUNS:
                raise RuntimeError(
                    f"Run({self._experiment_id!r}) is already active in this "
                    f"process. Only one Run per experiment_id at a time; "
                    f"finish() or exit the existing context first."
                )
            _ACTIVE_RUNS.add(self._experiment_id)
        # Safety net: if the caller leaks this Run (no finish(), no `with`),
        # release the slot when the object is gc'd so a retry isn't blocked.
        self._finalizer = weakref.finalize(
            self, _release_active_run, self._experiment_id
        )
        self._backend = backend or default_backend()
        self._backend.setup(
            traces_dir=os.environ.get("EVO_TRACES_DIR"),
            experiment_id=self._experiment_id,
        )
        self._tasks: dict[str, float] = {}
        self._task_meta: dict[str, dict[str, Any]] = {}
        self._task_started: dict[str, str] = {}
        self._logs: dict[str, list[Any]] = {}
        self._lock = threading.Lock()
        self._started_at = _utc_now()
        self._finished = False

    def log(self, task_id: str, data: Any) -> None:
        """Append a log entry to a task. Can be called many times.

        *data* can be anything -- a string, a dict, a number. The SDK
        doesn't interpret it; it's stored as-is in the trace's ``log``
        array.  The first ``log()`` call for a task records its start
        time (used as ``started_at`` in the trace if not overridden).
        """
        task_id = str(task_id)
        now = _utc_now()
        with self._lock:
            if task_id not in self._task_started:
                self._task_started[task_id] = now
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
        direction: str | None = None,
        **extra: Any,
    ) -> None:
        """Record the eval result for a task and write its trace.

        *direction* is either ``"max"`` (higher score is better, default)
        or ``"min"`` (lower is better, e.g. latency). Only needs to be set
        when this task's direction differs from the benchmark's top-level
        ``--metric``. Propagates to ``tasks_meta`` in the final result so
        downstream selection strategies can interpret scores correctly.

        Timestamps are filled automatically when not provided:

        - ``ended_at`` defaults to *now*.
        - ``started_at`` defaults to the time of the first ``log()``
          call for this task, or the Run's creation time.

        This flushes any accumulated ``log()`` entries for this task into
        the trace file alongside the eval fields.
        """
        task_id = str(task_id)
        now = _utc_now()
        if status is None:
            status = "passed" if score >= pass_threshold else "failed"
        if direction is not None and direction not in ("max", "min"):
            raise ValueError(f"direction must be 'max' or 'min', got {direction!r}")

        trace: dict[str, Any] = {
            "experiment_id": self._experiment_id,
            "task_id": task_id,
            "status": status,
            "score": score,
        }
        if direction is not None:
            trace["direction"] = direction
        if summary is not None:
            trace["summary"] = summary
        if failure_reason is not None:
            trace["failure_reason"] = failure_reason
        if cost is not None:
            trace["cost"] = cost

        # Auto-fill timestamps
        trace["started_at"] = started_at or self._task_started.get(task_id, self._started_at)
        trace["ended_at"] = ended_at or now

        if artifacts is not None:
            trace["artifacts"] = artifacts
        if extra:
            trace.update(extra)

        with self._lock:
            self._tasks[task_id] = score
            if direction is not None:
                self._task_meta[task_id] = {"direction": direction}
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
        try:
            if score is None:
                if not self._tasks:
                    score = 0.0
                else:
                    score = sum(self._tasks.values()) / len(self._tasks)

            result: dict[str, Any] = {
                "score": round(score, 4),
                "tasks": dict(self._tasks),
                "started_at": self._started_at,
                "ended_at": _utc_now(),
            }
            if self._task_meta:
                result["tasks_meta"] = {k: dict(v) for k, v in self._task_meta.items()}
            self._backend.emit_result(result)
            return result
        finally:
            with _ACTIVE_RUNS_LOCK:
                _ACTIVE_RUNS.discard(self._experiment_id)
            # Cancel the safety-net finalizer now that the slot is released.
            # Otherwise it stays armed and, when this finished Run is later
            # gc'd, fires _release_active_run(experiment_id) — clobbering the
            # slot of a *different* live Run that reused the same id (#97).
            self._finalizer.detach()

    # -- context manager --------------------------------------------------

    def __enter__(self) -> Run:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if exc_type is None and not self._finished:
            self.finish()
        elif not self._finished:
            # Exception path -- finish() never called and won't be. Release
            # the registry slot so a retry in the same process isn't blocked,
            # and detach the finalizer so it can't later clobber a reused id.
            with _ACTIVE_RUNS_LOCK:
                _ACTIVE_RUNS.discard(self._experiment_id)
            self._finalizer.detach()
