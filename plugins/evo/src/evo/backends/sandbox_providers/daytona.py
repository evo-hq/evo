"""Daytona sandbox provider.

Creates a Daytona sandbox, requests SSH access, then reuses the existing
SSH bootstrap path to install sandbox-agent and open the local tunnel.
"""
from __future__ import annotations

from typing import Any

from daytona import Daytona, DaytonaConfig

from ..protocol import (
    RemoteBackendUnavailable,
    SandboxHandle,
    SandboxSpec,
)
from ._common import SandboxAgentProviderMixin
from .ssh import SSHProvider


DEFAULT_API_URL = "https://app.daytona.io/api"
DEFAULT_TARGET = "us"
DEFAULT_TIMEOUT_SECONDS = 3600
DEFAULT_HEALTH_TIMEOUT = 60.0
DEFAULT_SSH_HOST = "ssh.app.daytona.io"
DEFAULT_SSH_PORT = 22
DEFAULT_SSH_TOKEN_TTL_MINUTES = 60


class DaytonaProvider(SandboxAgentProviderMixin):
    name = "daytona"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = dict(config)
        self.api_key = str(config.get("api_key", "")).strip() or None
        self.api_url = str(config.get("api_url", DEFAULT_API_URL)).strip() or DEFAULT_API_URL
        self.target = str(config.get("target", DEFAULT_TARGET)).strip() or DEFAULT_TARGET
        self.timeout = int(config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
        self.health_timeout = float(
            config.get("health_timeout_seconds", DEFAULT_HEALTH_TIMEOUT)
        )
        self.ssh_host = str(config.get("ssh_host", DEFAULT_SSH_HOST)).strip() or DEFAULT_SSH_HOST
        self.ssh_port = int(config.get("ssh_port", DEFAULT_SSH_PORT))
        self.ssh_token_ttl_minutes = int(
            config.get("ssh_token_ttl_minutes", DEFAULT_SSH_TOKEN_TTL_MINUTES)
        )
        self.sandbox_timeout = int(config.get("sandbox_timeout_seconds", self.timeout))
        self._daytona: Daytona | None = None

    def provision(self, spec: SandboxSpec) -> SandboxHandle:
        try:
            sandbox = self._client().create(timeout=min(spec.timeout_seconds, self.sandbox_timeout))
        except Exception as exc:
            raise RemoteBackendUnavailable(f"Daytona sandbox creation failed: {exc}") from exc

        try:
            ssh_access = sandbox.create_ssh_access(expires_in_minutes=self.ssh_token_ttl_minutes)
        except Exception as exc:
            try:
                sandbox.delete()
            except Exception:
                pass
            raise RemoteBackendUnavailable(
                f"Daytona sandbox {getattr(sandbox, 'id', '<unknown>')} could not create SSH access: {exc}"
            ) from exc

        ssh_user_host = f"{ssh_access.token}@{self.ssh_host}"
        ssh_provider = SSHProvider(
            {
                "host": ssh_user_host,
                "port": self.ssh_port,
                "keep_warm": False,
            }
        )
        try:
            handle = ssh_provider.provision(spec)
        except Exception:
            try:
                sandbox.revoke_ssh_access(token=ssh_access.token)
            except Exception:
                pass
            try:
                sandbox.delete()
            except Exception:
                pass
            raise

        handle.metadata = dict(handle.metadata or {})
        handle.metadata.update({
            "daytona_sandbox_id": getattr(sandbox, "id", ""),
            "daytona_api_url": self.api_url,
            "daytona_target": self.target,
            "daytona_ssh_token": ssh_access.token,
            "daytona_ssh_host": self.ssh_host,
            "daytona_ssh_port": self.ssh_port,
            "daytona_ssh_token_ttl_minutes": self.ssh_token_ttl_minutes,
        })
        return handle

    def tear_down(self, handle: SandboxHandle) -> None:
        sandbox = self._sandbox_for_handle(handle)
        ssh_provider = self._ssh_provider_for_handle(handle)
        try:
            ssh_provider.tear_down(handle)
        finally:
            token = (handle.metadata or {}).get("daytona_ssh_token")
            if token:
                try:
                    sandbox.revoke_ssh_access(token=token)
                except Exception:
                    pass
            try:
                sandbox.delete()
            except Exception:
                pass

    def is_alive(self, handle: SandboxHandle) -> bool:
        try:
            self._sandbox_for_handle(handle)
        except Exception:
            return False
        return self._ssh_provider_for_handle(handle).is_alive(handle)

    def _build_client(self) -> Daytona:
        config_kwargs: dict[str, Any] = {
            "api_url": self.api_url,
            "target": self.target,
        }
        if self.api_key:
            config_kwargs["api_key"] = self.api_key
        try:
            return Daytona(DaytonaConfig(**config_kwargs))
        except Exception as exc:
            raise RemoteBackendUnavailable(f"Daytona SDK initialization failed: {exc}") from exc

    def _client(self) -> Daytona:
        if self._daytona is None:
            self._daytona = self._build_client()
        return self._daytona

    def _sandbox_for_handle(self, handle: SandboxHandle):
        sandbox_id = (handle.metadata or {}).get("daytona_sandbox_id")
        if not sandbox_id:
            raise RemoteBackendUnavailable("Daytona handle missing sandbox id")
        try:
            return self._client().get(sandbox_id)
        except Exception as exc:
            raise RemoteBackendUnavailable(f"Could not resolve Daytona sandbox {sandbox_id}: {exc}") from exc

    def _ssh_provider_for_handle(self, handle: SandboxHandle) -> SSHProvider:
        meta = handle.metadata or {}
        host = meta.get("daytona_ssh_host", self.ssh_host)
        token = meta.get("daytona_ssh_token")
        if not token:
            raise RemoteBackendUnavailable("Daytona handle missing SSH token")
        return SSHProvider(
            {
                "host": f"{token}@{host}",
                "port": meta.get("daytona_ssh_port", self.ssh_port),
                "keep_warm": False,
            }
        )
