"""Hetzner Cloud sandbox provider.

Creates a Hetzner Cloud server, then reuses the SSH bootstrap path to
install sandbox-agent and open the local tunnel.
"""
from __future__ import annotations

import os
from typing import Any

from hcloud import Client
from hcloud.images import Image
from hcloud.locations import Location
from hcloud.server_types import ServerType
from hcloud.ssh_keys import SSHKey
from hcloud._exceptions import APIException

from ..protocol import (
    RemoteBackendUnavailable,
    SandboxHandle,
    SandboxSpec,
)
from ._common import SandboxAgentProviderMixin
from .ssh import SSHProvider


DEFAULT_IMAGE = "ubuntu-24.04"
DEFAULT_TIMEOUT_SECONDS = 3600
DEFAULT_HEALTH_TIMEOUT = 60.0
DEFAULT_SSH_PORT = 22
DEFAULT_SSH_USER = "root"


class HetznerProvider(SandboxAgentProviderMixin):
    name = "hetzner"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = dict(config)
        self.token = str(config.get("token", "")).strip() or None
        self.server_type = str(config.get("server_type", "")).strip()
        self.image = str(config.get("image", DEFAULT_IMAGE)).strip() or DEFAULT_IMAGE
        self.location = str(config.get("location", "")).strip() or None
        self.ssh_key_name = str(config.get("ssh_key_name", "")).strip() or None
        self.key = str(config.get("key", "")).strip() or None
        self.ssh_user = str(config.get("ssh_user", DEFAULT_SSH_USER)).strip() or DEFAULT_SSH_USER
        self.ssh_port = int(config.get("ssh_port", DEFAULT_SSH_PORT))
        self.timeout = int(config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
        self.health_timeout = float(
            config.get("health_timeout_seconds", DEFAULT_HEALTH_TIMEOUT)
        )
        self.keep_warm = _parse_bool(config.get("keep_warm", False))
        if not self.server_type:
            raise RemoteBackendUnavailable(
                "hetzner provider requires server_type (set via --provider-config server_type=...)."
            )
        self._client: Client | None = None

    def provision(self, spec: SandboxSpec) -> SandboxHandle:
        client = self._client_for_use()
        ssh_keys: list[SSHKey] = []
        if self.ssh_key_name:
            try:
                ssh_key = client.ssh_keys.get_all(name=self.ssh_key_name)[0]
            except IndexError as exc:
                raise RemoteBackendUnavailable(
                    f"Hetzner SSH key {self.ssh_key_name!r} was not found in the account."
                ) from exc
            ssh_keys = [ssh_key]

        try:
            kwargs: dict[str, Any] = {
                "name": f"evo-{spec.exp_id}",
                "server_type": ServerType(name=self.server_type),
                "image": Image(name=self.image),
                "ssh_keys": ssh_keys or None,
                "start_after_create": True,
            }
            if self.location:
                kwargs["location"] = Location(name=self.location)
            response = client.servers.create(**kwargs)
            server = response.server
            self._wait_for_server(server.id)
            server = client.servers.get_by_id(server.id)
        except (APIException, Exception) as exc:
            raise RemoteBackendUnavailable(f"Hetzner server creation failed: {exc}") from exc

        public_ip = getattr(getattr(server, "public_net", None), "ipv4", None)
        public_ip = getattr(public_ip, "ip", None) if public_ip is not None else None
        if not public_ip:
            try:
                client.servers.delete(server)
            except Exception:
                pass
            raise RemoteBackendUnavailable("Hetzner server has no public IPv4 address")

        ssh_provider = SSHProvider(
            {
                "host": f"{self.ssh_user}@{public_ip}",
                "key": self.key,
                "port": self.ssh_port,
                "keep_warm": self.keep_warm,
            }
        )
        try:
            handle = ssh_provider.provision(spec)
        except Exception:
            try:
                client.servers.delete(server)
            except Exception:
                pass
            raise

        handle.metadata = dict(handle.metadata or {})
        handle.metadata.update({
            "hetzner_server_id": server.id,
            "hetzner_server_name": server.name,
            "hetzner_public_ip": public_ip,
            "hetzner_server_type": self.server_type,
            "hetzner_image": self.image,
            "hetzner_location": self.location,
            "hetzner_ssh_key_name": self.ssh_key_name,
            "hetzner_ssh_user": self.ssh_user,
            "hetzner_ssh_port": self.ssh_port,
            "hetzner_keep_warm": self.keep_warm,
        })
        return handle

    def tear_down(self, handle: SandboxHandle) -> None:
        server = self._server_for_handle(handle)
        ssh_provider = self._ssh_provider_for_handle(handle)
        try:
            ssh_provider.tear_down(handle)
        finally:
            if not _parse_bool((handle.metadata or {}).get("hetzner_keep_warm", self.keep_warm)):
                try:
                    self._client_for_use().servers.delete(server)
                except Exception:
                    pass

    def is_alive(self, handle: SandboxHandle) -> bool:
        try:
            server = self._server_for_handle(handle)
            if str(getattr(server, "status", "")).lower() != "running":
                return False
        except Exception:
            return False
        return self._ssh_provider_for_handle(handle).is_alive(handle)

    def _client_for_use(self) -> Client:
        if self._client is not None:
            return self._client
        token = self.token or os.environ.get("HCLOUD_TOKEN")
        if not token:
            raise RemoteBackendUnavailable(
                "hetzner provider requires token (set HCLOUD_TOKEN or provider_config.token)."
            )
        self._client = Client(token=token)
        return self._client

    def _wait_for_server(self, server_id: int) -> None:
        import time

        deadline = time.monotonic() + self.timeout
        last_exc: Exception | None = None
        while time.monotonic() < deadline:
            try:
                server = self._client.servers.get_by_id(server_id)
                status = str(getattr(server, "status", "")).lower()
                public_net = getattr(server, "public_net", None)
                ipv4 = getattr(getattr(public_net, "ipv4", None), "ip", None) if public_net else None
                if status == "running" and ipv4:
                    return
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
            time.sleep(1.0)
        raise RemoteBackendUnavailable(
            f"Hetzner server {server_id} did not become running within {self.timeout}s: {last_exc}"
        )

    def _server_for_handle(self, handle: SandboxHandle):
        server_id = (handle.metadata or {}).get("hetzner_server_id")
        if not server_id:
            raise RemoteBackendUnavailable("Hetzner handle missing server id")
        try:
            return self._client_for_use().servers.get_by_id(int(server_id))
        except Exception as exc:
            raise RemoteBackendUnavailable(f"Could not resolve Hetzner server {server_id}: {exc}") from exc

    def _ssh_provider_for_handle(self, handle: SandboxHandle) -> SSHProvider:
        meta = handle.metadata or {}
        public_ip = meta.get("hetzner_public_ip")
        if not public_ip:
            raise RemoteBackendUnavailable("Hetzner handle missing public IP")
        return SSHProvider(
            {
                "host": f"{meta.get('hetzner_ssh_user', self.ssh_user)}@{public_ip}",
                "key": self.key,
                "port": meta.get("hetzner_ssh_port", self.ssh_port),
                "keep_warm": _parse_bool(meta.get("hetzner_keep_warm", self.keep_warm)),
            }
        )


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
