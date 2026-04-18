"""Unit tests for the cross-platform advisory lock.

Covers basic acquire/release, timeout on contention, and that consecutive
locks on the same path succeed after release. Exercises the portalocker
code path on whatever platform pytest runs on (POSIX or Windows).

Run: `python3 tests/unit/test_locking.py`
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))

from evo.locking import LockTimeoutError, advisory_lock  # noqa: E402


def test_acquire_and_release_basic() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        lock_path = Path(tmp) / "sub" / "lock"
        with advisory_lock(lock_path):
            assert lock_path.exists(), "lock file should be created on acquire"
        # Re-acquiring after release must succeed.
        with advisory_lock(lock_path):
            pass


def test_timeout_when_already_held() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        lock_path = Path(tmp) / "lock"
        holder_acquired = threading.Event()
        holder_release = threading.Event()

        def holder() -> None:
            with advisory_lock(lock_path, timeout_seconds=5.0):
                holder_acquired.set()
                holder_release.wait(timeout=5.0)

        t = threading.Thread(target=holder, daemon=True)
        t.start()
        assert holder_acquired.wait(timeout=5.0), "holder failed to acquire"

        start = time.monotonic()
        try:
            with advisory_lock(lock_path, timeout_seconds=0.3, poll_seconds=0.05):
                raised = False
        except LockTimeoutError:
            raised = True
        elapsed = time.monotonic() - start

        holder_release.set()
        t.join(timeout=5.0)

        assert raised, "expected LockTimeoutError when lock is held"
        assert elapsed >= 0.3, f"timeout should wait at least 0.3s, got {elapsed:.3f}s"
        assert elapsed < 2.0, f"timeout should not overshoot wildly, got {elapsed:.3f}s"


def test_reacquire_after_release_cross_thread() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        lock_path = Path(tmp) / "lock"
        with advisory_lock(lock_path):
            pass

        done = threading.Event()

        def other() -> None:
            with advisory_lock(lock_path, timeout_seconds=1.0):
                done.set()

        t = threading.Thread(target=other, daemon=True)
        t.start()
        t.join(timeout=3.0)
        assert done.is_set(), "second thread should acquire after release"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all tests passed")
