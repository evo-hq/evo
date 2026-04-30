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
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SRC = REPO_ROOT / "plugins" / "evo" / "src"
sys.path.insert(0, str(PLUGIN_SRC))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from _sandbox_agent_fixture import (  # noqa: E402
    localhost_sandbox_agent,
    managed_localhost_sandbox_agent,
)
from _sshd_fixture import localhost_sshd  # noqa: E402

from evo.backends import (  # noqa: E402
    AllocateCtx,
    DiscardCtx,
    PoolExhausted,
    RemoteBackendUnavailable,
    RemoteSandboxBackend,
    backend_state_key,
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


class FixturePoolProvider:
    """Real sandbox-provider shim over a fixed list of localhost daemons."""

    name = "fixture-pool"

    def __init__(self, endpoints: list[tuple[str, str]]) -> None:
        self._endpoints = list(endpoints)
        self._next = 0

    def provision(self, spec) -> Any:  # type: ignore[no-untyped-def]
        from evo.backends.protocol import SandboxHandle

        if self._next >= len(self._endpoints):
            raise RuntimeError("fixture pool exhausted")
        base_url, token = self._endpoints[self._next]
        self._next += 1
        return SandboxHandle(
            provider=self.name,
            base_url=base_url,
            bearer_token=token,
            native_id=f"fixture-{self._next}",
            metadata={
                "workspace_root": f"/tmp/evo-fixture-pool-{self._next}/repo",
                "bundle_dir": f"/tmp/evo-fixture-pool-{self._next}/bundles",
            },
        )

    def tear_down(self, handle) -> None:  # type: ignore[no-untyped-def]
        return

    def is_alive(self, handle) -> bool:  # type: ignore[no-untyped-def]
        try:
            with SandboxAgentClient(handle.base_url, bearer_token=handle.bearer_token) as client:
                client.health()
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_known_providers_lists_modal_manual_ssh_e2b_and_daytona() -> None:
    providers = known_providers()
    assert "modal" in providers, providers
    assert "manual" in providers, providers
    assert "ssh" in providers, providers
    assert "e2b" in providers, providers
    assert "daytona" in providers, providers
    assert "aws" in providers, providers


def test_unknown_provider_raises_clear_error() -> None:
    try:
        load_provider("e2b-not-yet-shipped", {})
        raise AssertionError("expected RemoteBackendUnavailable")
    except RemoteBackendUnavailable as exc:
        assert "Unknown remote provider" in str(exc), str(exc)


def test_dotted_path_provider_loads() -> None:
    provider = load_provider(
        "evo.backends.sandbox_providers.manual:ManualProvider",
        {"base_url": "http://127.0.0.1:9999", "bearer_token": "t"},
    )
    assert isinstance(provider, ManualProvider), provider


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


def test_ssh_provider_requires_host() -> None:
    try:
        load_provider("ssh", {})
        raise AssertionError("expected RemoteBackendUnavailable")
    except RemoteBackendUnavailable as exc:
        assert "requires host" in str(exc), str(exc)


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
             "--benchmark", "python3 eval.py",
             "--metric", "max", "--host", "generic"],
            cwd=repo,
        )
        try:
            config_path = repo / ".evo" / "run_0000" / "config.json"
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            assert cfg["execution_backend"] == "worktree", cfg
            assert "execution_backend_config" not in cfg, cfg
            assert cfg["commit_strategy"] == "all", cfg
            keyfile = repo / ".evo" / "keyfile"
            assert keyfile.exists(), keyfile
            assert oct(keyfile.stat().st_mode & 0o777) == "0o600", oct(keyfile.stat().st_mode & 0o777)

            # `evo new` should drive RemoteSandboxBackend.allocate, which
            # provisions (a no-op for manual; just returns the URL), ships
            # the parent commit, and persists the node-level backend choice.
            new_result = _evo(
                ["new", "--parent", "root", "-m", "remote test",
                 "--remote", "manual",
                 "--provider-config", provider_config],
                cwd=repo, check=False,
            )
            assert new_result.returncode == 0, (
                f"evo new failed:\nSTDOUT: {new_result.stdout}\n"
                f"STDERR: {new_result.stderr}"
            )

            graph = json.loads((repo / ".evo" / "run_0000" / "graph.json").read_text(encoding="utf-8"))
            node = graph["nodes"]["exp_0000"]
            assert node["backend"] == "remote", node
            assert node["backend_config"]["provider"] == "manual", node

            state = remote_state.read_state(repo)
            assert state["provider"] == "manual", state
            assert len(state["sandboxes"]) == 1, state
            sandbox = state["sandboxes"][0]
            assert sandbox["leased_by"]["exp_id"] == "exp_0000", sandbox
            raw_state = (repo / ".evo" / "run_0000" / "backend_state" / "remote-"
                         f"{backend_state_key('remote', node['backend_config'])}.json").read_text(encoding="utf-8")
            assert token not in raw_state, raw_state
            assert "bearer_token_enc" in raw_state, raw_state

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
             "--benchmark", "python3 eval.py",
             "--metric", "max", "--host", "generic"],
            cwd=repo,
        )
        try:
            _evo(
                ["new", "--parent", "root", "-m", "ws-ops test",
                 "--remote", "manual",
                 "--provider-config", provider_config],
                cwd=repo,
            )

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


