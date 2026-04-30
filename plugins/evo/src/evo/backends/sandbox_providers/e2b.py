"""E2B sandbox provider.

Uses E2B's Python SDK to provision a sandbox, boot sandbox-agent inside it,
and expose that service on a public E2B host URL.
"""
from __future__ import annotations

from typing import Any

from e2b import Sandbox
from e2b.exceptions import AuthenticationException

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


DEFAULT_TEMPLATE = "base"
DEFAULT_TIMEOUT_SECONDS = 3600
DEFAULT_HEALTH_TIMEOUT = 60.0
DEFAULT_ROOT = "/tmp/evo-e2b"


class E2BProvider(SandboxAgentProviderMixin):
    name = "e2b"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = dict(config)
        self.template = str(config.get("template", DEFAULT_TEMPLATE)).strip() or DEFAULT_TEMPLATE
        self.api_key = str(config.get("api_key", "")).strip() or None
        self.domain = str(config.get("domain", "")).strip() or None
        self.root = str(config.get("root", DEFAULT_ROOT)).strip() or DEFAULT_ROOT
        self.timeout = int(config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
        self.health_timeout = float(
            config.get("health_timeout_seconds", DEFAULT_HEALTH_TIMEOUT)
        )
        self.allow_internet_access = _parse_bool(
            config.get("allow_internet_access", True)
        )
        self.secure = _parse_bool(config.get("secure", True))

    def provision(self, spec: SandboxSpec) -> SandboxHandle:
        try:
            sandbox = Sandbox.create(
                template=self.template,
                timeout=min(spec.timeout_seconds, self.timeout),
                envs=spec.env or None,
                allow_internet_access=self.allow_internet_access,
                secure=self.secure,
                api_key=self.api_key,
                domain=self.domain,
            )
        except AuthenticationException as exc:
            raise RemoteBackendUnavailable(
                "E2B provider requested but authentication failed. "
                "Set E2B_API_KEY or pass --provider-config api_key=..."
            ) from exc
        except Exception as exc:
            raise RemoteBackendUnavailable(
                f"E2B sandbox creation failed: {exc}"
            ) from exc

        sandbox_id = sandbox.sandbox_id
        install_root = f"{self.root}/{sandbox_id}"
        workspace_root = f"{install_root}/repo"
        bundle_dir = f"{install_root}/bundles"
        bin_path = f"{install_root}/bin/sandbox-agent"
        log_path = f"{install_root}/sandbox-agent.log"
        pid_path = f"{install_root}/sandbox-agent.pid"

        bootstrap = "\n".join([
            "set -e",
            f"mkdir -p {shell_quote(install_root)}/bin",
            f"mkdir -p {shell_quote(workspace_root)}",
            f"mkdir -p {shell_quote(bundle_dir)}",
            "command -v git >/dev/null 2>&1 || {",
            "  echo 'git is required in the E2B template for evo remote mode' >&2",
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
            result = sandbox.commands.run(
                bootstrap,
                cwd="/",
                timeout=120,
            )
        except Exception as exc:
            self._kill_sandbox(sandbox_id)
            raise RemoteBackendUnavailable(
                f"E2B sandbox bootstrap failed: {exc}"
            ) from exc
        if result.exit_code != 0:
            self._kill_sandbox(sandbox_id)
            raise RemoteBackendUnavailable(
                f"E2B sandbox bootstrap failed: {result.stderr or result.stdout}"
            )

        base_url = f"https://{sandbox.get_host(spec.exposed_port)}"
        handle = SandboxHandle(
            provider=self.name,
            base_url=base_url,
            bearer_token=spec.bearer_token,
            native_id=sandbox_id,
            metadata={
                "template": self.template,
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
                label=f"E2B sandbox {sandbox_id}",
            )
        except Exception:
            try:
                self.tear_down(handle)
            except Exception:
                pass
            raise
        return handle

    def tear_down(self, handle: SandboxHandle) -> None:
        self._kill_sandbox(handle.native_id)

    def is_alive(self, handle: SandboxHandle) -> bool:
        try:
            info = Sandbox.get_info(
                handle.native_id,
                api_key=self.api_key,
                domain=self.domain,
            )
        except Exception:
            return False
        state = getattr(info.state, "value", info.state)
        return state == "running"

    def _kill_sandbox(self, sandbox_id: str) -> None:
        try:
            Sandbox.kill(
                sandbox_id,
                api_key=self.api_key,
                domain=self.domain,
            )
        except Exception:
            pass


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
