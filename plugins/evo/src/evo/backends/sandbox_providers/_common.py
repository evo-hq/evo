"""Shared helpers for sandbox providers that bootstrap sandbox-agent."""
from __future__ import annotations

import time

from ...sandbox_client import SandboxAgentClient
from ..protocol import RemoteBackendUnavailable, SandboxHandle


SANDBOX_AGENT_VERSION = "0.4.x"
SANDBOX_AGENT_BINARY_URL = (
    f"https://releases.rivet.dev/sandbox-agent/{SANDBOX_AGENT_VERSION}/"
    "binaries/sandbox-agent-x86_64-unknown-linux-musl"
)


class SandboxAgentProviderMixin:
    """Default client factory for providers that speak sandbox-agent."""

    def build_client(self, handle: SandboxHandle) -> SandboxAgentClient:
        return SandboxAgentClient(handle.base_url, handle.bearer_token)


def install_sandbox_agent_script(bin_path: str) -> str:
    """Shell snippet that installs sandbox-agent to `bin_path` if missing."""
    return "\n".join([
        f"if [ ! -x {shell_quote(bin_path)} ]; then",
        "  if command -v curl >/dev/null 2>&1; then",
        f"    curl -fsSL {shell_quote(SANDBOX_AGENT_BINARY_URL)} -o {shell_quote(bin_path)}",
        "    chmod +x " + shell_quote(bin_path),
        "  else",
        "    command -v python3 >/dev/null 2>&1 || {",
        "      echo 'python3 is required to install sandbox-agent' >&2",
        "      exit 1",
        "    }",
        "    python3 - <<'PY'",
        "import os, stat, urllib.request",
        f"url = {SANDBOX_AGENT_BINARY_URL!r}",
        f"path = {bin_path!r}",
        "req = urllib.request.Request(url, headers={'User-Agent': 'evo-bootstrap/0.4'})",
        "with urllib.request.urlopen(req) as response, open(path, 'wb') as fh:",
        "    fh.write(response.read())",
        "mode = os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH",
        "os.chmod(path, mode)",
        "PY",
        "  fi",
        "fi",
    ])


def wait_for_sandbox_agent(
    handle_base_url: str,
    bearer_token: str,
    *,
    timeout_s: float,
    label: str,
) -> None:
    """Poll sandbox-agent /v1/health until it answers or timeout expires."""
    deadline = time.monotonic() + timeout_s
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with SandboxAgentClient(handle_base_url, bearer_token) as client:
                client.health()
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(1.0)
    raise RemoteBackendUnavailable(
        f"{label} did not become healthy within {timeout_s}s. "
        f"sandbox-agent may have failed to start. Last error: {last_exc}"
    )


def shell_quote(value: str) -> str:
    """Tiny single-quote shell escaper for generated scripts."""
    return "'" + value.replace("'", "'\"'\"'") + "'"