def test_distinct_remote_configs_can_be_live_in_one_run(workdir: Path) -> None:
    """Two concurrent remote experiments with different provider configs
    must not share one remote_state file."""
    repo = _build_repo(workdir)

    with localhost_sandbox_agent() as (base_url_a, token_a), localhost_sandbox_agent() as (base_url_b, token_b):
        sandbox_workspace_a = workdir / "sandbox-a"
        sandbox_workspace_b = workdir / "sandbox-b"
        sandbox_bundles_a = workdir / "bundles-a"
        sandbox_bundles_b = workdir / "bundles-b"
        provider_config_a = (
            f"base_url={base_url_a},bearer_token={token_a},"
            f"workspace_root={sandbox_workspace_a},bundle_dir={sandbox_bundles_a}"
        )
        provider_config_b = (
            f"base_url={base_url_b},bearer_token={token_b},"
            f"workspace_root={sandbox_workspace_b},bundle_dir={sandbox_bundles_b}"
        )
        _evo(
            ["init", "--target", "agent.py",
             "--benchmark", "python eval.py",
             "--metric", "max", "--host", "generic"],
            cwd=repo,
        )
        try:
            _evo(
                ["new", "--parent", "root", "-m", "remote A",
                 "--remote", "manual",
                 "--provider-config", provider_config_a],
                cwd=repo,
            )
            _evo(
                ["new", "--parent", "root", "-m", "remote B",
                 "--remote", "manual",
                 "--provider-config", provider_config_b],
                cwd=repo,
            )

            graph = json.loads((repo / ".evo" / "run_0000" / "graph.json").read_text(encoding="utf-8"))
            cfg_a = graph["nodes"]["exp_0000"]["backend_config"]
            cfg_b = graph["nodes"]["exp_0001"]["backend_config"]
            state_a = remote_state.read_state(repo, backend_state_key("remote", cfg_a))
            state_b = remote_state.read_state(repo, backend_state_key("remote", cfg_b))
            assert state_a["provider_config"]["base_url"] == base_url_a, state_a
            assert state_b["provider_config"]["base_url"] == base_url_b, state_b
            assert state_a["sandboxes"][0]["leased_by"]["exp_id"] == "exp_0000", state_a
            assert state_b["sandboxes"][0]["leased_by"]["exp_id"] == "exp_0001", state_b
        finally:
            _shutdown_dashboard(repo)


def test_multiple_remote_leases_same_config_and_pool_size(workdir: Path) -> None:
    repo = _build_repo(workdir)
    with (
        managed_localhost_sandbox_agent() as sandbox_a,
        managed_localhost_sandbox_agent() as sandbox_b,
    ):
        provider = FixturePoolProvider([
            (sandbox_a.base_url, sandbox_a.bearer_token),
            (sandbox_b.base_url, sandbox_b.bearer_token),
        ])
        backend = RemoteSandboxBackend(
            provider,
            provider_name="fixture-pool",
            provider_config={"pool_size": "2"},
        )

        root_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        first = backend.allocate(
            AllocateCtx(
                root=repo,
                exp_id="exp_0000",
                parent_node=None,
                parent_commit=root_commit,
                parent_ref="main",
                branch="evo/run_0000/exp_0000",
                hypothesis="first",
            )
        )
        second = backend.allocate(
            AllocateCtx(
                root=repo,
                exp_id="exp_0001",
                parent_node=None,
                parent_commit=root_commit,
                parent_ref="main",
                branch="evo/run_0000/exp_0001",
                hypothesis="second",
            )
        )
        assert first.worktree != second.worktree, (first, second)

        state = remote_state.read_state(repo, backend.state_key)
        assert len(state["sandboxes"]) == 2, state
        leased = sorted(s["leased_by"]["exp_id"] for s in state["sandboxes"])
        assert leased == ["exp_0000", "exp_0001"], leased

        try:
            backend.allocate(
                AllocateCtx(
                    root=repo,
                    exp_id="exp_0002",
                    parent_node=None,
                    parent_commit=root_commit,
                    parent_ref="main",
                    branch="evo/run_0000/exp_0002",
                    hypothesis="third",
                )
            )
            raise AssertionError("expected PoolExhausted")
        except PoolExhausted:
            pass


