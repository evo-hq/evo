"""Pluggable storage backends for the asset registry (#55 follow-up).

An asset URI's scheme selects a backend that can upload a local file to remote
storage, download it back to a local path, and check existence. Remote backends
(S3, HF Hub) wrap their SDKs behind lazy, optional imports and accept an
**injected client**, so their logic is unit-testable with a fake client -- no
network, no credentials. Real S3/HF round-trips are a manual/CI concern.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable


# --- pure URI parsing ------------------------------------------------------

def parse_s3_uri(uri: str) -> tuple[str, str]:
    """`s3://bucket/key/path` -> ('bucket', 'key/path')."""
    if not uri.startswith("s3://"):
        raise ValueError(f"not an s3:// uri: {uri!r}")
    rest = uri[len("s3://"):]
    bucket, sep, key = rest.partition("/")
    if not bucket or not sep or not key:
        raise ValueError(f"s3 uri must be s3://bucket/key (got {uri!r})")
    return bucket, key


def parse_hf_uri(uri: str) -> tuple[str, str]:
    """`hf://org/model/path/to/file` -> ('org/model', 'path/to/file').

    The repo id is the first two segments (owner/name); the remainder is the
    file path within the repo.
    """
    if not uri.startswith("hf://"):
        raise ValueError(f"not an hf:// uri: {uri!r}")
    parts = [p for p in uri[len("hf://"):].split("/") if p != ""]
    if len(parts) < 3:
        raise ValueError(f"hf uri must be hf://owner/name/path (got {uri!r})")
    return f"{parts[0]}/{parts[1]}", "/".join(parts[2:])


def _strip_file_scheme(uri: str) -> str:
    return uri[len("file://"):] if uri.startswith("file://") else uri


# --- backends --------------------------------------------------------------

class LocalBackend:
    scheme = "local"

    def upload(self, local: Path, uri: str) -> None:
        target = Path(_strip_file_scheme(uri))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local, target)

    def download(self, uri: str, dest_dir: Path) -> Path:
        source = Path(_strip_file_scheme(uri))
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / source.name
        shutil.copy2(source, dest)
        return dest

    def exists(self, uri: str) -> bool:
        return Path(_strip_file_scheme(uri)).exists()


class S3Backend:
    scheme = "s3"

    def __init__(self, client: Any | None = None,
                 _import_client: Callable[[], Any] | None = None):
        self.client = client
        self._import_client = _import_client or (
            lambda: __import__("boto3").client("s3")
        )

    def _resolved_client(self) -> Any:
        if self.client is not None:
            return self.client
        try:
            self.client = self._import_client()
        except ImportError as exc:
            raise RuntimeError(
                "boto3 is required for s3:// assets; pip install boto3"
            ) from exc
        return self.client

    def upload(self, local: Path, uri: str) -> None:
        bucket, key = parse_s3_uri(uri)
        self._resolved_client().upload_file(
            Filename=str(local), Bucket=bucket, Key=key)

    def download(self, uri: str, dest_dir: Path) -> Path:
        bucket, key = parse_s3_uri(uri)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / Path(key).name
        self._resolved_client().download_file(
            Bucket=bucket, Key=key, Filename=str(dest))
        return dest

    def exists(self, uri: str) -> bool:
        bucket, key = parse_s3_uri(uri)
        client = self._resolved_client()
        try:
            client.head_object(Bucket=bucket, Key=key)
            return True
        except Exception:
            return False


class HFBackend:
    scheme = "hf"

    def __init__(self, client: Any | None = None,
                 _import_client: Callable[[], Any] | None = None):
        self.client = client
        self._import_client = _import_client or _default_hf_client

    def _resolved_client(self) -> Any:
        if self.client is not None:
            return self.client
        try:
            self.client = self._import_client()
        except ImportError as exc:
            raise RuntimeError(
                "huggingface_hub is required for hf:// assets; "
                "pip install huggingface_hub"
            ) from exc
        return self.client

    def upload(self, local: Path, uri: str) -> None:
        repo_id, path = parse_hf_uri(uri)
        self._resolved_client().upload_file(
            path_or_fileobj=str(local), path_in_repo=path, repo_id=repo_id)

    def download(self, uri: str, dest_dir: Path) -> Path:
        repo_id, path = parse_hf_uri(uri)
        dest_dir.mkdir(parents=True, exist_ok=True)
        out = self._resolved_client().hf_hub_download(
            repo_id=repo_id, filename=path, local_dir=str(dest_dir))
        return Path(out)

    def exists(self, uri: str) -> bool:
        repo_id, path = parse_hf_uri(uri)
        return bool(self._resolved_client().file_exists(
            repo_id=repo_id, filename=path))


def _default_hf_client() -> Any:
    """Adapter exposing hf_hub_download / upload_file / file_exists backed by
    the real huggingface_hub library (only used outside tests)."""
    import huggingface_hub as hf  # noqa: F401 -- raises ImportError if absent
    from huggingface_hub import HfApi
    from types import SimpleNamespace

    api = HfApi()
    return SimpleNamespace(
        hf_hub_download=hf.hf_hub_download,
        upload_file=api.upload_file,
        file_exists=api.file_exists,
    )


def backend_for_uri(uri: str, *, client: Any | None = None):
    """Select a backend by URI scheme. `client` is injected for remote backends
    (tests pass a fake; production leaves it None to lazily construct the SDK)."""
    if uri.startswith("s3://"):
        return S3Backend(client=client)
    if uri.startswith("hf://"):
        return HFBackend(client=client)
    return LocalBackend()
