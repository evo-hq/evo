"""Git-bundle round-trip helpers for the remote-sandbox backend.

A `git bundle` is a single-file pack of git objects + refs. Used as the
commit-transport between the orchestrator's git database and the in-sandbox
clone, avoiding the need for a shared git remote.

Two directions:
  - Outbound (orchestrator → sandbox): ship a parent commit into a fresh
    container during `evo new` so the experiment has somewhere to branch
    from. `ship_commit_to_sandbox`.
  - Inbound (sandbox → orchestrator): pull a new experiment commit from
    the sandbox into the orchestrator's git database after `evo run`
    commits. `fetch_commit_from_sandbox`.

Symmetric in shape (build a bundle, transfer the bytes, unbundle on the
other side); the difference is who creates the bundle.
"""
from __future__ import annotations

import io
import subprocess
import tarfile
from pathlib import Path

from .sandbox_client import SandboxAgentClient


# Conventional in-sandbox paths. Kept here rather than scattered through
# the backend so the layout is one decision in one place.
SANDBOX_REPO_ROOT = "/workspace/repo"
SANDBOX_BUNDLE_DIR = "/tmp/evo-bundles"


def ship_commit_to_sandbox(
    client: SandboxAgentClient,
    *,
    local_repo: Path,
    commit: str,
    sandbox_repo: str | None = None,
    bundle_dir: str | None = None,
    bundle_filename: str = "parent.bundle",
) -> str:
    """Move a single commit (and all reachable objects) from the local
    repo into the sandbox's clone, then check it out as detached HEAD.

    `sandbox_repo` and `bundle_dir` default to the module-level
    SANDBOX_REPO_ROOT / SANDBOX_BUNDLE_DIR but are read at CALL time
    (not definition time) so tests and callers can override the layout
    without monkey-patching gotchas.

    Returns the in-sandbox path of the bundle file.
    """
    if sandbox_repo is None:
        sandbox_repo = SANDBOX_REPO_ROOT
    if bundle_dir is None:
        bundle_dir = SANDBOX_BUNDLE_DIR

    # 1. Build the bundle locally.
    bundle_blob = _create_bundle(local_repo, commit)

    # 2. Wrap it in a tar (sandbox-agent's upload-batch extracts a tar).
    tar_bytes = _tar_single_file(bundle_filename, bundle_blob)

    # 3. Upload + extract.
    client.fs_mkdir(bundle_dir, recursive=True)
    client.fs_upload_batch(bundle_dir, tar_bytes)
    sandbox_bundle_path = f"{bundle_dir}/{bundle_filename}"

    # 4. Unbundle inside the sandbox.
    result = client.process_run(
        "git",
        args=["bundle", "unbundle", sandbox_bundle_path],
        cwd=sandbox_repo,
    )
    if result.exit_code != 0:
        raise RuntimeError(
            f"git bundle unbundle failed in sandbox "
            f"(exit={result.exit_code}): {result.stderr[:500]}"
        )
    return sandbox_bundle_path


def fetch_commit_from_sandbox(
    client: SandboxAgentClient,
    *,
    local_repo: Path,
    base_commit: str,
    head_commit: str,
    sandbox_repo: str | None = None,
    bundle_dir: str | None = None,
    bundle_filename: str | None = None,
) -> None:
    """Pull `base_commit..head_commit` from the sandbox into the local
    git database. Incremental bundle (only the new objects).

    Doesn't create any branch ref locally; callers should `git update-ref`
    if they want to pin the commit beyond the next `git gc`.
    """
    if sandbox_repo is None:
        sandbox_repo = SANDBOX_REPO_ROOT
    if bundle_dir is None:
        bundle_dir = SANDBOX_BUNDLE_DIR
    if bundle_filename is None:
        bundle_filename = f"exp-{head_commit[:12]}.bundle"
    sandbox_bundle_path = f"{bundle_dir}/{bundle_filename}"

    # 1. Stamp a temporary ref on the head commit. `git bundle create` needs
    # a named ref tip, not a bare commit (same constraint as the outbound
    # path). `range..ref` works; `range..commit` doesn't.
    tip_ref = f"refs/evo-bundle/exp-{head_commit[:12]}"
    update_ref = client.process_run(
        "git", args=["update-ref", tip_ref, head_commit], cwd=sandbox_repo,
    )
    if update_ref.exit_code != 0:
        raise RuntimeError(
            f"git update-ref failed in sandbox (exit={update_ref.exit_code}): "
            f"{update_ref.stderr[:500]}"
        )

    # 2. Build the incremental bundle inside the sandbox.
    client.fs_mkdir(bundle_dir, recursive=True)
    try:
        result = client.process_run(
            "git",
            args=["bundle", "create", sandbox_bundle_path,
                  f"{base_commit}..{tip_ref}"],
            cwd=sandbox_repo,
        )
        if result.exit_code != 0:
            raise RuntimeError(
                f"git bundle create failed in sandbox "
                f"(exit={result.exit_code}): {result.stderr[:500]}"
            )

        # 3. Download the bundle bytes.
        bundle_blob = client.fs_read(sandbox_bundle_path)
    finally:
        # Best-effort cleanup of the temp ref so the sandbox's ref namespace
        # stays tidy across many experiments leasing the same slot.
        client.process_run(
            "git", args=["update-ref", "-d", tip_ref], cwd=sandbox_repo,
        )

    # 4. Unbundle locally.
    _apply_bundle(local_repo, bundle_blob)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _create_bundle(repo: Path, commit: str) -> bytes:
    """Run `git bundle create -` against the local repo and capture stdout.

    `git bundle create FILE <commit-hash>` rejects "empty bundles" because
    bundles require a named ref tip rather than a bare commit. We create
    a short-lived ref, bundle from it, and clean up afterwards. The ref
    name encodes the commit so concurrent ships of different commits
    don't collide.
    """
    ref = f"refs/evo-bundle/{commit}"
    subprocess.run(
        ["git", "update-ref", ref, commit],
        cwd=repo, check=True, capture_output=True,
    )
    try:
        proc = subprocess.run(
            ["git", "bundle", "create", "-", ref],
            cwd=repo, capture_output=True, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"git bundle create failed locally (exit={proc.returncode}): "
                f"{proc.stderr.decode('utf-8', errors='replace')[:500]}"
            )
        return proc.stdout
    finally:
        subprocess.run(
            ["git", "update-ref", "-d", ref],
            cwd=repo, check=False, capture_output=True,
        )


def _apply_bundle(repo: Path, bundle_bytes: bytes) -> None:
    """Run `git bundle unbundle -` against the local repo, piping the bundle
    bytes via stdin. Adds objects + ref tips to the local object DB."""
    proc = subprocess.run(
        ["git", "bundle", "unbundle", "/dev/stdin"],
        cwd=repo,
        input=bundle_bytes,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git bundle unbundle failed locally (exit={proc.returncode}): "
            f"{proc.stderr.decode('utf-8', errors='replace')[:500]}"
        )


def _tar_single_file(name: str, content: bytes) -> bytes:
    """Build an in-memory tar archive containing one file."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(content)
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()
