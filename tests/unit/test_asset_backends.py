"""Tests for pluggable asset storage backends (#55 follow-up).

Pure URI parsing + scheme dispatch + LocalBackend run for real. The S3/HF
backends are exercised with injected fake clients so no network/credentials are
touched.
"""
from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path

from evo.asset_backends import (
    HFBackend,
    LocalBackend,
    S3Backend,
    backend_for_uri,
    parse_hf_uri,
    parse_s3_uri,
)


class TestUriParsing(unittest.TestCase):
    def test_parse_s3_uri(self):
        self.assertEqual(parse_s3_uri("s3://my-bucket/a/b/c.bin"), ("my-bucket", "a/b/c.bin"))

    def test_parse_s3_uri_rejects_missing_key(self):
        with self.assertRaises(ValueError):
            parse_s3_uri("s3://only-bucket")

    def test_parse_s3_uri_rejects_wrong_scheme(self):
        with self.assertRaises(ValueError):
            parse_s3_uri("gs://b/k")

    def test_parse_hf_uri(self):
        self.assertEqual(parse_hf_uri("hf://org/model/adapter.safetensors"),
                         ("org/model", "adapter.safetensors"))

    def test_parse_hf_uri_rejects_missing_path(self):
        with self.assertRaises(ValueError):
            parse_hf_uri("hf://org-model-only")


class TestDispatch(unittest.TestCase):
    def test_local_for_plain_path(self):
        self.assertIsInstance(backend_for_uri("/data/x.bin"), LocalBackend)

    def test_local_for_file_scheme(self):
        self.assertIsInstance(backend_for_uri("file:///data/x.bin"), LocalBackend)

    def test_s3_for_s3_scheme(self):
        self.assertIsInstance(backend_for_uri("s3://b/k", client=object()), S3Backend)

    def test_hf_for_hf_scheme(self):
        self.assertIsInstance(backend_for_uri("hf://org/m/f", client=object()), HFBackend)


class TestLocalBackend(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)
        self.src = self.d / "src.bin"
        self.src.write_text("payload")

    def tearDown(self):
        self._tmp.cleanup()

    def test_download_copies_into_dest(self):
        dest = self.d / "cache"
        dest.mkdir()
        out = LocalBackend().download(str(self.src), dest)
        self.assertEqual(Path(out).read_text(), "payload")
        self.assertEqual(Path(out).parent, dest)

    def test_exists(self):
        self.assertTrue(LocalBackend().exists(str(self.src)))
        self.assertFalse(LocalBackend().exists(str(self.d / "nope")))

    def test_upload_copies_to_target(self):
        target = self.d / "out" / "dest.bin"
        LocalBackend().upload(self.src, str(target))
        self.assertEqual(target.read_text(), "payload")


class _FakeS3Client:
    """Records boto3-style calls; simulates a bucket dict."""
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.uploaded = []

    def upload_file(self, Filename, Bucket, Key):
        self.objects[(Bucket, Key)] = Path(Filename).read_bytes()
        self.uploaded.append((Bucket, Key))

    def download_file(self, Bucket, Key, Filename):
        Path(Filename).write_bytes(self.objects[(Bucket, Key)])

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise KeyError((Bucket, Key))
        return {"ContentLength": len(self.objects[(Bucket, Key)])}


class TestS3Backend(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_upload_then_download_roundtrip(self):
        src = self.d / "a.bin"; src.write_text("weights")
        client = _FakeS3Client()
        be = S3Backend(client=client)
        be.upload(src, "s3://bucket/models/a.bin")
        self.assertIn(("bucket", "models/a.bin"), client.objects)
        dest = self.d / "cache"; dest.mkdir()
        out = be.download("s3://bucket/models/a.bin", dest)
        self.assertEqual(Path(out).read_text(), "weights")
        self.assertEqual(Path(out).parent, dest)

    def test_exists(self):
        client = _FakeS3Client({("bucket", "k.bin"): b"x"})
        be = S3Backend(client=client)
        self.assertTrue(be.exists("s3://bucket/k.bin"))
        self.assertFalse(be.exists("s3://bucket/missing"))

    def test_missing_boto3_raises_clear_error(self):
        # No client injected and boto3 unavailable -> actionable error.
        be = S3Backend(client=None, _import_client=lambda: (_ for _ in ()).throw(ImportError()))
        with self.assertRaises(RuntimeError) as ctx:
            be.exists("s3://bucket/k")
        self.assertIn("boto3", str(ctx.exception))


class _FakeHF:
    """Records huggingface_hub-style calls."""
    def __init__(self, files=None):
        self.files = dict(files or {})  # (repo_id, path) -> bytes
        self.uploaded = []

    def hf_hub_download(self, repo_id, filename, local_dir=None):
        data = self.files[(repo_id, filename)]
        out = Path(local_dir) / Path(filename).name
        out.write_bytes(data)
        return str(out)

    def upload_file(self, path_or_fileobj, path_in_repo, repo_id):
        self.files[(repo_id, path_in_repo)] = Path(path_or_fileobj).read_bytes()
        self.uploaded.append((repo_id, path_in_repo))

    def file_exists(self, repo_id, filename):
        return (repo_id, filename) in self.files


class TestHFBackend(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_upload_then_download_roundtrip(self):
        src = self.d / "a.bin"; src.write_text("adapter")
        client = _FakeHF()
        be = HFBackend(client=client)
        be.upload(src, "hf://org/model/a.bin")
        self.assertIn(("org/model", "a.bin"), client.files)
        dest = self.d / "cache"; dest.mkdir()
        out = be.download("hf://org/model/a.bin", dest)
        self.assertEqual(Path(out).read_text(), "adapter")

    def test_exists(self):
        client = _FakeHF({("org/model", "a.bin"): b"x"})
        be = HFBackend(client=client)
        self.assertTrue(be.exists("hf://org/model/a.bin"))
        self.assertFalse(be.exists("hf://org/model/missing"))

    def test_missing_dep_raises_clear_error(self):
        be = HFBackend(client=None, _import_client=lambda: (_ for _ in ()).throw(ImportError()))
        with self.assertRaises(RuntimeError) as ctx:
            be.exists("hf://org/model/a.bin")
        self.assertIn("huggingface_hub", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
