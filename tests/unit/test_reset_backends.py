"""`evo reset` must reset every distinct backend spec in the run -- the
workspace default plus per-experiment overrides -- so remote sandboxes
created via `evo new --remote ...` are torn down even when the workspace
default is a local backend."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from evo.backends import backend_state_key, remote_state
from evo.core import (
    atomic_write_json,
    graph_path,
    init_workspace,
    load_graph,
    reset_runtime_state,
    workspace_path,
)


FAKE_PROVIDER_MODULE = '''\
from pathlib import Path


class FakeProvider:
    name = "fake"

    def __init__(self, config):
        self.config = dict(config)

    def provision(self, spec):
        raise RuntimeError("not used in this test")

    def tear_down(self, handle):
        marker_dir = Path(self.config["marker_dir"])
        if (marker_dir / "fail-teardown").exists():
            raise RuntimeError("teardown boom")
        (marker_dir / f"torn-{handle.native_id}").touch()

    def is_alive(self, handle):
        return False

    def build_client(self, handle):
        raise RuntimeError("not used in this test")
'''


class TestResetTearsDownOverrideBackends(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "t@t"], cwd=self.root, check=True
        )
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.root, check=True)
        (self.root / "t.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=self.root, check=True)
        init_workspace(
            self.root,
            target="t.py",
            benchmark="python bench.py",
            metric="max",
            gate=None,
        )
        self.marker_dir = self.root / "markers"
        self.marker_dir.mkdir()
        self.modules_dir = self.root / "fake_modules"
        self.modules_dir.mkdir()
        (self.modules_dir / "evo_test_fake_provider.py").write_text(
            FAKE_PROVIDER_MODULE, encoding="utf-8"
        )
        sys.path.insert(0, str(self.modules_dir))

    def tearDown(self):
        sys.path.remove(str(self.modules_dir))
        sys.modules.pop("evo_test_fake_provider", None)
        self._tmp.cleanup()

    def _seed_remote_override_node(self) -> str:
        provider_ref = "evo_test_fake_provider:FakeProvider"
        provider_config = {"marker_dir": str(self.marker_dir)}
        backend_config = {
            "provider": provider_ref,
            "provider_config": provider_config,
        }

        graph = load_graph(self.root)
        graph["nodes"]["exp_0000"] = {
            "id": "exp_0000",
            "status": "pending",
            "backend": "remote",
            "backend_config": backend_config,
        }
        atomic_write_json(graph_path(self.root), graph)

        state_key = backend_state_key("remote", backend_config)
        remote_state.init_state(
            self.root,
            provider=provider_ref,
            provider_config=provider_config,
            state_key=state_key,
        )
        with remote_state.locked_state(self.root, state_key) as state:
            state["sandboxes"].append(
                {
                    "id": 0,
                    "native_id": "sb-test",
                    "base_url": "http://127.0.0.1:9",
                    "bearer_token": "",
                    "metadata": {},
                    "leased_by": None,
                }
            )
        return state_key

    def test_reset_tears_down_per_node_remote_sandboxes(self):
        self._seed_remote_override_node()

        reset_runtime_state(self.root)

        self.assertTrue(
            (self.marker_dir / "torn-sb-test").exists(),
            "per-node remote sandbox was not torn down by reset",
        )
        self.assertFalse(workspace_path(self.root).exists())

    def test_failed_teardown_preserves_state_and_raises_then_retry_succeeds(self):
        state_key = self._seed_remote_override_node()
        (self.marker_dir / "fail-teardown").touch()

        with self.assertRaises(RuntimeError) as ctx:
            reset_runtime_state(self.root)
        self.assertIn("sb-test", str(ctx.exception))

        # The failure must not erase the record of the (possibly still
        # billing) sandbox: run dir and its state entry survive for retry.
        self.assertTrue(workspace_path(self.root).exists())
        state = remote_state.read_state(self.root, state_key)
        self.assertEqual(
            [s["native_id"] for s in state["sandboxes"]], ["sb-test"]
        )

        (self.marker_dir / "fail-teardown").unlink()
        reset_runtime_state(self.root)
        self.assertTrue((self.marker_dir / "torn-sb-test").exists())
        self.assertFalse(workspace_path(self.root).exists())

    def test_reset_survives_unloadable_override_backend(self):
        graph = load_graph(self.root)
        graph["nodes"]["exp_0000"] = {
            "id": "exp_0000",
            "status": "pending",
            "backend": "remote",
            "backend_config": {"provider": "definitely-not-a-provider"},
        }
        atomic_write_json(graph_path(self.root), graph)

        with self.assertRaises(RuntimeError) as ctx:
            reset_runtime_state(self.root)
        self.assertIn("definitely-not-a-provider", str(ctx.exception))
        # An unloadable provider can't tear down its sandboxes; the run
        # dir must survive so a later reset can finish the cleanup.
        self.assertTrue(workspace_path(self.root).exists())


if __name__ == "__main__":
    unittest.main()
