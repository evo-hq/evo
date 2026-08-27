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


if __name__ == "__main__":
    unittest.main()
