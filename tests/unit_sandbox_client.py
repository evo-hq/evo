"""Unit tests for SandboxAgentClient and git_bundle helpers.

Spins up an in-process Flask app that fakes sandbox-agent's surface,
points the real client at it, and exercises the round-trip. No Modal,
no real container, no network. The fake's filesystem is just a dict.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SRC = REPO_ROOT / "plugins" / "evo" / "src"
sys.path.insert(0, str(PLUGIN_SRC))

from flask import Flask, Response, jsonify, request  # noqa: E402

from evo.sandbox_client import (  # noqa: E402
    SandboxAgentClient,
    SandboxAgentError,
)
from evo import git_bundle  # noqa: E402


# ---------------------------------------------------------------------------
# Fake sandbox-agent server
# ---------------------------------------------------------------------------


class FakeSandbox:
    """In-memory fake of sandbox-agent's filesystem + process surface."""

    def __init__(self, repo: Path) -> None:
        # Real on-disk repo for the in-sandbox `git` operations -- the fake
        # delegates `process_run` for commands matching `git ...` to a real
        # subprocess in a managed cwd. Everything else is rejected.
        self.repo = repo
        # In-memory filesystem keyed by absolute path -> bytes.
        # Files written via fs_write live here AND get mirrored on disk
        # under self.fsroot so git can see them when needed.
        self.fsroot = repo.parent / "_fake_fs"
        self.fsroot.mkdir(exist_ok=True)
        self.bearer_token = "test-token-abc"

    def _check_auth(self) -> bool:
        header = request.headers.get("Authorization", "")
        return header == f"Bearer {self.bearer_token}"

    def _resolve(self, path: str) -> Path:
        # All paths are sandbox-absolute. Map onto self.fsroot for storage,
        # except for /workspace/repo which IS the test repo.
        if path.startswith("/workspace/repo"):
            return self.repo / path[len("/workspace/repo"):].lstrip("/")
        return self.fsroot / path.lstrip("/")


