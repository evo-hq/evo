"""Abstraction over local-subprocess vs. provider-owned remote execution.

`evo run` (and a few other CLI commands) need to run shell commands and
read/write files in the experiment's workspace. In `worktree` and `pool`
modes the workspace is a local directory; in `remote` mode it lives
inside a sandbox container reachable via the backend's provider-owned
client object.

`WorkspaceExecutor` is the seam. Two implementations:
  - `LocalExecutor`: subprocess + Path operations (existing behavior)
  - `RemoteExecutor`: routed through a provider-owned sandbox client

Stream semantics (run vs. stream):
  - `run()` is one-shot: blocks until the process exits, returns
    stdout/stderr/exit_code.
  - `stream()` is long-running: tees stdout/stderr to local files as
    bytes arrive (so a sandbox death mid-run preserves whatever was
    emitted up to that point). Returns the same shape as `run()` once
    the process terminates.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .backends.protocol import SandboxClient


@dataclass
class ExecResult:
    """Common result shape regardless of execution mode."""

    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool = False
    duration_ms: int = 0


class WorkspaceExecutor:
    """Base class. Subclasses implement local or remote execution."""

    is_remote: bool = False

    def run(
        self,
        cmd: list[str],
        *,
        cwd: Path | str,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        check: bool = False,
    ) -> ExecResult:
        raise NotImplementedError

    def stream(
        self,
        cmd: list[str],
        *,
        cwd: Path | str,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        stdout_path: Path,
        stderr_path: Path,
        mirror_remote_dir: Path | str | None = None,
        mirror_local_dir: Path | None = None,
    ) -> ExecResult:
        raise NotImplementedError

    def read_text(self, path: Path | str) -> str:
        raise NotImplementedError

    def read_bytes(self, path: Path | str) -> bytes:
        raise NotImplementedError

    def write_text(self, path: Path | str, content: str) -> None:
        raise NotImplementedError

    def file_exists(self, path: Path | str) -> bool:
        raise NotImplementedError

    def list_dir(self, path: Path | str) -> list[str]:
        """Return filenames (not full paths) directly under `path`. If the
        directory doesn't exist, returns []."""
        raise NotImplementedError

    def fetch_dir(self, src: Path | str, dst: Path) -> None:
        """Copy the contents of a workspace directory into a local
        directory. For local this is shutil.copytree-ish; for remote it
        downloads each file via fs_read."""
        raise NotImplementedError

    def close(self) -> None:
        """Release any held resources (HTTP session for remote)."""
        pass


# ---------------------------------------------------------------------------
# Local executor -- preserves today's subprocess + Path semantics
# ---------------------------------------------------------------------------


