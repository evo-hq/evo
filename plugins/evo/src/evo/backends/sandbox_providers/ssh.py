"""SSH sandbox provider.

Brings up sandbox-agent on an existing machine reachable over SSH, then
reaches it through a local SSH tunnel. This keeps the orchestrator-side
HTTP path uniform with managed providers while avoiding firewall setup.
"""
from __future__ import annotations

import hashlib
import secrets
import shlex
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from ...sandbox_client import SandboxAgentClient
from ..protocol import (
    RemoteBackendUnavailable,
    SandboxHandle,
    SandboxProvider,
    SandboxSpec,
)
from ._common import SandboxAgentProviderMixin


SANDBOX_AGENT_VERSION = "0.4.x"
SANDBOX_AGENT_INSTALL_URL = (
    f"https://releases.rivet.dev/sandbox-agent/{SANDBOX_AGENT_VERSION}/install.sh"
)
DEFAULT_HEALTH_TIMEOUT = 60.0
CONTROL_DIR = Path("/tmp/evo-ssh-control")
REMOTE_SHARED_ROOT = "/tmp/evo-ssh-shared"
REMOTE_SANDBOX_ROOT = "/tmp/evo-ssh-sandboxes"


class SSHProvider(SandboxAgentProviderMixin):
    name = "ssh"

    def __init__(self, config: dict[str, Any]) -> None:
        host = str(config.get("host", "")).strip()
        if not host:
            raise RemoteBackendUnavailable(
                "ssh provider requires host (set via "
                "`--provider-config host=user@host` or use "
                "`--remote ssh:user@host`)."
            )
        self.host = host
        self.key = str(config.get("key", "")).strip() or None
        try:
            self.port = int(config.get("port", 22))
        except (TypeError, ValueError) as exc:
            raise RemoteBackendUnavailable(
                f"ssh provider port must be an integer, got {config.get('port')!r}."
            ) from exc
        tunnel_port = config.get("tunnel_port")
        if tunnel_port in (None, ""):
            self.tunnel_port: int | None = None
        else:
            try:
                self.tunnel_port = int(tunnel_port)
            except (TypeError, ValueError) as exc:
                raise RemoteBackendUnavailable(
                    "ssh provider tunnel_port must be an integer when set."
                ) from exc
        self.keep_warm = _parse_bool(config.get("keep_warm", False))
        self.health_timeout = float(
            config.get("health_timeout_seconds", DEFAULT_HEALTH_TIMEOUT)
        )

        if not _command_exists("ssh"):
            raise RemoteBackendUnavailable(
                "ssh provider requested but no `ssh` binary is available on PATH."
            )
        if not _command_exists("curl"):
            raise RemoteBackendUnavailable(
                "ssh provider requires `curl` on the orchestrator host."
            )

        self._host_hash = hashlib.sha256(
            f"{self.host}:{self.port}".encode("utf-8")
        ).hexdigest()[:12]

    def provision(self, spec: SandboxSpec) -> SandboxHandle:
        native_id = f"ssh-{secrets.token_hex(6)}"
        remote_root = f"{REMOTE_SANDBOX_ROOT}/{native_id}"
        install_home = f"{REMOTE_SHARED_ROOT}/{self._host_hash}/home"
        workspace_root = f"{remote_root}/repo"
        bundle_dir = f"{remote_root}/bundles"
        log_path = f"{remote_root}/sandbox-agent.log"
        pid_path = f"{remote_root}/sandbox-agent.pid"

        self._run_remote(
            "\n".join([
                "set -e",
                f"mkdir -p {shlex.quote(install_home)}",
                f"mkdir -p {shlex.quote(remote_root)}",
                f"mkdir -p {shlex.quote(workspace_root)}",
                f"mkdir -p {shlex.quote(bundle_dir)}",
            ])
        )
        agent_bin = self._ensure_agent_installed(install_home)
        remote_port = self._pick_remote_port()
        self._start_agent(
            agent_bin=agent_bin,
            token=spec.bearer_token,
            exposed_port=remote_port,
            env=spec.env,
            pid_path=pid_path,
            log_path=log_path,
            remote_root=remote_root,
        )

        local_port = self.tunnel_port or _free_local_port()
        control_path = self._control_path(native_id)
        self._ensure_tunnel(
            control_path=control_path,
            local_port=local_port,
            remote_port=remote_port,
        )
        handle = SandboxHandle(
            provider=self.name,
            base_url=f"http://127.0.0.1:{local_port}",
            bearer_token=spec.bearer_token,
            native_id=native_id,
            metadata={
                "host": self.host,
                "ssh_port": self.port,
                "key": self.key,
                "remote_port": remote_port,
                "tunnel_port": local_port,
                "control_path": str(control_path),
                "pid_path": pid_path,
                "log_path": log_path,
                "remote_root": remote_root,
                "workspace_root": workspace_root,
                "bundle_dir": bundle_dir,
                "install_home": install_home,
                "agent_bin": agent_bin,
                "keep_warm": self.keep_warm,
            },
        )
        try:
            with SandboxAgentClient(handle.base_url, handle.bearer_token) as client:
                client.wait_for_health(timeout_seconds=self.health_timeout, poll_interval=0.5)
        except Exception as exc:
            try:
                self.tear_down(handle)
            except Exception:
                pass
            raise RemoteBackendUnavailable(
                f"SSH sandbox on {self.host} did not become healthy within "
                f"{self.health_timeout}s: {exc}"
            ) from exc
        return handle

    def tear_down(self, handle: SandboxHandle) -> None:
        meta = handle.metadata or {}
        control_path = Path(meta.get("control_path", ""))
        pid_path = meta.get("pid_path")
        remote_root = meta.get("remote_root")
        keep_warm = _parse_bool(meta.get("keep_warm", self.keep_warm))

        if not keep_warm and pid_path:
            self._run_remote(
                "\n".join([
                    "set -e",
                    f"if [ -s {shlex.quote(pid_path)} ]; then",
                    f"  pid=$(cat {shlex.quote(pid_path)})",
                    "  kill \"$pid\" 2>/dev/null || true",
                    "  i=0",
                    "  while kill -0 \"$pid\" 2>/dev/null; do",
                    "    i=$((i + 1))",
                    "    if [ \"$i\" -ge 20 ]; then",
                    "      kill -9 \"$pid\" 2>/dev/null || true",
                    "      break",
                    "    fi",
                    "    sleep 0.25",
                    "  done",
                    "fi",
                    f"rm -f {shlex.quote(pid_path)}",
                    (
                        f"rm -rf {shlex.quote(remote_root)}"
                        if remote_root
                        else ":"
                    ),
                ]),
                check=False,
            )
        self._close_tunnel(control_path)

    def is_alive(self, handle: SandboxHandle) -> bool:
        meta = handle.metadata or {}
        pid_path = meta.get("pid_path")
        remote_port = meta.get("remote_port")
        tunnel_port = meta.get("tunnel_port")
        control_path_str = meta.get("control_path")
        if not pid_path or remote_port is None or tunnel_port is None or not control_path_str:
            return False
        ping = self._run_remote("printf ok", check=False)
        if ping.returncode != 0:
            return False
        proc = self._run_remote(
            f"test -s {shlex.quote(pid_path)} && kill -0 \"$(cat {shlex.quote(pid_path)})\"",
            check=False,
        )
        if proc.returncode != 0:
            return False
        control_path = Path(control_path_str)
        try:
            self._ensure_tunnel(
                control_path=control_path,
                local_port=int(tunnel_port),
                remote_port=int(remote_port),
            )
        except RemoteBackendUnavailable:
            return False
        try:
            with SandboxAgentClient(handle.base_url, handle.bearer_token) as client:
                client.health()
            return True
        except Exception:
            return False

    def _ensure_agent_installed(self, install_home: str) -> str:
        resolve_script = f"""
set -e
for candidate in \
  /usr/local/bin/sandbox-agent \
  {shlex.quote(f"{install_home}/.local/bin/sandbox-agent")} \
  "$(command -v sandbox-agent 2>/dev/null || true)"
do
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then
    printf '%s\\n' "$candidate"
    exit 0
  fi
done
exit 1
"""
        resolved = self._run_remote(resolve_script, check=False)
        if resolved.returncode == 0 and resolved.stdout.strip():
            return resolved.stdout.strip()

        install_script = "\n".join([
            "set -e",
            f"mkdir -p {shlex.quote(install_home)}",
            (
                f"HOME={shlex.quote(install_home)} "
                f"sh -lc {shlex.quote(f'curl -fsSL {SANDBOX_AGENT_INSTALL_URL} | sh')}"
            ),
            resolve_script,
        ])
        installed = self._run_remote(install_script, check=False)
        if installed.returncode != 0 or not installed.stdout.strip():
            stderr = (installed.stderr or "").strip()
            raise RemoteBackendUnavailable(
                "ssh provider could not install sandbox-agent on the remote "
                f"host {self.host}. stderr: {stderr[:500]}"
            )
        return installed.stdout.strip()

    def _start_agent(
        self,
        *,
        agent_bin: str,
        token: str,
        exposed_port: int,
        env: dict[str, str],
        pid_path: str,
        log_path: str,
        remote_root: str,
    ) -> None:
        env_prefix = " ".join(
            f"{key}={shlex.quote(value)}" for key, value in sorted(env.items())
        )
        env_cmd = f"env {env_prefix} " if env_prefix else ""
        command = "\n".join([
            "set -e",
            f"mkdir -p {shlex.quote(remote_root)}",
            f"if [ -s {shlex.quote(pid_path)} ] && kill -0 \"$(cat {shlex.quote(pid_path)})\" 2>/dev/null; then",
            f"  kill \"$(cat {shlex.quote(pid_path)})\" 2>/dev/null || true",
            "  sleep 0.5",
            "fi",
            f"rm -f {shlex.quote(pid_path)}",
            (
                f"nohup {env_cmd}{shlex.quote(agent_bin)} server "
                f"--token={shlex.quote(token)} "
                f"--host 127.0.0.1 "
                f"--port {exposed_port} "
                f"> {shlex.quote(log_path)} 2>&1 < /dev/null &"
            ),
            f"echo $! > {shlex.quote(pid_path)}",
        ])
        started = self._run_remote(command, check=False)
        if started.returncode != 0:
            raise RemoteBackendUnavailable(
                f"ssh provider failed to start sandbox-agent on {self.host}: "
                f"{(started.stderr or '').strip()[:500]}"
            )

    def _pick_remote_port(self) -> int:
        cmd = (
            "python3 -c "
            + shlex.quote(
                "import socket;"
                "s=socket.socket();"
                "s.bind(('127.0.0.1', 0));"
                "print(s.getsockname()[1]);"
                "s.close()"
            )
        )
        result = self._run_remote(cmd, check=False)
        if result.returncode != 0 or not result.stdout.strip():
            raise RemoteBackendUnavailable(
                f"ssh provider could not find a free remote port on {self.host}. "
                f"stderr: {(result.stderr or '').strip()[:500]}"
            )
        return int(result.stdout.strip())

    def _ensure_tunnel(
        self, *, control_path: Path, local_port: int, remote_port: int
    ) -> None:
        if self._control_alive(control_path):
            return
        control_path.parent.mkdir(parents=True, exist_ok=True)
        if control_path.exists():
            control_path.unlink()
        cmd = self._ssh_base_args() + [
            "-S", str(control_path),
            "-o", "ControlMaster=yes",
            "-o", "ControlPersist=yes",
            "-o", "ExitOnForwardFailure=yes",
            "-f",
            "-N",
            "-L", f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
            self.host,
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RemoteBackendUnavailable(
                f"ssh provider could not establish a tunnel to {self.host}: "
                f"{proc.stderr.strip()[:500]}"
            )

    def _close_tunnel(self, control_path: Path) -> None:
        if not control_path:
            return
        if self._control_alive(control_path):
            subprocess.run(
                self._ssh_base_args()
                + ["-S", str(control_path), "-O", "exit", self.host],
                capture_output=True,
                text=True,
                check=False,
            )
        if control_path.exists():
            control_path.unlink()

    def _control_alive(self, control_path: Path) -> bool:
        if not control_path.exists():
            return False
        proc = subprocess.run(
            self._ssh_base_args()
            + ["-S", str(control_path), "-O", "check", self.host],
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode == 0

    def _control_path(self, native_id: str) -> Path:
        return CONTROL_DIR / f"{self._host_hash}-{native_id}.sock"

    def _ssh_base_args(self) -> list[str]:
        args = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-p", str(self.port),
        ]
        if self.key:
            args.extend(["-i", self.key])
        return args

    def _run_remote(
        self,
        script: str,
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            self._ssh_base_args() + [self.host, f"sh -lc {shlex.quote(script)}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if check and proc.returncode != 0:
            raise RemoteBackendUnavailable(
                f"ssh command failed against {self.host}: "
                f"{proc.stderr.strip()[:500]}"
            )
        return proc


def _command_exists(name: str) -> bool:
    return subprocess.run(
        ["sh", "-lc", f"command -v {shlex.quote(name)}"],
        capture_output=True,
        text=True,
        check=False,
    ).returncode == 0


def _free_local_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
