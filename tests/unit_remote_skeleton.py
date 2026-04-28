"""Unit tests for the RemoteSandboxBackend skeleton.

Lifecycle parity with PoolBackend: allocate (auto-provision), discard
(tear down), reset_all, orphaned-lease reconciliation. Uses a `FakeProvider`
so no Modal/E2B/etc. SDK or network is required.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SRC = REPO_ROOT / "plugins" / "evo" / "src"
sys.path.insert(0, str(PLUGIN_SRC))

from evo.backends import (  # noqa: E402
    AllocateCtx,
    DiscardCtx,
    PoolExhausted,
    RemoteBackendUnavailable,
    RemoteSandboxBackend,
    SandboxHandle,
    SandboxProvider,
    SandboxSpec,
    load_backend,
)
from evo.backends.protocol import (  # noqa: E402
    PoolExhausted as _,
)
from evo.backends import remote_state  # noqa: E402
from evo.backends.sandbox_providers import known_providers, load_provider  # noqa: E402


class FakeProvider:
    """In-memory `SandboxProvider`. Records calls; provisions never actually
    spin up containers."""

    name = "fake"

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}
        self.provisions: list[SandboxSpec] = []
        self.tear_downs: list[SandboxHandle] = []
        self.alive: dict[str, bool] = {}
        self._next_id = 0

    def provision(self, spec: SandboxSpec) -> SandboxHandle:
        self.provisions.append(spec)
        native = f"fake-{self._next_id}"
        self._next_id += 1
        handle = SandboxHandle(
            provider=self.name,
            base_url=f"http://localhost:0/{native}",
            bearer_token=spec.bearer_token,
            native_id=native,
            metadata={},
        )
        self.alive[native] = True
        return handle

    def tear_down(self, handle: SandboxHandle) -> None:
        self.tear_downs.append(handle)
        self.alive[handle.native_id] = False

    def is_alive(self, handle: SandboxHandle) -> bool:
        return self.alive.get(handle.native_id, False)


def _init_remote_workspace(root: Path, provider_name: str = "fake") -> None:
    """Set up a minimal workspace so the backend has somewhere to read/write
    state from. Mirrors what `init_workspace` does for the remote case but
    without the rest of the CLI plumbing."""
    from evo.core import init_workspace, set_host

    # init_workspace requires a clean repo with at least one commit.
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "agent.py").write_text("STATE='baseline'\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=root, check=True)
    init_workspace(
        root,
        target="agent.py",
        benchmark="echo {}",
        metric="max",
        gate=None,
        host="generic",
        execution_backend="remote",
        commit_strategy="tracked-only",
        remote_provider=provider_name,
        remote_provider_config={"image_tag": "test"},
    )


def test_unknown_provider_raises_clear_error() -> None:
    """`load_provider('e2b', {})` must surface a typed error before any
    init logic runs. Different from a missing-SDK error."""
    try:
        load_provider("e2b-not-yet-shipped", {})
        raise AssertionError("expected RemoteBackendUnavailable")
    except RemoteBackendUnavailable as exc:
        assert "Unknown remote provider" in str(exc), str(exc)


def test_modal_loader_without_sdk_raises_actionable_error() -> None:
    """`load_provider('modal', {})` raises with install instructions when
    either the provider module is missing OR the modal SDK is missing.
    The user-facing error is what matters."""
    try:
        load_provider("modal", {})
    except RemoteBackendUnavailable as exc:
        msg = str(exc)
        assert "Modal" in msg
        assert "pip install modal" in msg, msg
    # If modal SDK happens to be installed, that's also a valid path; no fail.


def test_known_providers_lists_modal() -> None:
    assert "modal" in known_providers()


def test_remote_state_init_creates_file_with_zero_sandboxes(workdir: Path) -> None:
    _init_remote_workspace(workdir)
    state = remote_state.read_state(workdir)
    assert state["provider"] == "fake"
    assert state["provider_config"] == {"image_tag": "test"}
    assert state["sandboxes"] == []


def test_remote_state_locked_state_round_trip(workdir: Path) -> None:
    """Writes done under locked_state survive a fresh read."""
    _init_remote_workspace(workdir)
    with remote_state.locked_state(workdir) as state:
        state["sandboxes"].append({
            "id": 0, "native_id": "x", "base_url": "http://x", "leased_by": None,
            "last_branch": None, "provisioned_at": None,
        })
    again = remote_state.read_state(workdir)
    assert len(again["sandboxes"]) == 1
    assert again["sandboxes"][0]["native_id"] == "x"


def test_backend_allocate_provisions_on_first_use(workdir: Path) -> None:
    _init_remote_workspace(workdir)
    fake = FakeProvider()
    backend = RemoteSandboxBackend(fake)

    ctx = AllocateCtx(
        root=workdir,
        exp_id="exp_0000",
        parent_node=None,
        parent_commit="deadbeef",
        parent_ref="main",
        branch="evo/run_0000/exp_0000",
        hypothesis="test",
    )
    result = backend.allocate(ctx)

    assert len(fake.provisions) == 1, "provision should fire on first allocate"
    assert result.worktree == Path("/workspace/exp_0000")
    state = remote_state.read_state(workdir)
    assert len(state["sandboxes"]) == 1
    sandbox = state["sandboxes"][0]
    assert sandbox["native_id"] == "fake-0"
    assert sandbox["leased_by"]["exp_id"] == "exp_0000"
    # Bearer token NEVER persists to disk.
    assert "bearer_token" not in sandbox
    raw = json.loads((remote_state.remote_state_path(workdir)).read_text(encoding="utf-8"))
    flattened = json.dumps(raw)
    assert "bearer_token" not in flattened, "bearer_token leaked to disk"


def test_backend_discard_tears_down_sandbox(workdir: Path) -> None:
    _init_remote_workspace(workdir)
    fake = FakeProvider()
    backend = RemoteSandboxBackend(fake)

    ctx = AllocateCtx(
        root=workdir, exp_id="exp_0000", parent_node=None,
        parent_commit="dead", parent_ref="main",
        branch="evo/run_0000/exp_0000", hypothesis="t",
    )
    backend.allocate(ctx)
    discard_ctx = DiscardCtx(
        root=workdir,
        node={"id": "exp_0000", "status": "discarded"},
    )
    backend.discard(discard_ctx)

    assert len(fake.tear_downs) == 1
    state = remote_state.read_state(workdir)
    assert state["sandboxes"] == []


def test_backend_concurrency_one_blocks_second_allocate(workdir: Path) -> None:
    """POC concurrency=1: a second allocate while the first is leased
    must raise PoolExhausted, not silently provision a second sandbox."""
    _init_remote_workspace(workdir)
    fake = FakeProvider()
    backend = RemoteSandboxBackend(fake)

    ctx1 = AllocateCtx(
        root=workdir, exp_id="exp_0000", parent_node=None,
        parent_commit="d1", parent_ref="main",
        branch="evo/run_0000/exp_0000", hypothesis="t",
    )
    backend.allocate(ctx1)

    ctx2 = AllocateCtx(
        root=workdir, exp_id="exp_0001", parent_node=None,
        parent_commit="d2", parent_ref="main",
        branch="evo/run_0000/exp_0001", hypothesis="t",
    )
    try:
        backend.allocate(ctx2)
        raise AssertionError("expected PoolExhausted")
    except PoolExhausted as exc:
        assert "concurrency=1" in str(exc) or "free sandbox" in str(exc), str(exc)


def test_backend_release_lease_returns_slot_to_free_pool(workdir: Path) -> None:
    """release_lease in the POC tears down the sandbox; a fresh allocate
    must therefore re-provision."""
    _init_remote_workspace(workdir)
    fake = FakeProvider()
    backend = RemoteSandboxBackend(fake)

    ctx = AllocateCtx(
        root=workdir, exp_id="exp_0000", parent_node=None,
        parent_commit="d", parent_ref="main",
        branch="evo/run_0000/exp_0000", hypothesis="t",
    )
    backend.allocate(ctx)
    backend.release_lease(DiscardCtx(
        root=workdir, node={"id": "exp_0000", "status": "committed"},
    ))

    # Second allocate provisions a new sandbox -- POC tears down on release.
    ctx2 = AllocateCtx(
        root=workdir, exp_id="exp_0001", parent_node=None,
        parent_commit="d", parent_ref="main",
        branch="evo/run_0000/exp_0001", hypothesis="t",
    )
    backend.allocate(ctx2)
    assert len(fake.provisions) == 2, fake.provisions


def test_backend_reset_all_clears_state(workdir: Path) -> None:
    _init_remote_workspace(workdir)
    fake = FakeProvider()
    backend = RemoteSandboxBackend(fake)
    ctx = AllocateCtx(
        root=workdir, exp_id="exp_0000", parent_node=None,
        parent_commit="d", parent_ref="main",
        branch="evo/run_0000/exp_0000", hypothesis="t",
    )
    backend.allocate(ctx)

    backend.reset_all(workdir)
    # Workspace dir gone; no sandboxes left in memory.
    from evo.core import workspace_path
    assert not workspace_path(workdir).exists()


def test_load_backend_routes_remote(workdir: Path) -> None:
    """Top-level load_backend resolves execution_backend=remote and constructs
    via the registry. Validates the public seam."""
    # We can't call `load_backend` directly with an SDK-less modal; use a
    # workspace whose provider is missing to confirm the error type. The
    # successful path is covered by the FakeProvider tests above.
    _init_remote_workspace(workdir)
    # Manually overwrite config to use a known-missing provider.
    from evo.core import config_path, load_config
    config = load_config(workdir)
    config["execution_backend_config"] = {"provider": "e2b-not-real", "provider_config": {}}
    config_path_p = config_path(workdir)
    config_path_p.write_text(json.dumps(config), encoding="utf-8")
    try:
        load_backend(workdir)
        raise AssertionError("expected RemoteBackendUnavailable")
    except RemoteBackendUnavailable as exc:
        assert "Unknown remote provider" in str(exc), str(exc)


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="evo-remote-unit-"))
    try:
        # Tests with no fixture
        for fn in (
            test_unknown_provider_raises_clear_error,
            test_modal_loader_without_sdk_raises_actionable_error,
            test_known_providers_lists_modal,
        ):
            print(f"--- {fn.__name__} ---")
            fn()
            print("    OK")

        # Tests requiring a workspace fixture
        for fn in (
            test_remote_state_init_creates_file_with_zero_sandboxes,
            test_remote_state_locked_state_round_trip,
            test_backend_allocate_provisions_on_first_use,
            test_backend_discard_tears_down_sandbox,
            test_backend_concurrency_one_blocks_second_allocate,
            test_backend_release_lease_returns_slot_to_free_pool,
            test_backend_reset_all_clears_state,
            test_load_backend_routes_remote,
        ):
            sub = workdir / fn.__name__
            sub.mkdir()
            print(f"--- {fn.__name__} ---")
            fn(sub)
            print("    OK")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print("UNIT REMOTE SKELETON OK")


if __name__ == "__main__":
    main()
