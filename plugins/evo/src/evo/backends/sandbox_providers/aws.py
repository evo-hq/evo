"""AWS EC2 sandbox provider.

Creates an EC2 instance, then reuses the existing SSH bootstrap path to
install sandbox-agent and open the local tunnel.
"""
from __future__ import annotations

from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from ..protocol import (
    RemoteBackendUnavailable,
    SandboxHandle,
    SandboxSpec,
)
from .ssh import SSHProvider


DEFAULT_INSTANCE_TYPE = "t3.micro"
DEFAULT_TIMEOUT_SECONDS = 3600
DEFAULT_HEALTH_TIMEOUT = 60.0
DEFAULT_SSH_PORT = 22
DEFAULT_SSH_USER = "ubuntu"


class AWSProvider:
    name = "aws"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = dict(config)
        self.region = str(config.get("region", "")).strip()
        self.image_id = str(config.get("image_id", "")).strip()
        self.key_name = str(config.get("key_name", "")).strip()
        self.key = str(config.get("key", "")).strip() or None
        self.instance_type = str(config.get("instance_type", DEFAULT_INSTANCE_TYPE)).strip() or DEFAULT_INSTANCE_TYPE
        self.subnet_id = str(config.get("subnet_id", "")).strip() or None
        self.security_group_ids = _parse_list(config.get("security_group_ids", ""))
        self.ssh_user = str(config.get("ssh_user", DEFAULT_SSH_USER)).strip() or DEFAULT_SSH_USER
        self.ssh_port = int(config.get("ssh_port", DEFAULT_SSH_PORT))
        self.timeout = int(config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
        self.health_timeout = float(
            config.get("health_timeout_seconds", DEFAULT_HEALTH_TIMEOUT)
        )
        self.keep_warm = _parse_bool(config.get("keep_warm", False))
        if not self.region:
            raise RemoteBackendUnavailable(
                "aws provider requires region (set via --provider-config region=...)."
            )
        if not self.image_id:
            raise RemoteBackendUnavailable(
                "aws provider requires image_id (set via --provider-config image_id=ami-...)."
            )
        if not self.key_name:
            raise RemoteBackendUnavailable(
                "aws provider requires key_name (set via --provider-config key_name=...)."
            )

        self._ec2 = boto3.resource("ec2", region_name=self.region)

    def provision(self, spec: SandboxSpec) -> SandboxHandle:
        try:
            params: dict[str, Any] = {
                "ImageId": self.image_id,
                "InstanceType": self.instance_type,
                "KeyName": self.key_name,
                "MinCount": 1,
                "MaxCount": 1,
            }
            if self.subnet_id:
                params["SubnetId"] = self.subnet_id
            if self.security_group_ids:
                params["SecurityGroupIds"] = self.security_group_ids
            instances = self._ec2.create_instances(**params)
            instance = instances[0]
            instance.wait_until_running()
            instance.reload()
        except (BotoCoreError, ClientError, Exception) as exc:
            raise RemoteBackendUnavailable(f"AWS EC2 instance creation failed: {exc}") from exc

        public_dns = instance.public_dns_name or instance.public_ip_address
        if not public_dns:
            try:
                instance.terminate()
            except Exception:
                pass
            raise RemoteBackendUnavailable("AWS EC2 instance has no public DNS or IP address")

        ssh_provider = SSHProvider(
            {
                "host": f"{self.ssh_user}@{public_dns}",
                "key": self.key,
                "port": self.ssh_port,
                "keep_warm": self.keep_warm,
            }
        )
        try:
            handle = ssh_provider.provision(spec)
        except Exception:
            try:
                instance.terminate()
            except Exception:
                pass
            raise

        handle.metadata = dict(handle.metadata or {})
        handle.metadata.update({
            "aws_instance_id": instance.id,
            "aws_region": self.region,
            "aws_public_dns": public_dns,
            "aws_key_name": self.key_name,
            "aws_instance_type": self.instance_type,
            "aws_ssh_user": self.ssh_user,
            "aws_ssh_port": self.ssh_port,
            "aws_keep_warm": self.keep_warm,
        })
        return handle

    def tear_down(self, handle: SandboxHandle) -> None:
        instance = self._instance_for_handle(handle)
        ssh_provider = self._ssh_provider_for_handle(handle)
        try:
            ssh_provider.tear_down(handle)
        finally:
            if not _parse_bool((handle.metadata or {}).get("aws_keep_warm", self.keep_warm)):
                try:
                    instance.terminate()
                except Exception:
                    pass

    def is_alive(self, handle: SandboxHandle) -> bool:
        try:
            instance = self._instance_for_handle(handle)
            if instance.state and getattr(instance.state, "Name", instance.state) not in {"running"}:
                return False
        except Exception:
            return False
        return self._ssh_provider_for_handle(handle).is_alive(handle)

    def _instance_for_handle(self, handle: SandboxHandle):
        instance_id = (handle.metadata or {}).get("aws_instance_id")
        if not instance_id:
            raise RemoteBackendUnavailable("AWS handle missing instance id")
        return self._ec2.Instance(instance_id)

    def _ssh_provider_for_handle(self, handle: SandboxHandle) -> SSHProvider:
        meta = handle.metadata or {}
        host = meta.get("aws_public_dns")
        if not host:
            raise RemoteBackendUnavailable("AWS handle missing public DNS name")
        return SSHProvider(
            {
                "host": f"{meta.get('aws_ssh_user', self.ssh_user)}@{host}",
                "key": self.key,
                "port": meta.get("aws_ssh_port", self.ssh_port),
                "keep_warm": _parse_bool(meta.get("aws_keep_warm", self.keep_warm)),
            }
        )


def _parse_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
