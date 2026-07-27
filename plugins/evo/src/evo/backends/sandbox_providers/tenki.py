"""Tenki sandbox provider.

Uses Tenki's Python SDK (`tenki`) to provision a sandbox session,
boot sandbox-agent inside it, and expose that service on a public Tenki
preview URL. Requires an x86_64 Tenki image (sandbox-agent ships as an
x86_64 musl binary). The API key selects the workspace automatically;
an explicit workspace id remains available as an advanced override.

Tenki SDK reference: https://tenki.cloud/docs/sandbox/sdk
"""
from __future__ import annotations

import os
import time
from typing import Any

from tenki import (
    Client,
    MissingAuthTokenError,
    Sandbox,
    SessionNotFoundError,
    UnauthorizedError,
)

from ...sandbox_client import SandboxAgentClient
from ..protocol import (
    RemoteBackendUnavailable,
    SandboxHandle,
    SandboxSpec,
)
from ._common import (
    install_sandbox_agent_script,
    SandboxAgentProviderMixin,
    shell_quote,
    wait_for_sandbox_agent,
)


DEFAULT_NAME_PREFIX = "evo-sandbox"
DEFAULT_TIMEOUT_SECONDS = 3600
DEFAULT_CREATE_TIMEOUT_SECONDS = 180.0
DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS = 180.0
DEFAULT_HEALTH_TIMEOUT = 60.0
DEFAULT_ROOT = "/home/tenki/evo"

_AUTH_HINT = (
    "Set TENKI_API_KEY (or TENKI_AUTH_TOKEN), or pass "
    "--provider-config auth_token=tk_..."
)
UPLOAD_CHUNK_BYTES = 1024 * 1024
MIN_CPU_CORES = 1
MAX_CPU_CORES = 16
MIN_MEMORY_MB = 128
MAX_MEMORY_MB = 65536
MIN_DISK_SIZE_GB = 5
MAX_DISK_SIZE_GB = 100


class TenkiSandboxClient(SandboxAgentClient):
    """sandbox-agent client that routes bulk uploads over Tenki's data plane.

    sandbox-agent caps HTTP request bodies at 2 MiB with no override flag,
    which 413s any parent-commit bundle larger than that. Tenki's SDK
    offers a chunked streaming write on its authenticated data plane, so
    this client sends the bundle tar through that channel and extracts it
    with an in-sandbox exec. Everything else stays on sandbox-agent.
    """

    def __init__(
        self,
        base_url: str,
        bearer_token: str | None,
        *,
        provider: "TenkiProvider",
        native_id: str,
    ) -> None:
        super().__init__(base_url, bearer_token)
        self._provider = provider
        self._native_id = native_id

    def clone(self) -> "TenkiSandboxClient":
        return TenkiSandboxClient(
            self.base_url,
            self.bearer_token or None,
            provider=self._provider,
            native_id=self._native_id,
        )

    def fs_upload_batch(self, dest_dir: str, tar_bytes: bytes) -> None:
        tar_path = f"{dest_dir.rstrip('/')}/.evo-upload.tar"
        chunks = (
            tar_bytes[i : i + UPLOAD_CHUNK_BYTES]
            for i in range(0, len(tar_bytes), UPLOAD_CHUNK_BYTES)
        )
        with self._provider._client() as client:
            sandbox = client.get(self._native_id)
            sandbox.exec("mkdir", "-p", dest_dir, check=True)
            sandbox.fs.write_stream(tar_path, chunks)
            sandbox.exec("tar", "-xf", tar_path, "-C", dest_dir, check=True)
            sandbox.exec("rm", "-f", tar_path)