def test_plaintext_remote_token_migrates_on_read(workdir: Path) -> None:
    repo = _build_repo(workdir)
    _evo(
        ["init", "--target", "agent.py",
         "--benchmark", "python eval.py",
         "--metric", "max", "--host", "generic"],
        cwd=repo,
    )
    state_key = backend_state_key(
        "remote",
        {"provider": "manual", "provider_config": {"base_url": "http://127.0.0.1:9999"}},
    )
    state_path = repo / ".evo" / "run_0000" / "backend_state" / f"remote-{state_key}.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    legacy = {
        "provider": "manual",
        "provider_config": {"base_url": "http://127.0.0.1:9999"},
        "sandboxes": [
            {
                "id": 0,
                "native_id": "legacy",
                "base_url": "http://127.0.0.1:9999",
                "bearer_token": "plain-secret",
                "leased_by": None,
                "last_branch": None,
                "provisioned_at": None,
            }
        ],
    }
    state_path.write_text(json.dumps(legacy, indent=2), encoding="utf-8")

    state = remote_state.read_state(repo, state_key)
    assert state["sandboxes"][0]["bearer_token"] == "plain-secret", state
    rewritten = state_path.read_text(encoding="utf-8")
    assert "plain-secret" not in rewritten, rewritten
    assert "bearer_token_enc" in rewritten, rewritten


