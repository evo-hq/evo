"""HTTP client for rivet-dev/sandbox-agent.

Thin wrapper over the daemon's REST surface: process exec (one-shot and
long-running with log streaming), filesystem ops, health. Used by both
`RemoteSandboxBackend` (for evo's lifecycle steps) and the MCP server
(for routed agent tool calls).

Endpoint reference: https://github.com/rivet-dev/sandbox-agent/blob/main/docs/openapi.json
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterator
from urllib.parse import urlencode

import requests


# Default timeouts. Generous; the orchestrator-side `evo run --timeout`
# bounds long-running benchmarks separately.
DEFAULT_REQUEST_TIMEOUT = 60.0       # most ops should be <1s; 60s = patience
LONG_REQUEST_TIMEOUT = 600.0         # process/run with embedded benchmark


@dataclass
class ProcessRunResult:
    """Response shape for `POST /v1/processes/run`."""

    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool
    duration_ms: int
    stdout_truncated: bool = False
    stderr_truncated: bool = False


@dataclass
class FsEntry:
    """One entry from `GET /v1/fs/entries`."""

    name: str
    path: str
    is_dir: bool
    size: int | None = None


class SandboxAgentError(Exception):
    """Raised on non-2xx responses from sandbox-agent. Carries the
    HTTP status and response body for diagnostics."""

    def __init__(self, status: int, message: str, body: str = "") -> None:
        super().__init__(f"[{status}] {message}")
        self.status = status
        self.body = body


class SandboxAgentClient:
    """Synchronous client for one sandbox-agent instance.

    Construction is cheap; one client per sandbox. Stateless except for the
    underlying `requests.Session` (kept-alive HTTP connections).
    """

    def __init__(self, base_url: str, bearer_token: str | None = None) -> None:
        # Strip trailing slash so we can compose paths cleanly.
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()
        if bearer_token:
            self._session.headers["Authorization"] = f"Bearer {bearer_token}"
        self._session.headers["User-Agent"] = "evo-sandbox-client/1"

    # ---------------------------------------------------------------- helpers

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _check(self, resp: requests.Response) -> requests.Response:
        if resp.status_code >= 400:
            try:
                body = resp.text
            except Exception:
                body = "<unreadable>"
            raise SandboxAgentError(
                resp.status_code,
                f"{resp.request.method} {resp.url} failed",
                body=body[:1000],
            )
        return resp

    # ---------------------------------------------------------------- health

    def health(self) -> dict[str, Any]:
        resp = self._session.get(self._url("/v1/health"), timeout=DEFAULT_REQUEST_TIMEOUT)
        self._check(resp)
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text}

    def wait_for_health(self, timeout_seconds: float = 30.0, poll_interval: float = 0.5) -> None:
        """Poll `/v1/health` until 200 or timeout. Used post-provision."""
        deadline = time.monotonic() + timeout_seconds
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            try:
                self.health()
                return
            except (requests.RequestException, SandboxAgentError) as exc:
                last_err = exc
                time.sleep(poll_interval)
        raise SandboxAgentError(
            0,
            f"sandbox-agent at {self.base_url} did not become healthy within "
            f"{timeout_seconds}s; last error: {last_err}",
        )

    # ---------------------------------------------------------------- process exec

    def process_run(
        self,
        command: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_ms: int | None = None,
        max_output_bytes: int | None = None,
    ) -> ProcessRunResult:
        """One-shot command. Blocks until the process exits or
        timeout_ms elapses (sandbox-side enforcement)."""
        body: dict[str, Any] = {"command": command}
        if args:
            body["args"] = list(args)
        if cwd:
            body["cwd"] = cwd
        if env:
            body["env"] = dict(env)
        if timeout_ms is not None:
            body["timeoutMs"] = timeout_ms
        if max_output_bytes is not None:
            body["maxOutputBytes"] = max_output_bytes

        # HTTP timeout: a bit more than sandbox-side timeout, or LONG default.
        http_timeout = (
            (timeout_ms / 1000) + 10 if timeout_ms is not None else LONG_REQUEST_TIMEOUT
        )
        resp = self._session.post(
            self._url("/v1/processes/run"),
            json=body,
            timeout=http_timeout,
        )
        self._check(resp)
        data = resp.json()
        return ProcessRunResult(
            stdout=data.get("stdout", ""),
            stderr=data.get("stderr", ""),
            exit_code=data.get("exitCode"),
            timed_out=bool(data.get("timedOut", False)),
            duration_ms=int(data.get("durationMs", 0)),
            stdout_truncated=bool(data.get("stdoutTruncated", False)),
            stderr_truncated=bool(data.get("stderrTruncated", False)),
        )

    def process_start(
        self,
        command: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        """Start a long-running process. Returns the process_id; logs are
        streamed via `process_logs(id, follow=True)`."""
        body: dict[str, Any] = {"command": command}
        if args:
            body["args"] = list(args)
        if cwd:
            body["cwd"] = cwd
        if env:
            body["env"] = dict(env)
        resp = self._session.post(
            self._url("/v1/processes"),
            json=body,
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        self._check(resp)
        data = resp.json()
        pid = data.get("id")
        if not pid:
            raise SandboxAgentError(
                resp.status_code, "process start returned no id", body=resp.text[:200]
            )
        return str(pid)

    def process_status(self, process_id: str) -> dict[str, Any]:
        resp = self._session.get(
            self._url(f"/v1/processes/{process_id}"),
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        self._check(resp)
        return resp.json()

    def process_logs(
        self,
        process_id: str,
        follow: bool = False,
        stream: str = "combined",
    ) -> Iterator[bytes]:
        """Yield raw byte chunks of process logs.

        With `follow=True`, the connection stays open until the process
        terminates and the daemon closes the stream. Yields whatever the
        server emits; callers tee to disk.
        """
        params = {"follow": "true" if follow else "false", "stream": stream}
        resp = self._session.get(
            self._url(f"/v1/processes/{process_id}/logs?{urlencode(params)}"),
            stream=True,
            timeout=None if follow else DEFAULT_REQUEST_TIMEOUT,
        )
        self._check(resp)
        try:
            for chunk in resp.iter_content(chunk_size=4096):
                if chunk:
                    yield chunk
        finally:
            resp.close()

    def process_stop(self, process_id: str) -> None:
        resp = self._session.post(
            self._url(f"/v1/processes/{process_id}/stop"),
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        self._check(resp)

    def process_kill(self, process_id: str) -> None:
        resp = self._session.post(
            self._url(f"/v1/processes/{process_id}/kill"),
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        self._check(resp)

    # ---------------------------------------------------------------- filesystem

    def fs_read(self, path: str) -> bytes:
        resp = self._session.get(
            self._url(f"/v1/fs/file?{urlencode({'path': path})}"),
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        self._check(resp)
        return resp.content

    def fs_write(self, path: str, data: bytes) -> None:
        resp = self._session.put(
            self._url(f"/v1/fs/file?{urlencode({'path': path})}"),
            data=data,
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        self._check(resp)

    def fs_entries(self, path: str) -> list[FsEntry]:
        resp = self._session.get(
            self._url(f"/v1/fs/entries?{urlencode({'path': path})}"),
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        self._check(resp)
        out = []
        for entry in resp.json().get("entries", []):
            out.append(FsEntry(
                name=entry.get("name", ""),
                path=entry.get("path", ""),
                is_dir=bool(entry.get("isDir", False)),
                size=entry.get("size"),
            ))
        return out

    def fs_stat(self, path: str) -> dict[str, Any]:
        resp = self._session.get(
            self._url(f"/v1/fs/stat?{urlencode({'path': path})}"),
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        self._check(resp)
        return resp.json()

    def fs_mkdir(self, path: str, recursive: bool = True) -> None:
        body = {"path": path, "recursive": recursive}
        resp = self._session.post(
            self._url("/v1/fs/mkdir"),
            json=body,
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        self._check(resp)

    def fs_delete(self, path: str, recursive: bool = False) -> None:
        params = {"path": path, "recursive": "true" if recursive else "false"}
        resp = self._session.delete(
            self._url(f"/v1/fs/entry?{urlencode(params)}"),
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        self._check(resp)

    def fs_move(self, src: str, dst: str) -> None:
        body = {"from": src, "to": dst}
        resp = self._session.post(
            self._url("/v1/fs/move"),
            json=body,
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        self._check(resp)

    def fs_upload_batch(self, dest_dir: str, tar_bytes: bytes) -> None:
        """Upload a tar archive that the server extracts under `dest_dir`."""
        params = {"path": dest_dir}
        resp = self._session.post(
            self._url(f"/v1/fs/upload-batch?{urlencode(params)}"),
            data=tar_bytes,
            headers={"Content-Type": "application/x-tar"},
            timeout=LONG_REQUEST_TIMEOUT,
        )
        self._check(resp)

    # ---------------------------------------------------------------- close

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "SandboxAgentClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()