class TenkiProvider(SandboxAgentProviderMixin):
    name = "tenki"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = dict(config)
        self.auth_token = (
            str(config.get("auth_token", "")).strip()
            or str(config.get("api_key", "")).strip()
            or None
        )
        self.base_url = str(config.get("base_url", "")).strip() or None
        self.workspace_id = (
            str(config.get("workspace_id", "")).strip()
            or os.environ.get("TENKI_WORKSPACE_ID", "").strip()
            or None
        )
        self.name_prefix = (
            str(config.get("name_prefix", DEFAULT_NAME_PREFIX)).strip()
            or DEFAULT_NAME_PREFIX
        )
        self.image = str(config.get("image", "")).strip() or None
        self.snapshot_id = str(config.get("snapshot_id", "")).strip() or None
        self.cpu_cores = _parse_positive_int(config.get("cpu_cores"), "cpu_cores")
        self.memory_mb = _parse_positive_int(config.get("memory_mb"), "memory_mb")
        self.disk_size_gb = _parse_positive_int(
            config.get("disk_size_gb"), "disk_size_gb"
        )
        self.idle_timeout_minutes = _parse_positive_int(
            config.get("idle_timeout_minutes"), "idle_timeout_minutes"
        )
        _validate_create_resources(
            self.cpu_cores, self.memory_mb, self.disk_size_gb
        )
        self.root = str(config.get("root", DEFAULT_ROOT)).strip() or DEFAULT_ROOT
        self.timeout = int(config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
        self.create_timeout = float(
            config.get("create_timeout_seconds", DEFAULT_CREATE_TIMEOUT_SECONDS)
        )
        self.bootstrap_timeout = float(
            config.get("bootstrap_timeout_seconds", DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS)
        )
        self.health_timeout = float(
            config.get("health_timeout_seconds", DEFAULT_HEALTH_TIMEOUT)
        )

    def provision(self, spec: SandboxSpec) -> SandboxHandle:
        max_duration = min(spec.timeout_seconds, self.timeout)
        create_kwargs: dict[str, Any] = {
            "name": self.name_prefix,
            "wait": True,
            "timeout": self.create_timeout,
            "allow_inbound": True,
            "allow_outbound": True,
            "max_duration": max_duration,
            "env": spec.env or None,
        }
        if self.workspace_id:
            create_kwargs["workspace_id"] = self.workspace_id
        if self.cpu_cores is not None:
            create_kwargs["cpu_cores"] = self.cpu_cores
        if self.memory_mb is not None:
            create_kwargs["memory_mb"] = self.memory_mb
        if self.disk_size_gb is not None:
            create_kwargs["disk_size_gb"] = self.disk_size_gb
        if self.idle_timeout_minutes is not None:
            create_kwargs["idle_timeout_minutes"] = self.idle_timeout_minutes
        if self.snapshot_id:
            create_kwargs["snapshot_id"] = self.snapshot_id
        elif self.image:
            create_kwargs["image"] = self.image
        if self.auth_token:
            create_kwargs["auth_token"] = self.auth_token
        if self.base_url:
            create_kwargs["base_url"] = self.base_url

        try:
            sandbox = Sandbox.create(**create_kwargs)
        except (MissingAuthTokenError, UnauthorizedError) as exc:
            raise RemoteBackendUnavailable(
                f"Tenki provider requested but authentication failed. {_AUTH_HINT}"
            ) from exc
        except Exception as exc:
            hint = ""
            if "workspace_id" in str(exc):
                hint = (
                    " Check --provider-config workspace_id=... or unset "
                    "TENKI_WORKSPACE_ID to use the API key's workspace."
                )
            raise RemoteBackendUnavailable(
                f"Tenki sandbox creation failed: {exc}.{hint}"
            ) from exc

        sandbox_id = sandbox.id
        install_root = f"{self.root}/{sandbox_id}"
        workspace_root = f"{install_root}/repo"
        bundle_dir = f"{install_root}/bundles"
        bin_path = f"{install_root}/bin/sandbox-agent"
        log_path = f"{install_root}/sandbox-agent.log"
        pid_path = f"{install_root}/sandbox-agent.pid"

        bootstrap = "\n".join([
            "set -e",
            'if [ "$(uname -m)" != "x86_64" ]; then',
            "  echo \"evo remote mode needs an x86_64 Tenki image; got $(uname -m)\" >&2",
            "  exit 1",
            "fi",
            f"mkdir -p {shell_quote(install_root)}/bin",
            f"mkdir -p {shell_quote(workspace_root)}",
            f"mkdir -p {shell_quote(bundle_dir)}",
            "command -v git >/dev/null 2>&1 || {",
            "  echo 'git is required in the Tenki image for evo remote mode' >&2",
            "  exit 1",
            "}",
            install_sandbox_agent_script(bin_path),
            f"if [ -s {shell_quote(pid_path)} ] && kill -0 \"$(cat {shell_quote(pid_path)})\" 2>/dev/null; then",
            "  exit 0",
            "fi",
            (
                f"nohup {shell_quote(bin_path)} server "
                f"--token={shell_quote(spec.bearer_token)} "
                f"--host 0.0.0.0 --port {spec.exposed_port} "
                f">{shell_quote(log_path)} 2>&1 & echo $! > {shell_quote(pid_path)}"
            ),
            "sleep 0.5",
            f"kill -0 \"$(cat {shell_quote(pid_path)})\"",
        ])
        try:
            result = sandbox.exec(
                "bash", "-lc", bootstrap,
                timeout=self.bootstrap_timeout,
            )
        except Exception as exc:
            note = self._close_or_leak_note(sandbox_id, sandbox)
            raise RemoteBackendUnavailable(
                f"Tenki sandbox bootstrap failed: {exc}{note}"
            ) from exc
        if result.exit_code != 0:
            note = self._close_or_leak_note(sandbox_id, sandbox)
            raise RemoteBackendUnavailable(
                f"Tenki sandbox bootstrap failed: "
                f"{result.stderr_text or result.stdout_text}{note}"
            )

        try:
            exposed = sandbox.expose_port(spec.exposed_port, ttl=max_duration)
        except Exception as exc:
            note = self._close_or_leak_note(sandbox_id, sandbox)
            raise RemoteBackendUnavailable(
                f"Tenki sandbox {sandbox_id} could not expose port "
                f"{spec.exposed_port}: {exc}{note}"
            ) from exc

        base_url = exposed.url
        if not base_url.startswith(("http://", "https://")):
            base_url = f"https://{base_url}"
        handle = SandboxHandle(
            provider=self.name,
            base_url=base_url,
            bearer_token=spec.bearer_token,
            native_id=sandbox_id,
            metadata={
                "workspace_root": workspace_root,
                "bundle_dir": bundle_dir,
                "install_root": install_root,
                "pid_path": pid_path,
                "log_path": log_path,
            },
        )
        try:
            wait_for_sandbox_agent(
                base_url,
                spec.bearer_token,
                timeout_s=self.health_timeout,
                label=f"Tenki sandbox {sandbox_id}",
            )
        except Exception as exc:
            note = self._close_or_leak_note(sandbox_id, sandbox)
            if note:
                raise RemoteBackendUnavailable(f"{exc}{note}") from exc
            raise
        return handle

    def build_client(self, handle: SandboxHandle) -> TenkiSandboxClient:
        return TenkiSandboxClient(
            handle.base_url,
            handle.bearer_token,
            provider=self,
            native_id=handle.native_id,
        )

    def tear_down(self, handle: SandboxHandle) -> None:
        if not self._close_sandbox(handle.native_id):
            raise RemoteBackendUnavailable(
                f"Tenki sandbox {handle.native_id} could not be closed and "
                f"may still be running; close it from the Tenki console or "
                f"with the tenki CLI, or retry."
            )

    def is_alive(self, handle: SandboxHandle) -> bool:
        try:
            with self._client() as client:
                sandbox = client.get(handle.native_id)
                return sandbox.state == "RUNNING"
        except Exception:
            return False

    def _client(self) -> Client:
        kwargs: dict[str, Any] = {}
        if self.auth_token:
            kwargs["auth_token"] = self.auth_token
        if self.base_url:
            kwargs["base_url"] = self.base_url
        try:
            return Client(**kwargs)
        except MissingAuthTokenError as exc:
            raise RemoteBackendUnavailable(
                f"Tenki SDK initialization failed: no auth token. {_AUTH_HINT}"
            ) from exc

    def _close_sandbox(
        self, sandbox_id: str, sandbox: Sandbox | None = None
    ) -> bool:
        if sandbox is not None:
            try:
                sandbox.close_if_open()
                return True
            except SessionNotFoundError:
                return True
            except Exception:
                pass
        for attempt in range(2):
            try:
                with self._client() as client:
                    client.get(sandbox_id).close_if_open()
                return True
            except SessionNotFoundError:
                return True
            except Exception:
                if attempt == 0:
                    time.sleep(1.0)
        return False

    def _close_or_leak_note(
        self, sandbox_id: str, sandbox: Sandbox | None = None
    ) -> str:
        if self._close_sandbox(sandbox_id, sandbox):
            return ""
        return (
            f" Cleanup also failed: sandbox {sandbox_id} may still be "
            f"running and billing; close it from the Tenki console or with "
            f"the tenki CLI."
        )


def _parse_positive_int(value: Any, key: str) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise RemoteBackendUnavailable(
            f"Tenki provider config {key!r} must be an integer, got {value!r}"
        ) from exc
    if parsed <= 0:
        raise RemoteBackendUnavailable(
            f"Tenki provider config {key!r} must be a positive integer, got {parsed}"
        )
    return parsed


def _validate_create_resources(
    cpu_cores: int | None,
    memory_mb: int | None,
    disk_size_gb: int | None,
) -> None:
    """Validate the resource limits accepted by the Tenki create API."""
    if cpu_cores is not None and not MIN_CPU_CORES <= cpu_cores <= MAX_CPU_CORES:
        raise RemoteBackendUnavailable(
            "Tenki provider config rejected: "
            f"cpu_cores must be between {MIN_CPU_CORES} and {MAX_CPU_CORES}"
        )
    if memory_mb is not None:
        if not MIN_MEMORY_MB <= memory_mb <= MAX_MEMORY_MB:
            raise RemoteBackendUnavailable(
                "Tenki provider config rejected: "
                f"memory_mb must be between {MIN_MEMORY_MB} and {MAX_MEMORY_MB}"
            )
        if memory_mb % 2 != 0:
            raise RemoteBackendUnavailable(
                "Tenki provider config rejected: memory_mb must be aligned to 2 MiB"
            )
    if (
        disk_size_gb is not None
        and not MIN_DISK_SIZE_GB <= disk_size_gb <= MAX_DISK_SIZE_GB
    ):
        raise RemoteBackendUnavailable(
            "Tenki provider config rejected: "
            f"disk_size_gb must be between {MIN_DISK_SIZE_GB} "
            f"and {MAX_DISK_SIZE_GB}"
        )
