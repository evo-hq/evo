"""`evo asset` CLI round-trip against a temp workspace (#55)."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from unittest import mock

from evo.assets import assets_path, load_registry
from evo.cli import (
    cmd_asset_get,
    cmd_asset_list,
    cmd_asset_put,
    cmd_asset_rm,
    cmd_asset_use,
)
from evo.core import init_workspace


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@evo"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
    (root / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)


def _put_args(path, name, kind, exp=None, tag=None, copy=False, backend=None):
    return argparse.Namespace(path=str(path), name=name, kind=kind, exp=exp,
                              tag=tag or [], copy=copy, backend=backend)


class _FakeRemoteBackend:
    """In-memory stand-in for S3/HF: upload stores bytes by uri, download
    writes them into dest_dir under the uri's basename."""
    _store: dict = {}

    def upload(self, local, uri):
        type(self)._store[uri] = Path(local).read_bytes()

    def download(self, uri, dest_dir):
        Path(dest_dir).mkdir(parents=True, exist_ok=True)
        dest = Path(dest_dir) / uri.rstrip("/").split("/")[-1]
        dest.write_bytes(type(self)._store[uri])
        return dest

    def exists(self, uri):
        return uri in type(self)._store


class _FakeNestedBackend(_FakeRemoteBackend):
    """Like huggingface_hub(local_dir=...): keeps the repo subpath under dest_dir
    (the flat fake above hides that), and counts downloads."""
    downloads = 0

    def download(self, uri, dest_dir):
        type(self).downloads += 1
        rel = uri.split("://", 1)[1].split("/", 2)[2]  # drop owner/name
        dest = Path(dest_dir) / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(type(self)._store[uri])
        return dest


