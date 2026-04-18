from __future__ import annotations

import contextlib
import time
from pathlib import Path

import portalocker


class LockTimeoutError(RuntimeError):
    """Raised when a workspace lock cannot be acquired in time."""


@contextlib.contextmanager
def advisory_lock(lock_path: Path, timeout_seconds: float = 10.0, poll_seconds: float = 0.1):
    """Acquire an advisory file lock with bounded retries.

    Uses portalocker for cross-platform support: fcntl.flock on POSIX,
    LockFileEx on Windows. Semantics match the previous fcntl-only
    implementation (exclusive, non-blocking, bounded polling).
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+")
    start = time.monotonic()
    acquired = False
    try:
        while True:
            try:
                portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
                acquired = True
                break
            except portalocker.LockException:
                if time.monotonic() - start >= timeout_seconds:
                    raise LockTimeoutError(f"Timed out acquiring lock: {lock_path}")
                time.sleep(poll_seconds)
        yield
    finally:
        if acquired:
            portalocker.unlock(handle)
        handle.close()