def _build_fake_app(sandbox: FakeSandbox) -> Flask:
    app = Flask("fake-sandbox-agent")

    def auth_or_401() -> Response | None:
        if not sandbox._check_auth():
            return Response("unauthorized", status=401)
        return None

    @app.get("/v1/health")
    def health():
        if (resp := auth_or_401()):
            return resp
        return jsonify({"status": "ok"})

    @app.post("/v1/processes/run")
    def processes_run():
        if (resp := auth_or_401()):
            return resp
        body = request.get_json(force=True)
        cmd = body.get("command")
        raw_args = body.get("args", []) or []
        cwd = body.get("cwd")
        env = body.get("env", {}) or {}
        full_env = dict(os.environ)
        full_env.update(env)
        # Resolve cwd via the same path mapping. If unset, default to repo.
        if cwd:
            cwd_path = sandbox._resolve(cwd)
        else:
            cwd_path = sandbox.repo
        cwd_path.mkdir(parents=True, exist_ok=True)

        # Translate sandbox-absolute path arguments to host-absolute paths
        # under fsroot, so the subprocess can find files written via
        # fs_write/upload-batch. Only translates args that look like
        # absolute sandbox paths.
        def _translate_arg(arg: str) -> str:
            if isinstance(arg, str) and arg.startswith(("/workspace/", "/tmp/")):
                return str(sandbox._resolve(arg))
            return arg
        translated_args = [_translate_arg(a) for a in raw_args]

        proc = subprocess.run(
            [cmd, *translated_args],
            cwd=cwd_path,
            env=full_env,
            capture_output=True,
            text=True,
        )
        return jsonify({
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "exitCode": proc.returncode,
            "timedOut": False,
            "durationMs": 1,
            "stdoutTruncated": False,
            "stderrTruncated": False,
        })

    @app.get("/v1/fs/file")
    def fs_read():
        if (resp := auth_or_401()):
            return resp
        path = request.args.get("path")
        target = sandbox._resolve(path)
        if not target.exists():
            return Response("not found", status=404)
        return Response(target.read_bytes(), mimetype="application/octet-stream")

    @app.put("/v1/fs/file")
    def fs_write():
        if (resp := auth_or_401()):
            return resp
        path = request.args.get("path")
        target = sandbox._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(request.get_data())
        return jsonify({"path": path, "bytes": len(request.get_data())})

    @app.get("/v1/fs/entries")
    def fs_entries():
        if (resp := auth_or_401()):
            return resp
        path = request.args.get("path")
        target = sandbox._resolve(path)
        if not target.exists() or not target.is_dir():
            return jsonify({"entries": []})
        entries = []
        for child in sorted(target.iterdir()):
            entries.append({
                "name": child.name,
                "path": str(child.relative_to(sandbox.fsroot.parent.parent) if False else child),
                "isDir": child.is_dir(),
                "size": child.stat().st_size if child.is_file() else None,
            })
        return jsonify({"entries": entries})

    @app.get("/v1/fs/stat")
    def fs_stat():
        if (resp := auth_or_401()):
            return resp
        path = request.args.get("path")
        target = sandbox._resolve(path)
        if not target.exists():
            return Response("not found", status=404)
        return jsonify({
            "path": path,
            "isDir": target.is_dir(),
            "size": target.stat().st_size if target.is_file() else 0,
        })

    @app.post("/v1/fs/mkdir")
    def fs_mkdir():
        if (resp := auth_or_401()):
            return resp
        body = request.get_json(force=True)
        target = sandbox._resolve(body["path"])
        target.mkdir(parents=True, exist_ok=bool(body.get("recursive", False)))
        return jsonify({"path": body["path"]})

    @app.delete("/v1/fs/entry")
    def fs_delete():
        if (resp := auth_or_401()):
            return resp
        path = request.args.get("path")
        recursive = request.args.get("recursive", "false") == "true"
        target = sandbox._resolve(path)
        if target.is_dir():
            if recursive:
                shutil.rmtree(target)
            else:
                target.rmdir()
        elif target.exists():
            target.unlink()
        return jsonify({"path": path})

    @app.post("/v1/fs/upload-batch")
    def fs_upload_batch():
        if (resp := auth_or_401()):
            return resp
        dest = request.args.get("path")
        dest_path = sandbox._resolve(dest)
        dest_path.mkdir(parents=True, exist_ok=True)
        # Body is a tar archive.
        with tarfile.open(fileobj=io.BytesIO(request.get_data()), mode="r") as tar:
            tar.extractall(dest_path)
        return jsonify({"path": dest})

    return app


