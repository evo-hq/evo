from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

import fcntl


class LockTimeoutError(RuntimeError):
    """Raised when a workspace lock cannot be acquired in time."""


@contextlib.contextmanager
def advisory_lock(lock_path: Path, timeout_seconds: float = 10.0, poll_seconds: float = 0.1):
    """Acquire an advisory file lock with bounded retries."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    start = time.monotonic()
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() - start >= timeout_seconds:
                    raise LockTimeoutError(f"Timed out acquiring lock: {lock_path}")
                time.sleep(poll_seconds)
        yield
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
