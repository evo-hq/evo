"""Modal sandbox provider.

Provisions a Modal sandbox running rivet-dev/sandbox-agent on an exposed
HTTPS port, returns its URL + bearer token, tears it down on demand.

Modal SDK reference: https://modal.com/docs/reference/modal.Sandbox
"""
from __future__ import annotations

import os
from typing import Any

# Lazy-import the modal SDK at module-import-time, but raise our typed
# error instead of letting ImportError propagate. The registry's
# `_load_modal` translates this into a user-facing actionable message.
import modal  # noqa: F401 — presence-checked at import; see registry

from ..protocol import (
    RemoteBackendUnavailable,
    SandboxHandle,
    SandboxProvider,
    SandboxSpec,
)
from ._common import (
    SANDBOX_AGENT_BINARY_URL,
    wait_for_sandbox_agent,
)


# Provider-side defaults. Overridable via provider_config at init time.
DEFAULT_APP_NAME = "evo-sandbox"
DEFAULT_TIMEOUT_SECONDS = 3600          # Modal hard-caps to 24h; we cap lower
DEFAULT_HEALTH_TIMEOUT = 60.0           # post-provision health-check budget


class ModalProvider:
    """SandboxProvider implementation backed by Modal's Sandbox API.

    One ModalProvider instance is created per workspace at backend-load
    time. It owns the modal.App reference and re-uses it across
    provisions; the app exists for the lifetime of the orchestrator
    process. Modal sandboxes themselves are short-lived (one per
    experiment in POC).
    """

    name = "modal"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self._app_name = config.get("app_name", DEFAULT_APP_NAME)
        self._timeout = int(config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
        self._health_timeout = float(
            config.get("health_timeout_seconds", DEFAULT_HEALTH_TIMEOUT)
        )
        self._image_extra_apt = self._parse_list(config.get("apt_install", ""))
        self._image_extra_pip = self._parse_list(config.get("pip_install", ""))
        self._region = config.get("region")  # optional

        # Built lazily on first provision. Modal's Image build is cached
        # by content hash, so subsequent provisions skip the build.
        self._app: modal.App | None = None
        self._image: modal.Image | None = None

    # ---------------------------------------------------------------- public

    def provision(self, spec: SandboxSpec) -> SandboxHandle:
        """Spin up a Modal sandbox running sandbox-agent on `spec.exposed_port`.

        Steps:
          1. Build/reuse the image (sandbox-agent baked in).
          2. Modal.Sandbox.create with that image, env vars, and the
             encrypted port for the HTTPS tunnel.
          3. Resolve the HTTPS tunnel URL.
          4. Poll /v1/health from this process until 200 (orchestrator-side
             readiness check; complements Modal's own startup probe).
        """
        from ..protocol import RemoteBackendUnavailable as _RBU  # local alias

        try:
            self._ensure_app_and_image()
        except modal.exception.Error as exc:
            raise RemoteBackendUnavailable(
                f"Modal SDK is installed but the API call failed. Likely "
                f"causes: not authenticated (run `modal token new`), or "
                f"network unreachable. Original error: {exc}"
            ) from exc

        env: dict[str, str] = dict(spec.env)

        # sandbox-agent's documented CLI: `sandbox-agent server --token X
        # --host 0.0.0.0 --port N`. Modal's HTTPS tunnel terminates TLS
        # at Modal's edge; the in-container daemon listens plain HTTP on
        # 0.0.0.0 so the tunnel can reach it (loopback would hide it).
        # Token-as-CLI-flag does leak to /proc/<pid>/cmdline; secrets
        # redaction (deferred per SPEC roadmap) is the eventual fix.
        sb_kwargs: dict[str, Any] = {
            "app": self._app,
            "image": self._image,
            "timeout": min(spec.timeout_seconds, self._timeout),
            "encrypted_ports": [spec.exposed_port],
        }
        if self._region:
            sb_kwargs["region"] = self._region

        # Enable build-log output so a failed Image build prints the
        # actual reason instead of just "Image build failed". Idempotent.
        modal.enable_output()

        try:
            sandbox = modal.Sandbox.create(
                "/usr/local/bin/sandbox-agent",
                "server",
                "--token", spec.bearer_token,
                "--host", "0.0.0.0",
                "--port", str(spec.exposed_port),
                env=env,
                **sb_kwargs,
            )
        except modal.exception.Error as exc:
            raise RemoteBackendUnavailable(
                f"Modal Sandbox.create failed: {exc}"
            ) from exc

        # Resolve the tunnel URL. Modal exposes HTTPS-terminated tunnels
        # when `encrypted_ports=` is set at creation.
        try:
            tunnels = sandbox.tunnels()
            tunnel = tunnels[spec.exposed_port]
            base_url = tunnel.url
        except (KeyError, AttributeError) as exc:
            sandbox.terminate()
            raise RemoteBackendUnavailable(
                f"Modal sandbox started but did not expose an HTTPS tunnel "
                f"on port {spec.exposed_port}: {exc}. Confirm "
                f"encrypted_ports is configured."
            ) from exc

        handle = SandboxHandle(
            provider=self.name,
            base_url=base_url,
            bearer_token=spec.bearer_token,
            native_id=sandbox.object_id,
            metadata={"app_name": self._app_name},
        )

        # Orchestrator-side health check. Modal's startup probe ensures the
        # container's running, but we also need sandbox-agent to be
        # listening. Poll /v1/health.
        try:
            wait_for_sandbox_agent(
                handle.base_url,
                handle.bearer_token,
                timeout_s=self._health_timeout,
                label=f"Modal sandbox at {handle.base_url}",
            )
        except Exception:
            try:
                sandbox.terminate()
            except Exception:
                pass
            raise

        return handle

    def tear_down(self, handle: SandboxHandle) -> None:
        """Best-effort terminate. Modal sandboxes also auto-terminate on
        their configured timeout, so this is a fast cleanup not a leak fix."""
        try:
            sandbox = modal.Sandbox.from_id(handle.native_id)
            sandbox.terminate()
        except modal.exception.Error:
            # Already gone, network blip, etc. Don't raise -- caller's
            # state cleanup proceeds regardless.
            pass

    def is_alive(self, handle: SandboxHandle) -> bool:
        try:
            sandbox = modal.Sandbox.from_id(handle.native_id)
            # Modal's poll() returns the exit_code if terminated, None if
            # still running. We treat None as alive.
            return sandbox.poll() is None
        except modal.exception.Error:
            return False

    # ---------------------------------------------------------------- internal

    def _ensure_app_and_image(self) -> None:
        """Build the modal.App and modal.Image once per provider instance.

        Modal caches the image by content hash, so the first provision
        builds (~30-60s) and subsequent provisions reuse the cached layer
        without rebuilding.
        """
        if self._app is not None and self._image is not None:
            return

        # Image: debian-slim + git + ripgrep + sandbox-agent binary.
        image = modal.Image.debian_slim().apt_install(
            "git", "ripgrep", "curl", "ca-certificates", "tar",
            *self._image_extra_apt,
        ).run_commands(
            f"curl -fsSL {SANDBOX_AGENT_BINARY_URL} > /usr/local/bin/sandbox-agent",
            "chmod +x /usr/local/bin/sandbox-agent",
        )
        if self._image_extra_pip:
            image = image.pip_install(*self._image_extra_pip)

        # App: looked up by name to allow multiple workspaces to share one
        # cached image build. Lookup-or-create.
        app = modal.App.lookup(self._app_name, create_if_missing=True)

        self._app = app
        self._image = image
    @staticmethod
    def _parse_list(value: Any) -> list[str]:
        """Allow apt_install / pip_install to be passed as either a list
        or a comma-separated string (which is what `--provider-config k=v`
        gives us)."""
        if not value:
            return []
        if isinstance(value, list):
            return [v for v in value if v]
        return [item.strip() for item in str(value).split(",") if item.strip()]
