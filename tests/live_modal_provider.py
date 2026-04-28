"""Live test for ModalProvider against real Modal infrastructure.

Skipped unless `EVO_LIVE_TEST_MODAL=1` is set. Requires:
  - `modal` Python SDK installed (`pip install modal` or `uv pip install modal`)
  - Modal authenticated (`modal token new` to OAuth, or env vars
    MODAL_TOKEN_ID + MODAL_TOKEN_SECRET)

Cost: provisions one Modal sandbox for ~30-60 seconds. Single-digit
Modal credits per run.

Run from repo root:
    EVO_LIVE_TEST_MODAL=1 uv run --project plugins/evo \
        python tests/live_modal_provider.py
"""
from __future__ import annotations

import os
import secrets
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SRC = REPO_ROOT / "plugins" / "evo" / "src"
sys.path.insert(0, str(PLUGIN_SRC))


def _gate() -> None:
    if os.environ.get("EVO_LIVE_TEST_MODAL") != "1":
        print("SKIPPED (set EVO_LIVE_TEST_MODAL=1 to enable)")
        sys.exit(0)
    try:
        import modal  # noqa: F401
    except ImportError:
        print("SKIPPED (modal SDK not installed; pip install modal)")
        sys.exit(0)


def test_provision_health_teardown() -> None:
    """End-to-end: provision a Modal sandbox, hit /v1/health, tear down."""
    from evo.backends.sandbox_providers import load_provider
    from evo.backends.protocol import SandboxSpec
    from evo.sandbox_client import SandboxAgentClient

    provider = load_provider("modal", {
        # Use a dedicated app name so this test doesn't collide with any
        # production-flavored evo workspace. Cleanup happens in tear_down.
        "app_name": "evo-live-test",
        "timeout_seconds": 300,            # bound the test sandbox lifetime
        "health_timeout_seconds": 90.0,    # generous; image cold-build can take a minute
    })

    bearer = secrets.token_urlsafe(32)
    spec = SandboxSpec(
        image_ref="evo-sandbox-base",
        env={},
        bearer_token=bearer,
        exposed_port=8080,
        timeout_seconds=300,
    )

    print("--- provisioning Modal sandbox (image build may take ~60s on first run) ---")
    t0 = time.monotonic()
    handle = provider.provision(spec)
    t1 = time.monotonic()
    print(f"    provisioned in {t1 - t0:.1f}s -> {handle.base_url}")
    assert handle.provider == "modal"
    assert handle.base_url.startswith("https://")
    assert handle.bearer_token == bearer
    assert handle.native_id

    try:
        print("--- hitting /v1/health from orchestrator ---")
        with SandboxAgentClient(handle.base_url, handle.bearer_token) as client:
            health = client.health()
            print(f"    health response: {health}")

            print("--- exec test: `echo from-modal` ---")
            result = client.process_run("echo", args=["from-modal"])
            assert result.exit_code == 0, result.stderr
            assert "from-modal" in result.stdout, result.stdout
            print(f"    exec OK: {result.stdout.strip()!r}")

            print("--- fs test: write + read /tmp/hello.txt ---")
            client.fs_write("/tmp/hello.txt", b"hello from live test\n")
            content = client.fs_read("/tmp/hello.txt")
            assert content == b"hello from live test\n", content
            print(f"    fs round-trip OK")

            print("--- is_alive check ---")
            assert provider.is_alive(handle) is True
            print(f"    sandbox is alive")
    finally:
        print("--- tearing down Modal sandbox ---")
        provider.tear_down(handle)
        # Modal's terminate is async; give it a moment, then verify.
        time.sleep(2.0)
        # is_alive may transiently return True due to Modal's eventual
        # consistency; don't assert False.
        print(f"    teardown call sent")


def main() -> None:
    _gate()
    print("=== Live Modal provider test ===")
    print(f"=== Auth: {'token env vars set' if os.environ.get('MODAL_TOKEN_ID') else 'using ~/.modal.toml'} ===")
    test_provision_health_teardown()
    print("LIVE MODAL OK")


if __name__ == "__main__":
    main()
