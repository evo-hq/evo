"""Cloudflare Sandbox provider.

Uses Cloudflare's Sandbox bridge HTTP API to provision an isolated sandbox
and then talks to that sandbox through the bridge. The bridge is the control
plane; evo does not install sandbox-agent inside the container for this
provider.
"""
from __future__ import annotations

import json
import os
import shlex
import time
import uuid
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

import requests

from ..protocol import (
    RemoteBackendUnavailable,
    SandboxClient,
    SandboxHandle,
    SandboxSpec,
)


DEFAULT_API_URL = "https://cloudflare-sandbox-bridge.example.workers.dev"
DEFAULT_TIMEOUT_SECONDS = 3600
DEFAULT_HEALTH_TIMEOUT = 60.0
DEFAULT_WORKSPACE_ROOT = "/workspace/repo"
DEFAULT_PROC_SUBDIR = ".evo-cloudflare-processes"


class CloudflareBridgeClient:
    """HTTP client for the Cloudflare Sandbox bridge.

    The client implements the same method surface the remote workspace
    executor expects, but uses the bridge's HTTP routes instead of a
    sandbox-agent daemon.
    """

    def __init__(
        self,
        base_url: str,
        bearer_token: str,
        sandbox_id: str,
        workspace_root: str = DEFAULT_WORKSPACE_ROOT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.bearer_token = bearer_token
        self.sandbox_id = sandbox_id
        self.workspace_root = str(workspace_root).rstrip("/") or DEFAULT_WORKSPACE_ROOT
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {bearer_token}"
        self._session.headers["User-Agent"] = "evo-cloudflare-client/1"

    # ---------------------------------------------------------------- helpers

    def clone(self) -> "CloudflareBridgeClient":
        return CloudflareBridgeClient(
            self.base_url,
            self.bearer_token,
            self.sandbox_id,
            workspace_root=self.workspace_root,
        )

    def __enter__(self) -> "CloudflareBridgeClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def _sandbox_url(self, path: str) -> str:
        return f"{self.base_url}/v1/sandbox/{self.sandbox_id}{path}"

    def _check(self, resp: requests.Response) -> requests.Response:
        if resp.status_code >= 400:
            try:
                body = resp.text
            except Exception:
                body = "<unreadable>"
            raise RemoteBackendUnavailable(
                f"Cloudflare sandbox request failed: [{resp.status_code}] "
                f"{resp.request.method} {resp.url}: {body[:1000]}"
            )
        return resp

    def _shell_exec(self, command: str, cwd: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"argv": ["sh", "-lc", command], "timeout_ms": 60000}
        if cwd:
            body["cwd"] = cwd
        resp = self._session.post(
            self._sandbox_url("/exec"),
            json=body,
            timeout=70.0,
        )
        self._check(resp)
        return _parse_sse_exec_response(resp.text)

    def _fs_path(self, path: str) -> str:
        return self._sandbox_url("/file/" + quote(path.lstrip("/"), safe="/"))

    def _proc_dir(self, process_id: str) -> str:
        return f"{self.workspace_root}/{DEFAULT_PROC_SUBDIR}/{process_id}"

    def _process_paths(self, process_id: str) -> dict[str, str]:
        proc_dir = self._proc_dir(process_id)
        return {
            "dir": proc_dir,
            "pid": f"{proc_dir}/pid",
            "exit": f"{proc_dir}/exit_code",
            "stdout": f"{proc_dir}/stdout.log",
            "stderr": f"{proc_dir}/stderr.log",
        }

    # ---------------------------------------------------------------- health

    def health(self) -> dict[str, Any]:
        resp = self._session.get(f"{self.base_url}/health", timeout=10.0)
        self._check(resp)
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text}

    def wait_for_health(self, timeout_seconds: float = 30.0, poll_interval: float = 0.5) -> None:
        deadline = time.monotonic() + timeout_seconds
        last_exc: Exception | None = None
        while time.monotonic() < deadline:
            try:
                self.health()
                return
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                time.sleep(poll_interval)
        raise RemoteBackendUnavailable(
            f"Cloudflare sandbox bridge at {self.base_url} did not become healthy "
            f"within {timeout_seconds}s; last error: {last_exc}"
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
    ) -> Any:
        del max_output_bytes
        shell_cmd = _build_shell_command(command, args or [], cwd=cwd, env=env)
        body: dict[str, Any] = {
            "argv": ["sh", "-lc", shell_cmd],
            "timeout_ms": timeout_ms if timeout_ms is not None else 60000,
        }
        if cwd:
            body["cwd"] = cwd
        resp = self._session.post(
            self._sandbox_url("/exec"),
            json=body,
            timeout=((timeout_ms or 60000) / 1000.0) + 20.0,
        )
        self._check(resp)
        data = _parse_sse_exec_response(resp.text)
        return type("ProcessRunResult", (), {
            "stdout": data.get("stdout", ""),
            "stderr": data.get("stderr", ""),
            "exit_code": data.get("exit_code"),
            "timed_out": bool(data.get("timed_out", False)),
            "duration_ms": int(data.get("duration_ms", 0)),
            "stdout_truncated": False,
            "stderr_truncated": False,
        })()

    def process_start(
        self,
        command: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        process_id = uuid.uuid4().hex
        paths = self._process_paths(process_id)
        shell_cmd = _build_shell_command(command, args or [], cwd=cwd, env=env)
        bootstrap = "\n".join([
            "set -eu",
            f"mkdir -p {shlex.quote(paths['dir'])}",
            f"nohup sh -lc {shlex.quote('set -eu; ' + _process_wrapper_shell(shell_cmd, paths))} "
            f">/dev/null 2>&1 &",
            f"echo $! > {shlex.quote(paths['pid'])}",
            "sleep 0.1",
        ])
        resp = self._session.post(
            self._sandbox_url("/exec"),
            json={"argv": ["sh", "-lc", bootstrap], "timeout_ms": 60000},
            timeout=70.0,
        )
        self._check(resp)
        # The shell wrapper has already kicked off the process. The sandbox
        # returns an SSE stream, but we only need a durable process id.
        return process_id

    def process_status(self, process_id: str) -> dict[str, Any]:
        paths = self._process_paths(process_id)
        try:
            pid = self.fs_read(paths["pid"]).decode("utf-8", errors="replace").strip()
        except Exception:
            return {"status": "not_found", "exitCode": 1}
        if not pid:
            return {"status": "not_found", "exitCode": 1}
        try:
            exit_code = self.fs_read(paths["exit"]).decode("utf-8", errors="replace").strip()
            if exit_code:
                return {"status": "exited", "exitCode": int(exit_code)}
        except Exception:
            pass
        probe = self._shell_exec(f"kill -0 {shlex.quote(pid)}")
        if int(probe.get("exit_code") or 0) == 0:
            return {"status": "running", "exitCode": None}
        return {"status": "exited", "exitCode": 1}

    def process_logs(
        self,
        process_id: str,
        follow: bool = False,
        stream: str = "combined",
    ) -> Iterator[Any]:
        paths = self._process_paths(process_id)
        if stream not in {"stdout", "stderr", "combined"}:
            stream = "combined"
        if not follow:
            if stream == "stderr":
                data = self.fs_read(paths["stderr"])
                if data:
                    yield _log_entry(0, "stderr", data)
                return
            if stream == "stdout":
                data = self.fs_read(paths["stdout"])
                if data:
                    yield _log_entry(0, "stdout", data)
                return
            out = b""
            try:
                out += self.fs_read(paths["stdout"])
            except Exception:
                pass
            try:
                out += self.fs_read(paths["stderr"])
            except Exception:
                pass
            if out:
                yield _log_entry(0, stream, out)
            return

        offset_out = 0
        offset_err = 0
        seq = 0
        while True:
            state = self.process_status(process_id)
            running = state.get("status") == "running"
            if stream in {"stdout", "combined"}:
                try:
                    data = self.fs_read(paths["stdout"])
                except Exception:
                    data = b""
                chunk = data[offset_out:]
                if chunk:
                    offset_out = len(data)
                    seq += 1
                    yield _log_entry(seq, "stdout", chunk)
            if stream in {"stderr", "combined"}:
                try:
                    data = self.fs_read(paths["stderr"])
                except Exception:
                    data = b""
                chunk = data[offset_err:]
                if chunk:
                    offset_err = len(data)
                    seq += 1
                    yield _log_entry(seq, "stderr", chunk)
            if not running:
                break
            time.sleep(0.25)

    def process_stop(self, process_id: str) -> None:
        self.process_kill(process_id)

    def process_kill(self, process_id: str) -> None:
        paths = self._process_paths(process_id)
        try:
            pid = self.fs_read(paths["pid"]).decode("utf-8", errors="replace").strip()
        except Exception:
            return
        if not pid:
            return
        self._shell_exec(f"kill -TERM {shlex.quote(pid)} || true")

    # ---------------------------------------------------------------- filesystem

    def fs_read(self, path: str) -> bytes:
        resp = self._session.get(self._fs_path(path), timeout=30.0)
        self._check(resp)
        return resp.content

    def fs_write(self, path: str, data: bytes) -> None:
        resp = self._session.put(self._fs_path(path), data=data, timeout=30.0)
        self._check(resp)

    def fs_entries(self, path: str) -> list[Any]:
        result = self._shell_exec(
            "python3 - <<'PY'\n"
            "import json, os, sys\n"
            f"path = {path!r}\n"
            "try:\n"
            "    with os.scandir(path) as it:\n"
            "        out = []\n"
            "        for entry in it:\n"
            "            st = entry.stat(follow_symlinks=False)\n"
            "            out.append({"
            "'name': entry.name, 'path': os.path.join(path, entry.name), "
            "'is_dir': entry.is_dir(follow_symlinks=False), 'size': st.st_size})\n"
            "        print(json.dumps(out))\n"
            "except FileNotFoundError:\n"
            "    print('[]')\n"
            "PY",
        )
        try:
            payload = json.loads(result.get("stdout", "[]") or "[]")
        except Exception:
            payload = []
        return [type("FsEntry", (), entry)() for entry in payload]

    def fs_stat(self, path: str) -> dict[str, Any]:
        result = self._shell_exec(
            "python3 - <<'PY'\n"
            "import json, os\n"
            f"path = {path!r}\n"
            "st = os.stat(path)\n"
            "print(json.dumps({'size': st.st_size, 'is_dir': os.path.isdir(path)}))\n"
            "PY",
        )
        return json.loads(result.get("stdout", "{}") or "{}")

    def fs_mkdir(self, path: str, recursive: bool = True) -> None:
        del recursive
        self._shell_exec(f"mkdir -p {shlex.quote(path)}")

    def fs_delete(self, path: str, recursive: bool = False) -> None:
        if recursive:
            self._shell_exec(f"rm -rf {shlex.quote(path)}")
        else:
            self._shell_exec(f"rm -f {shlex.quote(path)}")

    def fs_move(self, src: str, dst: str) -> None:
        self._shell_exec(f"mkdir -p {shlex.quote(dst.rsplit('/', 1)[0] or '/')} && mv {shlex.quote(src)} {shlex.quote(dst)}")

    def fs_upload_batch(self, dest_dir: str, tar_bytes: bytes) -> None:
        temp_path = f"{self.workspace_root}/{DEFAULT_PROC_SUBDIR}/{uuid.uuid4().hex}/upload.tar"
        self.fs_mkdir(str(Path(temp_path).parent), recursive=True)
        self.fs_write(temp_path, tar_bytes)
        self._shell_exec(
            f"mkdir -p {shlex.quote(dest_dir)} && "
            f"tar -xf {shlex.quote(temp_path)} -C {shlex.quote(dest_dir)} && "
            f"rm -f {shlex.quote(temp_path)}"
        )

    # ---------------------------------------------------------------- close

    def close(self) -> None:
        self._session.close()


class CloudflareProvider:
    name = "cloudflare"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = dict(config)
        self.api_url = (
            str(config.get("api_url", "")).strip()
            or str(os.environ.get("SANDBOX_API_URL", "")).strip()
            or None
        )
        self.api_key = (
            str(config.get("api_key", "")).strip()
            or str(os.environ.get("SANDBOX_API_KEY", "")).strip()
            or None
        )
        self.timeout = int(config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
        self.health_timeout = float(config.get("health_timeout_seconds", DEFAULT_HEALTH_TIMEOUT))
        self.workspace_root = str(config.get("workspace_root", DEFAULT_WORKSPACE_ROOT)).strip() or DEFAULT_WORKSPACE_ROOT
        if not self.api_url:
            raise RemoteBackendUnavailable(
                "cloudflare provider requires api_url (set SANDBOX_API_URL or pass "
                "--provider-config api_url=...)."
            )
        if not self.api_key:
            raise RemoteBackendUnavailable(
                "cloudflare provider requires api_key (set SANDBOX_API_KEY or pass "
                "--provider-config api_key=...)."
            )

    def provision(self, spec: SandboxSpec) -> SandboxHandle:
        session = requests.Session()
        session.headers["Authorization"] = f"Bearer {self.api_key}"
        session.headers["User-Agent"] = "evo-cloudflare-provider/1"
        try:
            resp = session.post(
                f"{self.api_url.rstrip('/')}/v1/sandbox",
                timeout=30.0,
            )
            if resp.status_code >= 400:
                raise RemoteBackendUnavailable(
                    f"Cloudflare sandbox creation failed: [{resp.status_code}] {resp.text[:1000]}"
                )
            sandbox_id = resp.json().get("id")
        except Exception as exc:
            raise RemoteBackendUnavailable(f"Cloudflare sandbox creation failed: {exc}") from exc

        if not sandbox_id:
            raise RemoteBackendUnavailable("Cloudflare sandbox creation returned no sandbox id")

        handle = SandboxHandle(
            provider=self.name,
            base_url=self.api_url,
            bearer_token=self.api_key,
            native_id=str(sandbox_id),
            metadata={
                "workspace_root": self.workspace_root,
                "bundle_dir": f"{self.workspace_root}/.evo-bundles",
                "api_url": self.api_url,
                "timeout_seconds": self.timeout,
                "health_timeout_seconds": self.health_timeout,
            },
        )

        try:
            client = CloudflareBridgeClient(
                self.api_url,
                self.api_key,
                handle.native_id,
                workspace_root=self.workspace_root,
            )
            client.wait_for_health(timeout_seconds=self.health_timeout)
            deadline = time.monotonic() + self.health_timeout
            while time.monotonic() < deadline:
                if self.is_alive(handle):
                    break
                time.sleep(0.5)
            else:
                raise RemoteBackendUnavailable(
                    f"Cloudflare sandbox {handle.native_id} did not become running "
                    f"within {self.health_timeout}s"
                )
        except Exception:
            try:
                session.delete(f"{self.api_url.rstrip('/')}/v1/sandbox/{handle.native_id}", timeout=30.0)
            except Exception:
                pass
            raise
        finally:
            session.close()

        return handle

    def tear_down(self, handle: SandboxHandle) -> None:
        session = requests.Session()
        session.headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            session.delete(f"{self.api_url.rstrip('/')}/v1/sandbox/{handle.native_id}", timeout=30.0)
        except Exception:
            pass
        finally:
            session.close()

    def is_alive(self, handle: SandboxHandle) -> bool:
        try:
            session = requests.Session()
            session.headers["Authorization"] = f"Bearer {self.api_key}"
            resp = session.get(
                f"{self.api_url.rstrip('/')}/v1/sandbox/{handle.native_id}/running",
                timeout=15.0,
            )
            if resp.status_code >= 400:
                return False
            return bool(resp.json().get("running"))
        except Exception:
            return False

    def build_client(self, handle: SandboxHandle) -> SandboxClient:
        workspace_root = (handle.metadata or {}).get("workspace_root", DEFAULT_WORKSPACE_ROOT)
        return CloudflareBridgeClient(
            self.api_url,
            self.api_key,
            handle.native_id,
            workspace_root=str(workspace_root),
        )


def _build_shell_command(
    command: str,
    args: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    parts = []
    if cwd:
        parts.append(f"cd {shlex.quote(cwd)}")
    if env:
        for key, value in env.items():
            parts.append(f"export {key}={shlex.quote(value)}")
    parts.append("exec " + " ".join([shlex.quote(command), *[shlex.quote(a) for a in args]]))
    return "; ".join(parts)


def _process_wrapper_shell(shell_cmd: str, paths: dict[str, str]) -> str:
    return "\n".join([
        f"stdout={shlex.quote(paths['stdout'])}",
        f"stderr={shlex.quote(paths['stderr'])}",
        f"exit_file={shlex.quote(paths['exit'])}",
        f"pid_file={shlex.quote(paths['pid'])}",
        f"mkdir -p {shlex.quote(paths['dir'])}",
        f"( {shell_cmd} ) >\"$stdout\" 2>\"$stderr\" < /dev/null",
        "code=$?",
        "printf '%s' \"$code\" > \"$exit_file\"",
    ])


def _parse_sse_exec_response(text: str) -> dict[str, Any]:
    stdout = bytearray()
    stderr = bytearray()
    exit_code: int | None = None
    timed_out = False
    for block in text.split("\n\n"):
        event = None
        data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = line[len("data:"):].strip()
        if not event or data is None:
            continue
        if event in {"stdout", "stderr"}:
            try:
                chunk = __import__("base64").b64decode(data)
            except Exception:
                chunk = data.encode("utf-8")
            if event == "stdout":
                stdout.extend(chunk)
            else:
                stderr.extend(chunk)
        elif event == "exit":
            try:
                exit_code = int(json.loads(data).get("exit_code"))
            except Exception:
                exit_code = 0
        elif event == "error":
            timed_out = "timeout" in data.lower()
    return {
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_ms": 0,
    }


def _log_entry(sequence: int, stream: str, data: bytes) -> Any:
    return type("ProcessLogEntry", (), {
        "sequence": sequence,
        "stream": stream,
        "timestamp_ms": int(time.time() * 1000),
        "data": data,
    })()