class TestAssetCli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _init_git_repo(self.root)
        init_workspace(self.root, target="t.py", benchmark="python bench.py",
                       metric="max", gate=None)
        # A file to register as an asset.
        self.asset_file = self.root / "adapter.bin"
        self.asset_file.write_text("weights")
        self._old_cwd = Path.cwd()
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def _capture(self, fn, args) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn(args)
        return buf.getvalue().strip()

    def test_put_registers_asset(self):
        cmd_asset_put(_put_args(self.asset_file, "adapter", "checkpoint",
                                exp="exp_0001", tag=["epoch=2"]))
        reg = load_registry(self.root)
        entry = reg["assets"]["adapter"]
        self.assertEqual(entry["kind"], "checkpoint")
        self.assertEqual(entry["produced_by"], "exp_0001")
        self.assertEqual(entry["tags"], {"epoch": "2"})
        self.assertEqual(Path(entry["path"]).read_text(), "weights")

    def test_put_missing_path_errors(self):
        with self.assertRaises(RuntimeError):
            cmd_asset_put(_put_args(self.root / "nope.bin", "x", "model"))

    def test_put_duplicate_name_errors(self):
        cmd_asset_put(_put_args(self.asset_file, "adapter", "checkpoint"))
        with self.assertRaises(RuntimeError):
            cmd_asset_put(_put_args(self.asset_file, "adapter", "model"))

    def test_get_prints_path(self):
        cmd_asset_put(_put_args(self.asset_file, "adapter", "checkpoint"))
        out = self._capture(cmd_asset_get, argparse.Namespace(name="adapter"))
        self.assertEqual(out, str(self.asset_file))

    def test_get_unknown_raises(self):
        with self.assertRaises(RuntimeError):
            cmd_asset_get(argparse.Namespace(name="ghost"))

    def test_put_copy_materializes_under_assets_dir(self):
        cmd_asset_put(_put_args(self.asset_file, "adapter", "checkpoint", copy=True))
        entry = load_registry(self.root)["assets"]["adapter"]
        self.assertTrue(entry["copied"])
        self.assertIn("assets", Path(entry["path"]).parts)
        self.assertEqual(Path(entry["path"]).read_text(), "weights")

    def test_list_filters_by_tag(self):
        cmd_asset_put(_put_args(self.asset_file, "a", "dataset", tag=["held-out=true"]))
        cmd_asset_put(_put_args(self.asset_file, "b", "dataset"))
        out = self._capture(cmd_asset_list, argparse.Namespace(
            kind=None, tag=["held-out=true"], produced_by=None, consumed_by=None, json=True))
        names = [e["name"] for e in json.loads(out)]
        self.assertEqual(names, ["a"])

    def test_use_records_consumption(self):
        cmd_asset_put(_put_args(self.asset_file, "adapter", "checkpoint"))
        cmd_asset_use(argparse.Namespace(name="adapter", exp="exp_0002"))
        self.assertEqual(
            load_registry(self.root)["assets"]["adapter"]["consumed_by"], ["exp_0002"])

    def test_rm_refuses_when_consumed(self):
        cmd_asset_put(_put_args(self.asset_file, "adapter", "checkpoint"))
        cmd_asset_use(argparse.Namespace(name="adapter", exp="exp_0002"))
        with self.assertRaises(RuntimeError):
            cmd_asset_rm(argparse.Namespace(name="adapter", force=False))
        cmd_asset_rm(argparse.Namespace(name="adapter", force=True))
        self.assertNotIn("adapter", load_registry(self.root)["assets"])

    def test_put_normalizes_whitespace_name(self):
        # A padded handle must register under the trimmed name and be reachable
        # both by the trimmed name and by the padded string the user typed.
        cmd_asset_put(_put_args(self.asset_file, "  spaced  ", "checkpoint"))
        entry = load_registry(self.root)["assets"]["spaced"]
        self.assertEqual(entry["name"], "spaced")
        self.assertEqual(
            self._capture(cmd_asset_get, argparse.Namespace(name="spaced")),
            str(self.asset_file))
        self.assertEqual(
            self._capture(cmd_asset_get, argparse.Namespace(name="  spaced  ")),
            str(self.asset_file))

    def test_put_whitespace_name_duplicate_detected(self):
        cmd_asset_put(_put_args(self.asset_file, "spaced", "checkpoint"))
        with self.assertRaises(RuntimeError):
            cmd_asset_put(_put_args(self.asset_file, "  spaced  ", "model"))

    def test_put_blank_name_rejected(self):
        with self.assertRaises(RuntimeError):
            cmd_asset_put(_put_args(self.asset_file, "   ", "model"))

    def test_registry_file_location(self):
        cmd_asset_put(_put_args(self.asset_file, "adapter", "checkpoint"))
        self.assertTrue(assets_path(self.root).exists())

    # --- storage backends (#55 follow-up) -------------------------------

    def test_put_backend_uploads_and_records_uri(self):
        _FakeRemoteBackend._store = {}
        with mock.patch("evo.asset_backends.backend_for_uri",
                        return_value=_FakeRemoteBackend()):
            cmd_asset_put(_put_args(self.asset_file, "remote-adapter", "checkpoint",
                                    backend="s3://bucket/models/remote-adapter.bin"))
        entry = load_registry(self.root)["assets"]["remote-adapter"]
        self.assertEqual(entry["backend"], "s3")
        self.assertEqual(entry["uri"], "s3://bucket/models/remote-adapter.bin")
        self.assertIn("s3://bucket/models/remote-adapter.bin", _FakeRemoteBackend._store)

    def test_get_remote_downloads_to_cache(self):
        _FakeRemoteBackend._store = {}
        with mock.patch("evo.asset_backends.backend_for_uri",
                        return_value=_FakeRemoteBackend()):
            cmd_asset_put(_put_args(self.asset_file, "remote-adapter", "checkpoint",
                                    backend="s3://bucket/models/remote-adapter.bin"))
            out = self._capture(cmd_asset_get, argparse.Namespace(name="remote-adapter"))
        # get returns a LOCAL cache path whose contents match the uploaded file.
        self.assertTrue(Path(out).exists())
        self.assertEqual(Path(out).read_text(), "weights")
        self.assertIn("_cache", Path(out).parts)

    def test_local_put_records_local_backend(self):
        cmd_asset_put(_put_args(self.asset_file, "adapter", "checkpoint"))
        entry = load_registry(self.root)["assets"]["adapter"]
        self.assertEqual(entry.get("backend", "local"), "local")

    def test_put_rejects_bad_input_before_uploading(self):
        # Every rule that can reject a put must fire BEFORE the upload: a rejected
        # put may not write to (or overwrite) remote storage, or escape the workspace.
        _FakeRemoteBackend._store = {}
        cmd_asset_put(_put_args(self.asset_file, "taken", "model"))
        cmd_asset_put(_put_args(self.asset_file, "a-b", "model"))
        somedir = self.root / "somedir"
        somedir.mkdir()
        plain = self.root / "store" / "adapter.bin"
        remote = "s3://bucket/x.bin"
        cases = {
            "blank kind": _put_args(self.asset_file, "x", "  ", backend=remote),
            "directory": _put_args(somedir, "dirasset", "dataset", backend=remote),
            "plain-path backend": _put_args(self.asset_file, "p", "model", backend=str(plain)),
            "taken name": _put_args(self.asset_file, "taken", "model", backend=remote),
            "env-var collision": _put_args(self.asset_file, "a_b", "model", backend=remote),
            **{f"name {bad!r}": _put_args(self.asset_file, bad, "model", copy=True)
               for bad in ("../evil", "a/b", "a\\b", "..", ".")},
        }
        with mock.patch("evo.asset_backends.backend_for_uri",
                        return_value=_FakeRemoteBackend()):
            for label, args in cases.items():
                with self.subTest(label), self.assertRaises((ValueError, RuntimeError)):
                    cmd_asset_put(args)
        self.assertEqual(_FakeRemoteBackend._store, {})
        self.assertFalse(plain.exists())
        self.assertEqual(set(load_registry(self.root)["assets"]), {"taken", "a-b"})

    def test_concurrent_put_of_same_name_is_refused_without_uploading(self):
        # While one put is mid-upload, a second put of the same name must be
        # refused before it uploads anything (the name is reserved for the whole
        # put), not race the first and lose after the fact.
        outer = self
        uploads = []

        class _Reentrant(_FakeRemoteBackend):
            def upload(inner, local, uri):
                uploads.append(uri)
                if len(uploads) == 1:
                    with mock.patch("evo.assets.NAME_LOCK_TIMEOUT_SECONDS", 0.3):
                        with outer.assertRaises(RuntimeError) as ctx:
                            cmd_asset_put(_put_args(
                                outer.asset_file, "dup", "checkpoint",
                                backend="s3://bucket/second.bin"))
                    outer.assertIn("in progress", str(ctx.exception))
                super().upload(local, uri)

        _FakeRemoteBackend._store = {}
        with mock.patch("evo.asset_backends.backend_for_uri",
                        return_value=_Reentrant()):
            cmd_asset_put(_put_args(self.asset_file, "dup", "checkpoint",
                                    backend="s3://bucket/first.bin"))
        self.assertEqual(uploads, ["s3://bucket/first.bin"])  # second never uploaded
        self.assertEqual(
            load_registry(self.root)["assets"]["dup"]["uri"], "s3://bucket/first.bin")

    def test_put_backend_uploads_outside_registry_lock(self):
        # advisory_lock gives up after 10s; a big upload under it would fail every
        # concurrent `evo asset` call. Upload must not hold the registry lock.
        from evo.core import lock_file_for
        from evo.locking import advisory_lock

        root = self.root
        held = []

        class _ProbingBackend(_FakeRemoteBackend):
            def upload(self, local, uri):
                try:
                    with advisory_lock(lock_file_for(assets_path(root)),
                                       timeout_seconds=0.3):
                        held.append(False)
                except Exception:
                    held.append(True)
                super().upload(local, uri)

        _FakeRemoteBackend._store = {}
        with mock.patch("evo.asset_backends.backend_for_uri",
                        return_value=_ProbingBackend()):
            cmd_asset_put(_put_args(self.asset_file, "big", "checkpoint",
                                    backend="s3://bucket/big.bin"))
        self.assertEqual(held, [False])
        self.assertIn("big", load_registry(self.root)["assets"])

    def test_put_copy_materializes_outside_registry_lock(self):
        # --copy can copy a multi-GB file or dir; like an upload, it must not hold
        # the registry lock (which gives up after 10s) while it runs.
        from evo import assets as assets_mod
        from evo.core import lock_file_for
        from evo.locking import advisory_lock

        real = assets_mod.materialize
        root = self.root
        held = []

        def probing(r, name, source):
            try:
                with advisory_lock(lock_file_for(assets_path(root)), timeout_seconds=0.3):
                    held.append(False)
            except Exception:
                held.append(True)
            return real(r, name, source)

        with mock.patch("evo.assets.materialize", side_effect=probing):
            cmd_asset_put(_put_args(self.asset_file, "bigcopy", "checkpoint", copy=True))
        self.assertEqual(held, [False])
        entry = load_registry(self.root)["assets"]["bigcopy"]
        self.assertTrue(entry["copied"])
        self.assertEqual(Path(entry["path"]).read_text(), "weights")

    def test_put_copy_env_var_collision_leaves_no_orphan_copy(self):
        # The rules that can reject a put run BEFORE the copy, so a rejected put
        # never leaves a copied file behind.
        from evo.assets import assets_dir
        cmd_asset_put(_put_args(self.asset_file, "a-b", "model"))
        with self.assertRaises((ValueError, RuntimeError)):
            cmd_asset_put(_put_args(self.asset_file, "a_b", "model", copy=True))
        self.assertFalse((assets_dir(self.root) / "a_b").exists())

    def test_put_copy_is_removed_if_registration_fails(self):
        # If the put fails after the copy (here: the registry can't be saved),
        # the copy this put made must not be left behind -- but only that copy.
        from evo.assets import assets_dir
        keep = assets_dir(self.root) / "flaky" / "keep.txt"
        keep.parent.mkdir(parents=True)
        keep.write_text("leftover from an earlier asset")
        with mock.patch("evo.assets.save_registry", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                cmd_asset_put(_put_args(self.asset_file, "flaky", "checkpoint", copy=True))
        self.assertFalse((assets_dir(self.root) / "flaky" / self.asset_file.name).exists())
        self.assertTrue(keep.exists())  # neighbouring files are untouched
        self.assertNotIn("flaky", load_registry(self.root)["assets"])

    # --- remote assets resolve to the local cache ------------------------------

    def _put_remote(self, name="remote-adapter", uri="s3://bucket/models/a.bin"):
        _FakeRemoteBackend._store = {}
        with mock.patch("evo.asset_backends.backend_for_uri",
                        return_value=_FakeRemoteBackend()):
            cmd_asset_put(_put_args(self.asset_file, name, "checkpoint", backend=uri))
        return uri

    def _run_env(self, exp_id="exp_0002"):
        from evo.cli import _runtime_env_for_attempt
        from evo.core import load_config
        return _runtime_env_for_attempt(
            self.root, load_config(self.root), exp_id=exp_id, attempt_label="001",
            worktree=self.root, env_traces_dir="t", env_result_path="r.json",
            env_checkpoint_dir="ck")

    def test_list_shows_uri_for_remote_asset(self):
        uri = self._put_remote()
        listed = self._capture(cmd_asset_list, argparse.Namespace(
            kind=None, tag=[], produced_by=None, consumed_by=None, json=False))
        self.assertIn(uri, listed)
        self.assertNotIn("None", listed)

    def test_get_nested_remote_path_reuses_cache(self):
        # Backends that keep the remote subpath (HF) must still hit the cache on
        # the second get instead of re-downloading every time.
        _FakeNestedBackend._store = {}
        _FakeNestedBackend.downloads = 0
        with mock.patch("evo.asset_backends.backend_for_uri",
                        return_value=_FakeNestedBackend()):
            cmd_asset_put(_put_args(self.asset_file, "nested", "checkpoint",
                                    backend="hf://org/model/ckpt/epoch2/a.bin"))
            first = self._capture(cmd_asset_get, argparse.Namespace(name="nested"))
            second = self._capture(cmd_asset_get, argparse.Namespace(name="nested"))
        self.assertEqual(first, second)
        self.assertEqual(Path(second).read_text(), "weights")
        self.assertEqual(_FakeNestedBackend.downloads, 1)

    def test_rm_then_reput_never_serves_stale_cache(self):
        # A re-registered handle must fetch fresh bytes, whether it moved to a
        # different uri with the same filename or new bytes went to the SAME uri.
        _FakeRemoteBackend._store = {}
        steps = [("s3://run-a/model.bin", "v1"), ("s3://run-b/model.bin", "v2"),
                 ("s3://run-b/model.bin", "v3")]
        with mock.patch("evo.asset_backends.backend_for_uri",
                        return_value=_FakeRemoteBackend()):
            for n, (uri, content) in enumerate(steps):
                src = self.root / f"src{n}.bin"
                src.write_text(content)
                cmd_asset_put(_put_args(src, "ckpt", "checkpoint", backend=uri))
                got = self._capture(cmd_asset_get, argparse.Namespace(name="ckpt"))
                self.assertEqual(Path(got).read_text(), content)
                cmd_asset_rm(argparse.Namespace(name="ckpt", force=False))
                self.assertFalse(Path(got).exists())  # rm drops the downloaded copy

    def test_rm_keeps_entry_when_cache_cannot_be_cleared(self):
        # If the cached copy can't be deleted (e.g. Windows file lock), rm must
        # fail with the entry still registered; dropping it would leave stale
        # bytes that a later put at the same uri would serve.
        self._put_remote(name="held")
        with mock.patch("evo.asset_backends.backend_for_uri",
                        return_value=_FakeRemoteBackend()):
            self._capture(cmd_asset_get, argparse.Namespace(name="held"))
        with mock.patch("evo.assets.shutil.rmtree", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                cmd_asset_rm(argparse.Namespace(name="held", force=False))
        self.assertIn("held", load_registry(self.root)["assets"])

    def test_use_remote_downloads_and_reports_local_path(self):
        self._put_remote()
        with mock.patch("evo.asset_backends.backend_for_uri",
                        return_value=_FakeRemoteBackend()):
            out = self._capture(cmd_asset_use, argparse.Namespace(
                name="remote-adapter", exp="exp_0002"))
        local = out.split("EVO_ASSET_REMOTE_ADAPTER=", 1)[1]
        self.assertIn("_cache", Path(local).parts)
        self.assertEqual(Path(local).read_text(), "weights")

    def test_use_remote_fetch_failure_records_nothing(self):
        self._put_remote()

        class _Down(_FakeRemoteBackend):
            def download(self, uri, dest_dir):
                raise OSError("network down")

        with mock.patch("evo.asset_backends.backend_for_uri", return_value=_Down()):
            with self.assertRaises(OSError):
                cmd_asset_use(argparse.Namespace(name="remote-adapter", exp="exp_0002"))
        self.assertEqual(
            load_registry(self.root)["assets"]["remote-adapter"]["consumed_by"], [])

    def _use_racing(self, during_fetch):
        """Run `use` for exp_0002 while `during_fetch(name)` mutates the registry
        after the fetch but before the consumption is recorded."""
        import evo.cli as cli
        real = cli._resolve_asset_local_path

        def racing(root, name, entry):
            path = real(root, name, entry)
            during_fetch(name)
            return path

        with mock.patch("evo.asset_backends.backend_for_uri",
                        return_value=_FakeRemoteBackend()), \
             mock.patch("evo.cli._resolve_asset_local_path", side_effect=racing):
            cmd_asset_use(argparse.Namespace(name="remote-adapter", exp="exp_0002"))

    def test_use_detects_asset_replaced_mid_fetch_but_not_concurrent_use(self):
        from evo.assets import load_registry as _load, registry_record_use, save_registry
        other = self.root / "other.bin"
        other.write_text("other")

        def replace(name):  # rm + re-put at another uri: the fetched path is the OLD asset's
            cmd_asset_rm(argparse.Namespace(name=name, force=False))
            with mock.patch("evo.asset_backends.backend_for_uri",
                            return_value=_FakeRemoteBackend()):
                cmd_asset_put(_put_args(other, name, "checkpoint",
                                        backend="s3://other/a.bin"))

        def other_exp_uses(name):  # consumed_by changes; the asset's identity does not
            reg = _load(self.root)
            registry_record_use(reg, name, "exp_0009")
            save_registry(self.root, reg)

        def consumers():
            return load_registry(self.root)["assets"]["remote-adapter"]["consumed_by"]

        self._put_remote()
        with self.subTest("replaced mid-fetch"):
            with self.assertRaises(RuntimeError):
                self._use_racing(replace)
            self.assertEqual(consumers(), [])
        with self.subTest("concurrent use by another experiment"):
            self._use_racing(other_exp_uses)
            self.assertEqual(consumers(), ["exp_0009", "exp_0002"])

    def test_run_env_points_remote_asset_at_cached_path(self):
        self._put_remote()
        with mock.patch("evo.asset_backends.backend_for_uri",
                        return_value=_FakeRemoteBackend()):
            cmd_asset_use(argparse.Namespace(name="remote-adapter", exp="exp_0002"))
            value = self._run_env()["EVO_ASSET_REMOTE_ADAPTER"]
        self.assertIn("_cache", Path(value).parts)
        self.assertEqual(Path(value).read_text(), "weights")

    def test_run_env_falls_back_to_uri_when_fetch_fails(self):
        # A run must never be blocked by a fetch failure; the recipe can still
        # `evo asset get` the uri and see the real error.
        from evo.assets import clear_asset_cache
        uri = self._put_remote()
        with mock.patch("evo.asset_backends.backend_for_uri",
                        return_value=_FakeRemoteBackend()):
            cmd_asset_use(argparse.Namespace(name="remote-adapter", exp="exp_0002"))
        clear_asset_cache(self.root, "remote-adapter")

        class _Down(_FakeRemoteBackend):
            def download(self, uri, dest_dir):
                raise OSError("network down")

        with mock.patch("evo.asset_backends.backend_for_uri", return_value=_Down()):
            env = self._run_env()
        self.assertEqual(env["EVO_ASSET_REMOTE_ADAPTER"], uri)


if __name__ == "__main__":
    unittest.main()
