"""Abstraction over local-subprocess vs. remote-sandbox-agent execution.

`evo run` (and a few other CLI commands) need to run shell commands and
read/write files in the experiment's workspace. In `worktree` and `pool`
modes the workspace is a local directory; in `remote` mode it lives
inside a sandbox container reachable via sandbox-agent's HTTP API.

`WorkspaceExecutor` is the seam. Two implementations:
  - `LocalExecutor`: subprocess + Path operations (existing behavior)
  - `RemoteExecutor`: routed through a `SandboxAgentClient`

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
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .sandbox_client import SandboxAgentClient


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
    ) -> ExecResult:
        """Spawn the process and tee stdout/stderr to local files in real
        time. Returns when the process exits or `timeout` elapses."""
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
# Remote executor -- routes everything through SandboxAgentClient
# ---------------------------------------------------------------------------


class RemoteExecutor(WorkspaceExecutor):
    """Talks to one sandbox-agent instance. Holds the HTTP session for the
    duration of `evo run`; closed at the end."""

    is_remote = True

    def __init__(self, client: SandboxAgentClient) -> None:
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
    ) -> ExecResult:
        """Long-running process + streamed logs.

        Implementation note: for POC simplicity we use the one-shot
        `process_run` endpoint, write stdout/stderr to the local files
        once at the end, and bound by the orchestrator-side timeout.
        Salvage-on-death (incremental log fetch via the streaming
        endpoint) is a refinement -- tracked for alpha.4. The contract
        here matches the local streamer's: when this returns, the local
        files contain whatever the process wrote.
        """
        # Pre-create the files so callers reading them mid-run don't get
        # FileNotFoundError on the local-file paths set in env.
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        result = self.run(cmd, cwd=cwd, env=env, timeout=timeout)
        stdout_path.write_text(result.stdout or "", encoding="utf-8")
        stderr_path.write_text(result.stderr or "", encoding="utf-8")
        return result

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
