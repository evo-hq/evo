"""Manual sandbox provider: bring-your-own sandbox-agent.

Doesn't provision or tear down anything. Reads (base_url, bearer_token)
from `provider_config` at init time and returns the same SandboxHandle
on every `provision()` call.

Two real users:
  1. Self-hosted setups: a user runs sandbox-agent on their own VM
     (locally, on a Hetzner box, on a long-lived Modal app, anywhere)
     and points evo at it.
  2. Tests: a fixture spawns sandbox-agent on a free localhost port and
     uses this provider so the integration tests exercise real software
     without burning credits on a managed sandbox provider.

Config keys (read from `--provider-config base_url=...,bearer_token=...`):
  base_url       — required. e.g. http://127.0.0.1:8080
  bearer_token   — optional. matches what sandbox-agent was started with.
"""
from __future__ import annotations

import os
from typing import Any

import requests

from ..protocol import (
    RemoteBackendUnavailable,
    SandboxHandle,
    SandboxProvider,
    SandboxSpec,
)


class ManualProvider:
    name = "manual"

    def __init__(self, config: dict[str, Any]) -> None:
        # Allow env-var overrides for the test fixture, which may not have
        # an easy way to feed config through `--provider-config`.
        base_url = config.get("base_url") or os.environ.get("EVO_SANDBOX_BASE_URL")
        if not base_url:
            raise RemoteBackendUnavailable(
                "manual provider requires base_url (set via "
                "`--provider-config base_url=http://...` at evo init, "
                "or via the EVO_SANDBOX_BASE_URL env var)."
            )
        self.base_url = base_url.rstrip("/")
        self.bearer_token = (
            config.get("bearer_token")
            or os.environ.get("EVO_SANDBOX_BEARER_TOKEN")
            or ""
        )
        # Optional override for where evo treats as the in-sandbox
        # workspace root. Defaults to /workspace/repo (matches Modal's
        # image layout). For self-hosted setups on macOS or anywhere the
        # user can't write under /workspace, set this to a writable path
        # via `--provider-config workspace_root=/tmp/evo-sandbox`.
        self.workspace_root = (
            config.get("workspace_root")
            or os.environ.get("EVO_SANDBOX_WORKSPACE_ROOT")
            or "/workspace/repo"
        )
        self.bundle_dir = (
            config.get("bundle_dir")
            or os.environ.get("EVO_SANDBOX_BUNDLE_DIR")
            or "/tmp/evo-bundles"
        )

    def provision(self, spec: SandboxSpec) -> SandboxHandle:
        # `spec.bearer_token` is generated fresh by the backend on each
        # allocate; we ignore it here because the manual sandbox-agent
        # was started with a fixed token. Echo our configured token back.
        return SandboxHandle(
            provider=self.name,
            base_url=self.base_url,
            bearer_token=self.bearer_token,
            native_id=f"manual-{self.base_url}",
            metadata={
                "managed": False,
                "workspace_root": self.workspace_root,
                "bundle_dir": self.bundle_dir,
            },
        )

    def tear_down(self, handle: SandboxHandle) -> None:
        # No-op: the user owns the lifecycle of a manual sandbox.
        return

    def is_alive(self, handle: SandboxHandle) -> bool:
        # Cheap health probe so the orchestrator can detect a dead BYO
        # sandbox without round-tripping through the full client.
        try:
            headers = {}
            if self.bearer_token:
                headers["Authorization"] = f"Bearer {self.bearer_token}"
            resp = requests.get(
                f"{self.base_url}/v1/health", headers=headers, timeout=2.0
            )
            return resp.status_code == 200
        except requests.RequestException:
            return False
