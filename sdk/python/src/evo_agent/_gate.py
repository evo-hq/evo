"""Gate -- safety check reporting context."""

from __future__ import annotations

import os
import sys
from typing import Any

from ._backend import Backend, default_backend


class Gate:
    """Collects pass/fail checks and exits with the appropriate code.

    Usage::

        from evo_agent import Gate

        with Gate() as gate:
            gate.check("5", score=1.0)
            gate.check("9", score=0.3)
        # finish() called automatically -> sys.exit(0 or 1)
    """

    def __init__(
        self,
        *,
        threshold: float = 0.5,
        backend: Backend | None = None,
    ) -> None:
        self._threshold = threshold
        self._backend = backend or default_backend()
        self._backend.setup(
            traces_dir=os.environ.get("EVO_TRACES_DIR"),
            experiment_id=os.environ.get("EVO_EXPERIMENT_ID"),
        )
        self._checks: list[dict[str, Any]] = []
        self._finished = False

    def check(
        self,
        task_id: str,
        *,
        score: float | None = None,
        passed: bool | None = None,
        detail: str = "",
    ) -> None:
        """Record a single gate check.

        Pass either *score* (compared against threshold) or explicit *passed*.
        """
        if passed is None:
            if score is None:
                raise ValueError("provide either score or passed")
            passed = score >= self._threshold

        self._checks.append({
            "task_id": str(task_id),
            "passed": passed,
            "score": score,
            "detail": detail,
        })

    def finish(self) -> None:
        """Print summary and exit with 0 (all passed) or 1 (any failed)."""
        if self._finished:
            return
        self._finished = True

        lines: list[str] = []
        n_passed = 0
        for c in self._checks:
            tag = "PASS" if c["passed"] else "FAIL"
            score_str = f" {c['score']:.2f}" if c["score"] is not None else ""
            detail_str = f"  {c['detail']}" if c["detail"] else ""
            lines.append(f"  {tag}  task {c['task_id']}:{score_str}{detail_str}")
            if c["passed"]:
                n_passed += 1

        total = len(self._checks)
        all_passed = n_passed == total
        lines.append(f"\n[gate] {n_passed}/{total} passed")

        self._backend.emit_gate_summary(passed=all_passed, lines=lines)
        sys.exit(0 if all_passed else 1)

    # -- context manager --------------------------------------------------

    def __enter__(self) -> Gate:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if exc_type is None and not self._finished:
            self.finish()