class LocalExecutor(WorkspaceExecutor):
    is_remote = False

    def run(
        self,
        cmd: list[str],
        *,
        cwd: Path | str,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        check: bool = False,
    ) -> ExecResult:
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=check,
            )
            dt = int((time.monotonic() - t0) * 1000)
            return ExecResult(
                stdout=proc.stdout, stderr=proc.stderr,
                exit_code=proc.returncode, timed_out=False, duration_ms=dt,
            )
        except subprocess.TimeoutExpired as exc:
            dt = int((time.monotonic() - t0) * 1000)
            return ExecResult(
                stdout=(exc.stdout or b"").decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
                stderr=(exc.stderr or b"").decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or ""),
                exit_code=None, timed_out=True, duration_ms=dt,
            )

    def stream(
        self,
        cmd: list[str],
        *,
        cwd: Path | str,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        stdout_path: Path,
        stderr_path: Path,
        mirror_remote_dir: Path | str | None = None,
        mirror_local_dir: Path | None = None,
    ) -> ExecResult:
        """Spawn the process and tee stdout/stderr to local files in real
        time. Returns when the process exits or `timeout` elapses."""
        del mirror_remote_dir, mirror_local_dir
        t0 = time.monotonic()
        with stdout_path.open("wb") as out_f, stderr_path.open("wb") as err_f:
            proc = subprocess.Popen(
                cmd, cwd=cwd, env=env,
                stdout=out_f, stderr=err_f,
            )
            try:
                proc.wait(timeout=timeout)
                dt = int((time.monotonic() - t0) * 1000)
                # File is closed via the with-block; re-read for return value
                # (matches what cmd_run expects).
        # Note: out_f/err_f closed by the with-block immediately after
        # wait()/timeout; reopen for read below.
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                stdout = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else ""
                stderr = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else ""
                dt = int((time.monotonic() - t0) * 1000)
                return ExecResult(
                    stdout=stdout, stderr=stderr,
                    exit_code=None, timed_out=True, duration_ms=dt,
                )
        stdout = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else ""
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else ""
        return ExecResult(
            stdout=stdout, stderr=stderr,
            exit_code=proc.returncode, timed_out=False, duration_ms=dt,
        )

    def read_text(self, path: Path | str) -> str:
        return Path(path).read_text(encoding="utf-8")

    def read_bytes(self, path: Path | str) -> bytes:
        return Path(path).read_bytes()

    def write_text(self, path: Path | str, content: str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def file_exists(self, path: Path | str) -> bool:
        return Path(path).exists()

    def list_dir(self, path: Path | str) -> list[str]:
        p = Path(path)
        if not p.exists() or not p.is_dir():
            return []
        return sorted(child.name for child in p.iterdir())

    def fetch_dir(self, src: Path | str, dst: Path) -> None:
        src_p = Path(src)
        dst.mkdir(parents=True, exist_ok=True)
        if not src_p.exists():
            return
        for item in src_p.iterdir():
            if item.is_file():
                shutil.copy2(item, dst / item.name)


# ---------------------------------------------------------------------------
# Remote executor -- routes everything through the provider sandbox client
# ---------------------------------------------------------------------------


class RemoteExecutor(WorkspaceExecutor):
    """Talks to one remote sandbox client. Holds the client session for the
    duration of `evo run`; closed at the end."""

    is_remote = True

    def __init__(self, client: SandboxClient) -> None:
        self.client = client

    def run(
        self,
        cmd: list[str],
        *,
        cwd: Path | str,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        check: bool = False,
    ) -> ExecResult:
        if not cmd:
            raise ValueError("cmd must be a non-empty list")
        timeout_ms = int(timeout * 1000) if timeout is not None else None
        result = self.client.process_run(
            command=cmd[0],
            args=list(cmd[1:]),
            cwd=str(cwd),
            env=env or None,
            timeout_ms=timeout_ms,
        )
        if check and (result.exit_code or 0) != 0:
            raise subprocess.CalledProcessError(
                result.exit_code or 1, cmd, output=result.stdout, stderr=result.stderr,
            )
        return ExecResult(
            stdout=result.stdout, stderr=result.stderr,
            exit_code=result.exit_code, timed_out=result.timed_out,
            duration_ms=result.duration_ms,
        )

    def stream(
        self,
        cmd: list[str],
        *,
        cwd: Path | str,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        stdout_path: Path,
        stderr_path: Path,
        mirror_remote_dir: Path | str | None = None,
        mirror_local_dir: Path | None = None,
    ) -> ExecResult:
        """Long-running process + streamed logs + best-effort trace mirroring."""
        if not cmd:
            raise ValueError("cmd must be a non-empty list")

        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_bytes(b"")
        stderr_path.write_bytes(b"")

        process_id = self.client.process_start(
            command=cmd[0],
            args=list(cmd[1:]),
            cwd=str(cwd),
            env=env or None,
        )
        started = time.monotonic()
        stop_event = threading.Event()
        log_errors: list[tuple[str, Exception]] = []
        mirrored_sizes: dict[str, int | None] = {}

        def _consume_logs(stream_name: str, path: Path) -> None:
            log_client = self.client.clone()
            try:
                with path.open("ab") as handle:
                    for entry in log_client.process_logs(
                        process_id,
                        follow=True,
                        stream=stream_name,
                    ):
                        handle.write(entry.data)
                        handle.flush()
            except Exception as exc:  # noqa: BLE001
                log_errors.append((stream_name, exc))
            finally:
                log_client.close()

        def _mirror_once(client: SandboxClient) -> None:
            if mirror_remote_dir is None or mirror_local_dir is None:
                return
            mirror_local_dir.mkdir(parents=True, exist_ok=True)
            try:
                entries = client.fs_entries(str(mirror_remote_dir))
            except Exception:
                return
            for entry in entries:
                if entry.is_dir:
                    continue
                if mirrored_sizes.get(entry.path) == entry.size:
                    continue
                try:
                    blob = client.fs_read(entry.path)
                except Exception:
                    continue
                (mirror_local_dir / entry.name).write_bytes(blob)
                mirrored_sizes[entry.path] = entry.size

        def _mirror_loop() -> None:
            if mirror_remote_dir is None or mirror_local_dir is None:
                return
            mirror_client = self.client.clone()
            try:
                _mirror_once(mirror_client)
                while not stop_event.wait(0.25):
                    _mirror_once(mirror_client)
                _mirror_once(mirror_client)
            finally:
                mirror_client.close()

        stdout_thread = threading.Thread(
            target=_consume_logs,
            args=("stdout", stdout_path),
            daemon=True,
            name="evo-remote-stdout",
        )
        stderr_thread = threading.Thread(
            target=_consume_logs,
            args=("stderr", stderr_path),
            daemon=True,
            name="evo-remote-stderr",
        )
        mirror_thread = threading.Thread(
            target=_mirror_loop,
            daemon=True,
            name="evo-remote-traces",
        )
        stdout_thread.start()
        stderr_thread.start()
        mirror_thread.start()
        if mirror_remote_dir is not None and mirror_local_dir is not None:
            _mirror_once(self.client)

        timed_out = False
        status: dict[str, Any] | None = None
        status_error: Exception | None = None
        deadline = (started + timeout) if timeout is not None else None
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                try:
                    self.client.process_stop(process_id)
                except Exception:
                    pass
                time.sleep(0.5)
                try:
                    status = self.client.process_status(process_id)
                except Exception:
                    status = None
                if status is None or status.get("status") == "running":
                    try:
                        self.client.process_kill(process_id)
                    except Exception:
                        pass
                break
            try:
                status = self.client.process_status(process_id)
            except Exception as exc:  # noqa: BLE001
                status_error = exc
                break
            if mirror_remote_dir is not None and mirror_local_dir is not None:
                _mirror_once(self.client)
            if status.get("status") != "running":
                break
            time.sleep(0.25)

        stop_event.set()
        stdout_thread.join(timeout=2.0)
        stderr_thread.join(timeout=2.0)
        mirror_thread.join(timeout=2.0)

        stdout = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else ""
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else ""
        if status_error is not None and status is None:
            if stderr and not stderr.endswith("\n"):
                stderr += "\n"
            stderr += f"[remote stream disconnected] {status_error}"
        if log_errors and status is None:
            if stderr and not stderr.endswith("\n"):
                stderr += "\n"
            stderr += "[remote log follow failed] " + "; ".join(
                f"{name}: {exc}" for name, exc in log_errors
            )
        stderr_path.write_text(stderr, encoding="utf-8")

        return ExecResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=None if timed_out else ((status or {}).get("exitCode") if status else 1),
            timed_out=timed_out,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def read_text(self, path: Path | str) -> str:
        return self.client.fs_read(str(path)).decode("utf-8", errors="replace")

    def read_bytes(self, path: Path | str) -> bytes:
        return self.client.fs_read(str(path))

    def write_text(self, path: Path | str, content: str) -> None:
        self.client.fs_write(str(path), content.encode("utf-8"))

    def file_exists(self, path: Path | str) -> bool:
        try:
            self.client.fs_stat(str(path))
            return True
        except Exception:
            return False

    def list_dir(self, path: Path | str) -> list[str]:
        try:
            entries = self.client.fs_entries(str(path))
        except Exception:
            return []
        return sorted(e.name for e in entries)

    def fetch_dir(self, src: Path | str, dst: Path) -> None:
        dst.mkdir(parents=True, exist_ok=True)
        for name in self.list_dir(src):
            try:
                blob = self.client.fs_read(f"{src}/{name}")
            except Exception:
                continue
            (dst / name).write_bytes(blob)

    def close(self) -> None:
        self.client.close()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


@contextmanager
def workspace_executor_for(backend: Any, root: Path, node: dict[str, Any]) -> Iterator[WorkspaceExecutor]:
    """Yield a WorkspaceExecutor appropriate for `backend`, scoped to
    the experiment node's lease. Closes the underlying HTTP session on
    exit (no-op for local).
    """
    if getattr(backend, "name", None) == "remote":
        client = backend.client_for_node(root, node)
        executor = RemoteExecutor(client)
        try:
            yield executor
        finally:
            executor.close()
    else:
        yield LocalExecutor()
