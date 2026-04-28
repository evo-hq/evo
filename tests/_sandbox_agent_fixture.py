"""Test fixture: download + spawn a real sandbox-agent on localhost.

Used by integration tests so they exercise the actual sandbox-agent
binary instead of a Flask fake. No mocks anywhere in the stack.

Caching: the binary is downloaded once into `~/.cache/evo-tests/sandbox-agent`
and reused across runs. The download URL matches what
plugins/evo/src/evo/backends/sandbox_providers/modal.py uses (the
same install path the Modal image build follows).
"""
from __future__ import annotations

import os
import platform
import secrets
import socket
import stat
import subprocess
import sys
import time
import urllib.request

import requests
from pathlib import Path
from typing import Iterator
from contextlib import contextmanager


# Match the version pinned in the Modal provider so localhost tests
# exercise the same binary version that ships in production.
SANDBOX_AGENT_VERSION = "0.4.x"
RELEASES_BASE = f"https://releases.rivet.dev/sandbox-agent/{SANDBOX_AGENT_VERSION}/binaries"

CACHE_DIR = Path(os.environ.get("EVO_TEST_CACHE", str(Path.home() / ".cache" / "evo-tests")))
BINARY_PATH = CACHE_DIR / "sandbox-agent"


def _platform_triple() -> str:
    """Map Python's platform info to rivet's release-asset naming."""
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Darwin":
        if machine in ("arm64", "aarch64"):
            return "sandbox-agent-aarch64-apple-darwin"
        if machine in ("x86_64", "amd64"):
            return "sandbox-agent-x86_64-apple-darwin"
    elif system == "Linux":
        # Rivet ships only the x86_64 musl Linux binary; ARM64 Linux
        # users will need to wait for upstream support.
        if machine in ("x86_64", "amd64"):
            return "sandbox-agent-x86_64-unknown-linux-musl"
    raise RuntimeError(
        f"sandbox-agent has no published binary for {system}/{machine}; "
        f"see https://github.com/rivet-dev/sandbox-agent for source build"
    )


def ensure_sandbox_agent_binary() -> Path:
    """Download (once) + cache the platform-appropriate sandbox-agent
    binary. Returns the local path to it."""
    if BINARY_PATH.exists() and BINARY_PATH.stat().st_size > 0:
        return BINARY_PATH

    triple = _platform_triple()
    url = f"{RELEASES_BASE}/{triple}"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[fixture] downloading sandbox-agent from {url} ...", file=sys.stderr)
    # `requests` follows redirects + sets a normal UA; releases.rivet.dev
    # 403s urllib's default UA but accepts requests' default.
    resp = requests.get(url, stream=True, timeout=120.0)
    resp.raise_for_status()
    BINARY_PATH.write_bytes(resp.content)
    BINARY_PATH.chmod(BINARY_PATH.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    print(f"[fixture] cached at {BINARY_PATH} ({BINARY_PATH.stat().st_size} bytes)",
          file=sys.stderr)
    return BINARY_PATH


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@contextmanager
def localhost_sandbox_agent(token: str | None = None) -> Iterator[tuple[str, str]]:
    """Spawn a real sandbox-agent on a free localhost port for the duration
    of the with-block. Yields `(base_url, bearer_token)`.

    The agent runs in a clean working dir and binds to 127.0.0.1 only.
    On exit, the process is sent SIGTERM, then SIGKILL after 2s if needed.
    """
    binary = ensure_sandbox_agent_binary()
    if token is None:
        token = secrets.token_urlsafe(16)
    port = _free_port()

    argv = [
        str(binary), "server",
        "--token", token,
        "--host", "127.0.0.1",
        "--port", str(port),
    ]
    print(f"[fixture] launching: {argv}", file=sys.stderr)
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    base_url = f"http://127.0.0.1:{port}"

    # Poll /v1/health until ready or timeout.
    REPO_ROOT = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))
    from evo.sandbox_client import SandboxAgentClient  # noqa: E402

    try:
        client = SandboxAgentClient(base_url, bearer_token=token)
        client.wait_for_health(timeout_seconds=5.0, poll_interval=0.1)
        client.close()
    except Exception:
        # If startup failed, dump stderr so the test diag is useful.
        proc.terminate()
        try:
            stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
        except Exception:
            stderr = ""
        raise RuntimeError(
            f"sandbox-agent failed to become healthy on {base_url}; "
            f"stderr: {stderr[:1000]}"
        )

    try:
        yield base_url, token
    except BaseException:
        # On test failure, surface the daemon's stderr so the diagnostic
        # is in the same output stream as the assertion that fired.
        proc.terminate()
        try:
            stderr_bytes = proc.stderr.read() if proc.stderr else b""
            stderr = stderr_bytes.decode("utf-8", errors="replace")
        except Exception:
            stderr = ""
        if stderr.strip():
            print(f"\n[sandbox-agent stderr]\n{stderr[-4000:]}\n", file=sys.stderr)
        raise
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