def test_remote_streaming_salvages_partial_logs_and_traces(workdir: Path) -> None:
    repo = workdir / "repo-stream"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "agent.py").write_text("STATE='baseline'\n", encoding="utf-8")
    (repo / "eval.py").write_text(
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "traces = Path(os.environ['EVO_TRACES_DIR'])\n"
        "traces.mkdir(parents=True, exist_ok=True)\n"
        "for i in range(3):\n"
        "    (traces / f'task_{i}.json').write_text(json.dumps({'task_id': i, 'score': 1.0, 'summary': f'task-{i}'}))\n"
        "    print(f'tick-{i}', flush=True)\n"
        "    print(f'err-{i}', file=sys.stderr, flush=True)\n"
        "    time.sleep(1.0)\n"
        "Path(os.environ['EVO_RESULT_PATH']).write_text(json.dumps({'score': 1.0, 'tasks': {'0': 1.0}}))\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "stream fixture"], cwd=repo, check=True)

    with managed_localhost_sandbox_agent() as sandbox:
        sandbox_workspace = workdir / "stream-sandbox-workspace"
        sandbox_bundles = workdir / "stream-sandbox-bundles"
        provider_config = (
            f"base_url={sandbox.base_url},bearer_token={sandbox.bearer_token},"
            f"workspace_root={sandbox_workspace},bundle_dir={sandbox_bundles}"
        )
        _evo(
            ["init", "--target", "agent.py",
             "--benchmark", "python eval.py",
             "--metric", "max", "--host", "generic"],
            cwd=repo,
        )
        try:
            _evo(
                ["new", "--parent", "root", "-m", "stream test",
                 "--remote", "manual",
                 "--provider-config", provider_config],
                cwd=repo,
            )
            proc = subprocess.Popen(
                ["uv", "run", "--project", str(PLUGIN_ROOT), "evo", "run", "exp_0000"],
                cwd=repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            time.sleep(1.6)
            sandbox.process.terminate()
            stdout, stderr = proc.communicate(timeout=30)
            assert proc.returncode != 0, (stdout, stderr)

            attempt_dir = repo / ".evo" / "run_0000" / "experiments" / "exp_0000" / "attempts" / "001"
            benchmark_log = (attempt_dir / "benchmark.log").read_text(encoding="utf-8")
            benchmark_err = (attempt_dir / "benchmark_err.log").read_text(encoding="utf-8")
            traces_dir = attempt_dir / "traces"

            assert "tick-0" in benchmark_log, benchmark_log
            assert "err-0" in benchmark_err, benchmark_err
            assert any(traces_dir.glob("task_*.json")), list(traces_dir.glob("*"))

            graph = json.loads((repo / ".evo" / "run_0000" / "graph.json").read_text(encoding="utf-8"))
            node = graph["nodes"]["exp_0000"]
            assert node["status"] == "failed", node
            assert node.get("score") is not None, node
        finally:
            _shutdown_dashboard(repo)


def test_ssh_provider_full_lifecycle(workdir: Path) -> None:
    repo = _build_repo(workdir)

    with localhost_sshd() as ssh_info:
        env = {"HOME": str(ssh_info["local_home"])}
        _evo(
            ["init", "--target", "agent.py",
             "--benchmark", "python eval.py",
             "--metric", "max", "--host", "generic"],
            cwd=repo,
            env=env,
        )
        try:
            new_result = _evo(
                [
                    "new",
                    "--parent", "root",
                    "-m", "ssh test",
                    "--remote", f"ssh:{ssh_info['host']}:{ssh_info['port']}",
                    "--provider-config", f"key={ssh_info['key']}",
                ],
                cwd=repo,
                env=env,
                check=False,
            )
            assert new_result.returncode == 0, (
                f"evo new failed:\nSTDOUT: {new_result.stdout}\n"
                f"STDERR: {new_result.stderr}"
            )

            graph = json.loads(
                (repo / ".evo" / "run_0000" / "graph.json").read_text(encoding="utf-8")
            )
            node = graph["nodes"]["exp_0000"]
            assert node["backend"] == "remote", node
            assert node["backend_config"]["provider"] == "ssh", node

            state_key = backend_state_key("remote", node["backend_config"])
            state = remote_state.read_state(repo, state_key)
            sandbox = state["sandboxes"][0]
            metadata = sandbox["metadata"]

            ssh_check = subprocess.run(
                [
                    "ssh",
                    "-o", "BatchMode=yes",
                    "-o", "StrictHostKeyChecking=accept-new",
                    "-p", str(ssh_info["port"]),
                    "-i", str(ssh_info["key"]),
                    str(ssh_info["host"]),
                    (
                        "sh -lc "
                        + shlex.quote(
                            f"test -x {shlex.quote(metadata['agent_bin'])} && "
                            f"test -s {shlex.quote(metadata['pid_path'])} && "
                            f"kill -0 \"$(cat {shlex.quote(metadata['pid_path'])})\""
                        )
                    ),
                ],
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, "HOME": str(ssh_info["local_home"])},
            )
            assert ssh_check.returncode == 0, ssh_check.stderr

            with SandboxAgentClient(
                sandbox["base_url"], bearer_token=sandbox["bearer_token"]
            ) as client:
                head = client.process_run(
                    "git",
                    args=["rev-parse", "HEAD"],
                    cwd=metadata["workspace_root"],
                )
                assert head.exit_code == 0, head.stderr

            discard = _evo(
                ["discard", "exp_0000", "--reason", "ssh test cleanup"],
                cwd=repo,
                env=env,
                check=False,
            )
            assert discard.returncode == 0, (
                f"evo discard failed:\nSTDOUT: {discard.stdout}\n"
                f"STDERR: {discard.stderr}"
            )

            cleaned = subprocess.run(
                [
                    "ssh",
                    "-o", "BatchMode=yes",
                    "-o", "StrictHostKeyChecking=accept-new",
                    "-p", str(ssh_info["port"]),
                    "-i", str(ssh_info["key"]),
                    str(ssh_info["host"]),
                    (
                        "sh -lc "
                        + shlex.quote(
                            f"test ! -e {shlex.quote(metadata['pid_path'])} && "
                            f"test ! -d {shlex.quote(metadata['remote_root'])}"
                        )
                    ),
                ],
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, "HOME": str(ssh_info["local_home"])},
            )
            assert cleaned.returncode == 0, cleaned.stderr
        finally:
            _shutdown_dashboard(repo)


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="evo-remote-integ-"))
    try:
        # Tests with no fixture
        for fn in (
            test_known_providers_lists_modal_manual_ssh_e2b_and_daytona,
            test_unknown_provider_raises_clear_error,
            test_dotted_path_provider_loads,
            test_manual_provider_requires_base_url,
            test_ssh_provider_requires_host,
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
            test_distinct_remote_configs_can_be_live_in_one_run,
            test_multiple_remote_leases_same_config_and_pool_size,
            test_plaintext_remote_token_migrates_on_read,
            test_remote_streaming_salvages_partial_logs_and_traces,
            test_ssh_provider_full_lifecycle,
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
