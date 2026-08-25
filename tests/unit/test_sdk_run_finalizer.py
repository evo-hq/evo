"""The active-run registry must not be clobbered by a stale finalizer (#97).

Each Run arms `weakref.finalize(self, _release_active_run, experiment_id)`
as a safety net for leaked (never-finished) runs. But finish()/__exit__ did
not detach that finalizer, so when a *finished* Run was later gc'd its
finalizer fired and discarded the experiment_id from the registry — even if
that id now belonged to a different, still-active Run, silently defeating
the double-Run guard.
"""
from __future__ import annotations

import gc
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "sdk" / "python" / "src"))

from evo_agent._run import Run, _ACTIVE_RUNS
from evo_agent._backend import Backend


class _NullBackend(Backend):
    def setup(self, **k): pass
    def write_trace(self, *a, **k): pass
    def emit_result(self, *a, **k): pass
    def emit_gate_summary(self, *a, **k): pass


def _mk(exp="x") -> Run:
    return Run(experiment_id=exp, backend=_NullBackend())


@pytest.fixture(autouse=True)
def _clear_registry():
    _ACTIVE_RUNS.clear()
    yield
    _ACTIVE_RUNS.clear()


def test_gc_of_finished_run_does_not_free_a_live_runs_slot():
    r1 = _mk("x")
    r1.finish()               # slot freed
    r2 = _mk("x")             # allowed; r2 is live and never finished
    del r1
    gc.collect()              # r1's finalizer must NOT clobber r2's slot
    with pytest.raises(RuntimeError):
        _mk("x")              # r2 still active -> must be refused
    r2.finish()


def test_gc_of_context_exited_run_does_not_free_a_live_runs_slot():
    with _mk("y"):
        pass                  # finishes via __exit__
    r2 = _mk("y")
    import gc as _gc
    _gc.collect()
    with pytest.raises(RuntimeError):
        _mk("y")
    r2.finish()


def test_leaked_run_still_released_by_finalizer_on_gc():
    """The safety net must still work: a Run that is never finished releases
    its slot when garbage-collected."""
    r = _mk("z")
    del r
    gc.collect()
    r2 = _mk("z")             # slot must have been released by the finalizer
    r2.finish()