def _start_fake_server(sandbox: FakeSandbox) -> tuple[str, threading.Thread]:
    """Run Flask on a free port in a daemon thread; return (base_url, thread)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    app = _build_fake_app(sandbox)
    thread = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False),
        daemon=True,
    )
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    # Poll until ready.
    client = SandboxAgentClient(base_url, bearer_token=sandbox.bearer_token)
    client.wait_for_health(timeout_seconds=5.0, poll_interval=0.1)
    client.close()
    return base_url, thread


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _build_repo(workdir: Path) -> Path:
    repo = workdir / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "agent.py").write_text("STATE='baseline'\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, check=True)
    return repo


def test_health_and_auth(workdir: Path) -> None:
    repo = _build_repo(workdir)
    sandbox = FakeSandbox(repo)
    base_url, _t = _start_fake_server(sandbox)

    with SandboxAgentClient(base_url, bearer_token=sandbox.bearer_token) as client:
        assert client.health() == {"status": "ok"}

    # Wrong token rejects.
    with SandboxAgentClient(base_url, bearer_token="wrong") as client:
        try:
            client.health()
            raise AssertionError("expected SandboxAgentError")
        except SandboxAgentError as exc:
            assert exc.status == 401, exc.status


def test_fs_read_write_round_trip(workdir: Path) -> None:
    repo = _build_repo(workdir)
    sandbox = FakeSandbox(repo)
    base_url, _t = _start_fake_server(sandbox)

    with SandboxAgentClient(base_url, bearer_token=sandbox.bearer_token) as client:
        client.fs_write("/workspace/scratch/hello.txt", b"world\n")
        contents = client.fs_read("/workspace/scratch/hello.txt")
        assert contents == b"world\n"

        entries = client.fs_entries("/workspace/scratch")
        names = [e.name for e in entries]
        assert "hello.txt" in names

        client.fs_delete("/workspace/scratch/hello.txt")
        try:
            client.fs_read("/workspace/scratch/hello.txt")
            raise AssertionError("expected 404")
        except SandboxAgentError as exc:
            assert exc.status == 404


def test_process_run_executes_command(workdir: Path) -> None:
    repo = _build_repo(workdir)
    sandbox = FakeSandbox(repo)
    base_url, _t = _start_fake_server(sandbox)

    with SandboxAgentClient(base_url, bearer_token=sandbox.bearer_token) as client:
        result = client.process_run("echo", args=["hello"])
        assert result.exit_code == 0, result.stderr
        assert "hello" in result.stdout


def test_git_bundle_round_trip(workdir: Path) -> None:
    """Local repo → sandbox via ship_commit_to_sandbox; sandbox makes a new
    commit; sandbox → local via fetch_commit_from_sandbox; local repo now
    has the new commit reachable as an object."""
    repo = _build_repo(workdir)
    sandbox = FakeSandbox(repo)
    # The sandbox repo is a SECOND git repo that gets the bundle. Set it up
    # as a bare-ish empty git repo at /workspace/repo (which the fake maps
    # to sandbox.repo). But sandbox.repo is the same as repo above...
    # we need a separate sandbox-side clone.
    sandbox_clone = workdir / "sandbox_clone"
    sandbox_clone.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=sandbox_clone, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=sandbox_clone, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=sandbox_clone, check=True)
    sandbox.repo = sandbox_clone

    base_url, _t = _start_fake_server(sandbox)

    parent_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()

    with SandboxAgentClient(base_url, bearer_token=sandbox.bearer_token) as client:
        # Outbound: ship parent commit into the sandbox clone.
        git_bundle.ship_commit_to_sandbox(
            client, local_repo=repo, commit=parent_commit,
        )
        # Sandbox clone now has the commit reachable.
        in_sandbox = subprocess.run(
            ["git", "cat-file", "-e", parent_commit],
            cwd=sandbox_clone, capture_output=True,
        )
        assert in_sandbox.returncode == 0, "parent commit missing in sandbox clone"

        # Sandbox makes a new commit on top.
        subprocess.run(
            ["git", "checkout", "-q", parent_commit],
            cwd=sandbox_clone, check=True,
        )
        (sandbox_clone / "new_file.txt").write_text("from sandbox\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=sandbox_clone, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "sandbox-side commit"],
            cwd=sandbox_clone, check=True,
        )
        new_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=sandbox_clone, capture_output=True, text=True, check=True,
        ).stdout.strip()

        # Inbound: fetch the new commit back into the local repo.
        git_bundle.fetch_commit_from_sandbox(
            client, local_repo=repo,
            base_commit=parent_commit, head_commit=new_commit,
        )

    # Local repo now has the new commit's objects.
    local_check = subprocess.run(
        ["git", "cat-file", "-e", new_commit],
        cwd=repo, capture_output=True,
    )
    assert local_check.returncode == 0, "new commit not landed locally"


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="evo-sbx-client-"))
    try:
        for fn in (
            test_health_and_auth,
            test_fs_read_write_round_trip,
            test_process_run_executes_command,
            test_git_bundle_round_trip,
        ):
            sub = workdir / fn.__name__
            sub.mkdir()
            print(f"--- {fn.__name__} ---")
            fn(sub)
            print("    OK")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print("UNIT SANDBOX CLIENT OK")


if __name__ == "__main__":
    main()
