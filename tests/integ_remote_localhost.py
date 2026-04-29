"""Integration tests for the remote-sandbox backend, against a real
sandbox-agent binary running on localhost.

No mocks. No Flask fakes. The fixture downloads the real
rivet-dev/sandbox-agent release (cached after first run) and spawns it
on a free port; tests exercise the actual HTTP surface, the actual
git-bundle round-trip, and the actual evo CLI.

These tests replace the earlier `unit_remote_skeleton.py` (FakeProvider)
and `unit_sandbox_client.py` (Flask fake of the daemon) suites.

Skip conditions: none on macOS x86_64/arm64 or Linux x86_64. Other
platforms can't download a binary and will surface a clear RuntimeError
from the fixture.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SRC = REPO_ROOT / "plugins" / "evo" / "src"
sys.path.insert(0, str(PLUGIN_SRC))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from _sandbox_agent_fixture import localhost_sandbox_agent  # noqa: E402

from evo.backends import (  # noqa: E402
    AllocateCtx,
    DiscardCtx,
    PoolExhausted,
    RemoteBackendUnavailable,
    RemoteSandboxBackend,
    load_backend,
)
from evo.backends.sandbox_providers import known_providers, load_provider  # noqa: E402
from evo.backends.sandbox_providers.manual import ManualProvider  # noqa: E402
from evo.backends import remote_state  # noqa: E402
from evo.sandbox_client import SandboxAgentClient, SandboxAgentError  # noqa: E402
from evo import git_bundle  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


PLUGIN_ROOT = REPO_ROOT / "plugins" / "evo"


def _evo(args: list[str], cwd: Path, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    return subprocess.run(
        ["uv", "run", "--project", str(PLUGIN_ROOT), "evo", *args],
        cwd=cwd, check=check, capture_output=True, text=True, env=full_env,
    )


def _build_repo(workdir: Path) -> Path:
    repo = workdir / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "agent.py").write_text("STATE='baseline'\n", encoding="utf-8")
    # Tiny benchmark that emits a JSON envelope to $EVO_RESULT_PATH.
    (repo / "eval.py").write_text(
        "import os, json, sys\n"
        "from pathlib import Path\n"
        "result_path = os.environ['EVO_RESULT_PATH']\n"
        "Path(result_path).parent.mkdir(parents=True, exist_ok=True)\n"
        "Path(result_path).write_text(json.dumps({'score': 1.0, 'tasks': {}}))\n"
        "print(json.dumps({'score': 1.0}))\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, check=True)
    return repo


def _shutdown_dashboard(root: Path) -> None:
    pid_file = root / ".evo" / "dashboard.pid"
    if not pid_file.exists():
        return
    try:
        os.kill(int(pid_file.read_text().strip()), 15)
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_known_providers_lists_modal_and_manual() -> None:
    providers = known_providers()
    assert "modal" in providers, providers
    assert "manual" in providers, providers


def test_unknown_provider_raises_clear_error() -> None:
    try:
        load_provider("e2b-not-yet-shipped", {})
        raise AssertionError("expected RemoteBackendUnavailable")
    except RemoteBackendUnavailable as exc:
        assert "Unknown remote provider" in str(exc), str(exc)


def test_manual_provider_requires_base_url() -> None:
    """No base_url configured + no env override -> error."""
    # Stash the env var if set so the test is hermetic.
    saved = os.environ.pop("EVO_SANDBOX_BASE_URL", None)
    try:
        try:
            ManualProvider({})
            raise AssertionError("expected RemoteBackendUnavailable")
        except RemoteBackendUnavailable as exc:
            assert "base_url" in str(exc), str(exc)
    finally:
        if saved is not None:
            os.environ["EVO_SANDBOX_BASE_URL"] = saved


def test_health_and_auth(workdir: Path) -> None:
    """Real sandbox-agent: /v1/health works with the right token, 401s with wrong."""
    with localhost_sandbox_agent() as (base_url, token):
        with SandboxAgentClient(base_url, bearer_token=token) as client:
            health = client.health()
            assert health == {"status": "ok"}, health

        with SandboxAgentClient(base_url, bearer_token="wrong") as client:
            try:
                client.health()
                raise AssertionError("expected SandboxAgentError")
            except SandboxAgentError as exc:
                assert exc.status == 401, exc.status


def test_fs_round_trip(workdir: Path) -> None:
    with localhost_sandbox_agent() as (base_url, token):
        with SandboxAgentClient(base_url, bearer_token=token) as client:
            client.fs_mkdir("/tmp/evo-test", recursive=True)
            client.fs_write("/tmp/evo-test/hello.txt", b"world\n")
            assert client.fs_read("/tmp/evo-test/hello.txt") == b"world\n"
            entries = client.fs_entries("/tmp/evo-test")
            names = [e.name for e in entries]
            assert "hello.txt" in names, names


def test_process_run_executes_command(workdir: Path) -> None:
    with localhost_sandbox_agent() as (base_url, token):
        with SandboxAgentClient(base_url, bearer_token=token) as client:
            result = client.process_run("echo", args=["hello"])
            assert result.exit_code == 0, result.stderr
            assert "hello" in result.stdout


def test_git_bundle_round_trip(workdir: Path) -> None:
    """Real bundle round-trip: local repo -> sandbox-agent's filesystem ->
    new commit -> back to local repo. The sandbox-agent here treats
    /workspace/repo as a normal directory; we provision a real git repo
    inside that path before running bundle ops."""
    repo = _build_repo(workdir)
    parent_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()

    with localhost_sandbox_agent() as (base_url, token):
        with SandboxAgentClient(base_url, bearer_token=token) as client:
            # Create the in-sandbox repo at /tmp/sandbox-clone (sandbox-agent
            # binds to host fs; we use /tmp so cleanup is automatic).
            sandbox_repo = workdir / "sandbox_clone"
            sandbox_repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=sandbox_repo, check=True)
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=sandbox_repo, check=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=sandbox_repo, check=True)

            # Pass test-specific layout via the function args (no global mutation).
            sandbox_repo_str = str(sandbox_repo)
            bundle_dir_str = str(workdir / "bundles")

            git_bundle.ship_commit_to_sandbox(
                client, local_repo=repo, commit=parent_commit,
                sandbox_repo=sandbox_repo_str, bundle_dir=bundle_dir_str,
            )
            # Verify the commit landed in the sandbox repo.
            check = subprocess.run(
                ["git", "cat-file", "-e", parent_commit],
                cwd=sandbox_repo, capture_output=True,
            )
            assert check.returncode == 0, "parent commit missing in sandbox repo"

            # Make a new commit in the sandbox repo.
            subprocess.run(
                ["git", "checkout", "-q", parent_commit], cwd=sandbox_repo, check=True,
            )
            (sandbox_repo / "new_file.txt").write_text("from sandbox\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=sandbox_repo, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "sandbox-side commit"],
                cwd=sandbox_repo, check=True,
            )
            new_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=sandbox_repo,
                capture_output=True, text=True, check=True,
            ).stdout.strip()

            git_bundle.fetch_commit_from_sandbox(
                client, local_repo=repo,
                base_commit=parent_commit, head_commit=new_commit,
                sandbox_repo=sandbox_repo_str, bundle_dir=bundle_dir_str,
            )

            # Local repo now has the new commit.
            local_check = subprocess.run(
                ["git", "cat-file", "-e", new_commit],
                cwd=repo, capture_output=True,
            )
            assert local_check.returncode == 0, "new commit not landed locally"


def test_remote_backend_full_lifecycle(workdir: Path) -> None:
    """End-to-end with a real sandbox-agent + ManualProvider: allocate,
    discard, allocate again. Validates the lease lifecycle, _setup_workspace
    actually doing the bundle + checkout via real HTTP, and tear-down."""
    repo = _build_repo(workdir)

    with localhost_sandbox_agent() as (base_url, token):
        # Manual provider reads workspace_root + bundle_dir from
        # provider_config so the in-sandbox paths resolve to dirs the
        # localhost sandbox-agent (running as the test user, not in a
        # container) can actually create.
        sandbox_workspace = workdir / "in-sandbox-workspace"
        sandbox_bundles = workdir / "in-sandbox-bundles"
        provider_config = (
            f"base_url={base_url},bearer_token={token},"
            f"workspace_root={sandbox_workspace},"
            f"bundle_dir={sandbox_bundles}"
        )
        _evo(
            ["init", "--target", "agent.py",
             "--benchmark", "python eval.py",
             "--metric", "max", "--host", "generic",
             "--backend", "remote", "--provider", "manual",
             "--provider-config", provider_config],
            cwd=repo,
        )
        try:
            config_path = repo / ".evo" / "run_0000" / "config.json"
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            assert cfg["execution_backend"] == "remote", cfg
            assert cfg["execution_backend_config"]["provider"] == "manual", cfg
            # commit_strategy defaults to tracked-only for remote (parallels pool)
            assert cfg["commit_strategy"] == "tracked-only", cfg

            # `evo new` should drive RemoteSandboxBackend.allocate, which
            # provisions (a no-op for manual; just returns the URL) and
            # ships the parent commit + checks out the experiment branch.
            new_result = _evo(
                ["new", "--parent", "root", "-m", "remote test"],
                cwd=repo, check=False,
                env={"EVO_FORCE_FRESH_BACKEND": "1"},
            )
            assert new_result.returncode == 0, (
                f"evo new failed:\nSTDOUT: {new_result.stdout}\n"
                f"STDERR: {new_result.stderr}"
            )

            state = remote_state.read_state(repo)
            assert state["provider"] == "manual", state
            assert len(state["sandboxes"]) == 1, state
            sandbox = state["sandboxes"][0]
            assert sandbox["leased_by"]["exp_id"] == "exp_0000", sandbox

            # Verify the in-sandbox repo got the parent commit + branch.
            # workspace_root was overridden to a tmp path via provider_config;
            # read the resolved path from remote_state so the test doesn't
            # bake in the in-container default.
            state = remote_state.read_state(repo)
            sandbox_workspace_path = state["sandboxes"][0]["workspace_root"]
            with SandboxAgentClient(base_url, bearer_token=token) as client:
                check = client.process_run(
                    "git", args=["rev-parse", "HEAD"],
                    cwd=sandbox_workspace_path,
                )
                assert check.exit_code == 0, check.stderr
                head_in_sandbox = check.stdout.strip()
                local_head = subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=repo,
                    capture_output=True, text=True, check=True,
                ).stdout.strip()
                assert head_in_sandbox == local_head, (
                    f"sandbox HEAD {head_in_sandbox} != local HEAD {local_head}"
                )

            # Discard releases the lease.
            _evo(["discard", "exp_0000", "--reason", "test cleanup"], cwd=repo)
            state_after = remote_state.read_state(repo)
            # Manual provider's tear_down is a no-op, so the slot stays
            # in remote_state (with leased_by cleared). Actually -- the
            # backend's discard removes the slot entry entirely. Verify.
            assert state_after["sandboxes"] == [] or all(
                s.get("leased_by") is None for s in state_after["sandboxes"]
            ), state_after
        finally:
            _shutdown_dashboard(repo)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def test_workspace_ops_cli_subcommands(workdir: Path) -> None:
    """`evo bash | read | write | edit | glob | grep --exp-id <id>` against
    a real sandbox-agent on localhost. Validates the host-agnostic
    discipline: every workspace op requires --exp-id, errors loudly without."""
    repo = _build_repo(workdir)

    with localhost_sandbox_agent() as (base_url, token):
        sandbox_workspace = workdir / "in-sandbox-workspace"
        sandbox_bundles = workdir / "in-sandbox-bundles"
        provider_config = (
            f"base_url={base_url},bearer_token={token},"
            f"workspace_root={sandbox_workspace},"
            f"bundle_dir={sandbox_bundles}"
        )
        _evo(
            ["init", "--target", "agent.py",
             "--benchmark", "python eval.py",
             "--metric", "max", "--host", "generic",
             "--backend", "remote", "--provider", "manual",
             "--provider-config", provider_config],
            cwd=repo,
        )
        try:
            _evo(["new", "--parent", "root", "-m", "ws-ops test"], cwd=repo)

            workspace_path = str(sandbox_workspace)

            # 1. evo bash --exp-id (in-sandbox shell exec)
            out = _evo(["bash", "--exp-id", "exp_0000",
                        f"echo from-sandbox-{42}"], cwd=repo)
            assert "from-sandbox-42" in out.stdout, out.stdout

            # 2. evo write --exp-id (with --content)
            _evo(["write", "--exp-id", "exp_0000",
                  f"{workspace_path}/agent.py",
                  "--content", "STATE = 'GOOD via evo write'\n"], cwd=repo)

            # 3. evo read --exp-id (verify the write)
            out = _evo(["read", "--exp-id", "exp_0000",
                        f"{workspace_path}/agent.py"], cwd=repo)
            assert "GOOD via evo write" in out.stdout, out.stdout

            # 4. evo edit --exp-id (search-replace)
            _evo(["edit", "--exp-id", "exp_0000",
                  f"{workspace_path}/agent.py",
                  "--old", "GOOD via evo write",
                  "--new", "EVEN BETTER"], cwd=repo)
            out = _evo(["read", "--exp-id", "exp_0000",
                        f"{workspace_path}/agent.py"], cwd=repo)
            assert "EVEN BETTER" in out.stdout, out.stdout

            # 5. evo glob --exp-id
            out = _evo(["glob", "--exp-id", "exp_0000",
                        "*.py", "--path", workspace_path], cwd=repo)
            assert "agent.py" in out.stdout, out.stdout
            assert "eval.py" in out.stdout, out.stdout

            # 6. evo grep --exp-id
            out = _evo(["grep", "--exp-id", "exp_0000",
                        "EVEN BETTER", "--path", workspace_path], cwd=repo)
            assert "EVEN BETTER" in out.stdout, out.stdout

            # 7. Strict --exp-id discipline: missing flag = error
            missing = _evo(["bash", "echo nope"], cwd=repo, check=False)
            assert missing.returncode != 0, missing.stdout
            assert "exp-id" in (missing.stderr + missing.stdout).lower(), missing.stderr

            # 8. Wrong/unleased exp_id = error (typo protection)
            wrong = _evo(["bash", "--exp-id", "exp_9999", "echo wrong"],
                         cwd=repo, check=False)
            assert wrong.returncode != 0, wrong.stdout

            # 9. Edit with non-unique --old refuses without --replace-all
            # First write a file with two occurrences of the same string.
            _evo(["write", "--exp-id", "exp_0000",
                  f"{workspace_path}/dup.txt",
                  "--content", "X\nX\n"], cwd=repo)
            dup_attempt = _evo(["edit", "--exp-id", "exp_0000",
                                f"{workspace_path}/dup.txt",
                                "--old", "X", "--new", "Y"],
                               cwd=repo, check=False)
            assert dup_attempt.returncode != 0, dup_attempt.stdout
            assert "not unique" in dup_attempt.stderr.lower(), dup_attempt.stderr
            # And with --replace-all, both get replaced.
            _evo(["edit", "--exp-id", "exp_0000",
                  f"{workspace_path}/dup.txt",
                  "--old", "X", "--new", "Y", "--replace-all"], cwd=repo)
            out = _evo(["read", "--exp-id", "exp_0000",
                        f"{workspace_path}/dup.txt"], cwd=repo)
            assert out.stdout == "Y\nY\n", repr(out.stdout)
        finally:
            _shutdown_dashboard(repo)


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="evo-remote-integ-"))
    try:
        # Tests with no fixture
        for fn in (
            test_known_providers_lists_modal_and_manual,
            test_unknown_provider_raises_clear_error,
            test_manual_provider_requires_base_url,
        ):
            print(f"--- {fn.__name__} ---")
            fn()
            print("    OK")

        # Tests requiring a workdir
        for fn in (
            test_health_and_auth,
            test_fs_round_trip,
            test_process_run_executes_command,
            test_git_bundle_round_trip,
            test_remote_backend_full_lifecycle,
            test_workspace_ops_cli_subcommands,
        ):
            sub = workdir / fn.__name__
            sub.mkdir()
            print(f"--- {fn.__name__} ---")
            fn(sub)
            print("    OK")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print("INTEG REMOTE LOCALHOST OK")


if __name__ == "__main__":
    main()
