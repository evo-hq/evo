"""Remote sandbox records survive failed teardown attempts."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from evo.backends import remote_state
from evo.backends.protocol import DiscardCtx, SandboxHandle
from evo.backends.remote import RemoteSandboxBackend


class _TeardownProvider:
    name = "teardown-test"

    def __init__(self) -> None:
        self.fail = True
        self.calls: list[str] = []

    def provision(self, spec: Any) -> SandboxHandle:
        raise NotImplementedError

    def tear_down(self, handle: SandboxHandle) -> None:
        self.calls.append(handle.native_id)
        if self.fail:
            raise RuntimeError(f"close failed for {handle.native_id}")

    def is_alive(self, handle: SandboxHandle) -> bool:
        return True

    def build_client(self, handle: SandboxHandle) -> Any:
        raise NotImplementedError


def _backend_with_record(
    root: Path,
    provider: _TeardownProvider,
    *,
    exp_id: str | None,
) -> RemoteSandboxBackend:
    backend = RemoteSandboxBackend(provider)
    remote_state.init_state(
        root,
        provider=provider.name,
        provider_config={},
        state_key=backend.state_key,
    )
    with remote_state.locked_state(root, backend.state_key) as state:
        state["sandboxes"].append({
            "id": 0,
            "native_id": "sandbox-1",
            "base_url": "https://sandbox-1.example.test",
            "bearer_token": "test-token",
            "leased_by": (
                {"exp_id": exp_id, "pid": 1, "leased_at": "now"}
                if exp_id is not None
                else None
            ),
            "last_branch": None,
            "provisioned_at": "now",
        })
    return backend


def _native_ids(root: Path, backend: RemoteSandboxBackend) -> list[str]:
    state = remote_state.read_state(root, backend.state_key)
    return [sandbox["native_id"] for sandbox in state["sandboxes"]]


@pytest.mark.parametrize("method_name", ["discard", "release_lease"])
def test_direct_cleanup_retains_record_and_surfaces_failure(
    tmp_path: Path,
    method_name: str,
) -> None:
    provider = _TeardownProvider()
    backend = _backend_with_record(tmp_path, provider, exp_id="exp_0001")
    ctx = DiscardCtx(root=tmp_path, node={"id": "exp_0001"})

    with pytest.raises(RuntimeError, match="close failed for sandbox-1"):
        getattr(backend, method_name)(ctx)
    assert _native_ids(tmp_path, backend) == ["sandbox-1"]

    provider.fail = False
    getattr(backend, method_name)(ctx)
    assert _native_ids(tmp_path, backend) == []


def test_gc_retains_record_until_teardown_succeeds(tmp_path: Path) -> None:
    provider = _TeardownProvider()
    backend = _backend_with_record(tmp_path, provider, exp_id=None)
    ctx = DiscardCtx(root=tmp_path, node={"id": "exp_0001"})

    assert backend.gc(ctx) is False
    assert _native_ids(tmp_path, backend) == ["sandbox-1"]

    provider.fail = False
    assert backend.gc(ctx) is True
    assert _native_ids(tmp_path, backend) == []


def test_orphan_sweep_retains_record_until_teardown_succeeds(
    tmp_path: Path,
) -> None:
    provider = _TeardownProvider()
    backend = _backend_with_record(tmp_path, provider, exp_id=None)

    assert backend.sweep_orphans(tmp_path, set()) == []
    assert _native_ids(tmp_path, backend) == ["sandbox-1"]

    provider.fail = False
    assert backend.sweep_orphans(tmp_path, set()) == ["sandbox-1"]
    assert _native_ids(tmp_path, backend) == []
