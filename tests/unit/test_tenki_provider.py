"""Tenki provider config validation: resource limits are checked at
construction time against Tenki's create API limits, so bad values fail at
config-save / `evo new` instead of minutes later inside provisioning."""
from __future__ import annotations

import importlib.util
import unittest


@unittest.skipUnless(
    importlib.util.find_spec("tenki"),
    "tenki SDK not installed",
)
class TestTenkiResourceValidation(unittest.TestCase):
    def test_invalid_resources_are_rejected(self):
        from evo.backends.protocol import RemoteBackendUnavailable
        from evo.backends.sandbox_providers.tenki import TenkiProvider

        for bad in (
            {"cpu_cores": -1},
            {"cpu_cores": 0},
            {"cpu_cores": 17},
            {"memory_mb": -4096},
            {"memory_mb": 129},
            {"disk_size_gb": "-5"},
            {"disk_size_gb": 101},
            {"idle_timeout_minutes": 0},
            {"cpu_cores": [4]},
            {"memory_mb": "lots"},
        ):
            with self.assertRaises(RemoteBackendUnavailable, msg=bad):
                TenkiProvider(bad)

    def test_valid_and_empty_resources_are_accepted(self):
        from evo.backends.sandbox_providers.tenki import TenkiProvider

        provider = TenkiProvider(
            {"cpu_cores": "4", "memory_mb": 8192, "disk_size_gb": 50}
        )
        self.assertEqual(provider.cpu_cores, 4)
        self.assertEqual(provider.memory_mb, 8192)
        self.assertEqual(provider.disk_size_gb, 50)
        self.assertIsNone(TenkiProvider({}).cpu_cores)

    def test_provision_passes_workspace_id(self):
        from unittest.mock import patch

        from evo.backends.protocol import SandboxSpec
        from evo.backends.sandbox_providers import tenki as tenki_provider

        captured = {}

        class FakeResult:
            exit_code = 0
            stderr_text = ""
            stdout_text = ""

        class FakeSandbox:
            id = "sb-test"

            def exec(self, *argv, **kwargs):
                return FakeResult()

            def expose_port(self, port, **kwargs):
                return type("Exposed", (), {"url": "https://sb-test.example"})()

        def fake_create(**kwargs):
            captured.update(kwargs)
            return FakeSandbox()

        provider = tenki_provider.TenkiProvider(
            {"auth_token": "tk_test", "workspace_id": "ws-test"}
        )
        spec = SandboxSpec(
            image_ref="",
            env={},
            bearer_token="bearer",
        )
        with (
            patch.object(tenki_provider.Sandbox, "create", side_effect=fake_create),
            patch.object(tenki_provider, "wait_for_sandbox_agent"),
        ):
            handle = provider.provision(spec)

        self.assertEqual(captured["workspace_id"], "ws-test")
        self.assertEqual(handle.native_id, "sb-test")

    def test_fs_upload_batch_streams_chunks_over_data_plane(self):
        from evo.backends.sandbox_providers.tenki import (
            TenkiProvider,
            UPLOAD_CHUNK_BYTES,
        )

        recorded = {"execs": [], "stream_path": None, "chunks": None}

        class FakeFS:
            def write_stream(self, path, chunks, **kwargs):
                recorded["stream_path"] = path
                recorded["chunks"] = [bytes(c) for c in chunks]

        class FakeSandbox:
            fs = FakeFS()

            def exec(self, *argv, **kwargs):
                recorded["execs"].append(argv)

        class FakeClient:
            def get(self, native_id):
                assert native_id == "sb-test"
                return FakeSandbox()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

        provider = TenkiProvider({"auth_token": "tk_test"})
        provider._client = lambda: FakeClient()
        client = provider.build_client(
            type(
                "H",
                (),
                {
                    "base_url": "http://127.0.0.1:9",
                    "bearer_token": "b",
                    "native_id": "sb-test",
                },
            )()
        )

        payload = bytes(range(256)) * 10240
        self.assertGreater(len(payload), 2 * UPLOAD_CHUNK_BYTES)
        client.fs_upload_batch("/home/tenki/evo/x/bundles", payload)

        self.assertEqual(
            recorded["stream_path"], "/home/tenki/evo/x/bundles/.evo-upload.tar"
        )
        self.assertEqual(b"".join(recorded["chunks"]), payload)
        self.assertTrue(
            all(len(c) <= UPLOAD_CHUNK_BYTES for c in recorded["chunks"])
        )
        self.assertGreater(len(recorded["chunks"]), 1)
        self.assertEqual(recorded["execs"][0][:2], ("mkdir", "-p"))
        self.assertEqual(recorded["execs"][1][:2], ("tar", "-xf"))
        self.assertEqual(recorded["execs"][2][:2], ("rm", "-f"))

        clone = client.clone()
        self.assertEqual(clone._native_id, "sb-test")
        self.assertIsInstance(clone, type(client))

    def test_tear_down_raises_when_close_retries_are_exhausted(self):
        from evo.backends.protocol import RemoteBackendUnavailable, SandboxHandle
        from evo.backends.sandbox_providers.tenki import TenkiProvider

        provider = TenkiProvider({"auth_token": "tk_test"})
        provider._close_sandbox = lambda sandbox_id, sandbox=None: False
        handle = SandboxHandle(
            provider="tenki",
            base_url="http://127.0.0.1:9",
            bearer_token="",
            native_id="sb-test",
            metadata={},
        )
        with self.assertRaises(RemoteBackendUnavailable) as ctx:
            provider.tear_down(handle)
        self.assertIn("sb-test", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
