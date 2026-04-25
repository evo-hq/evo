"""End-to-end live tests for `evo dispatch` (claude-code path).

Same hand-rolled style as tests/e2e.py: real `evo` subprocess against a
real tmp git repo, no pytest, no mocks. Differences:

* These tests fire real `claude -p` subprocesses against the Anthropic
  API. Skipped unless EVO_LIVE_TEST_CLAUDE=1 is set.
* Cost: roughly $0.30–$1.00 per full run. Use the cheap-model env var
  EVO_DISPATCH_MODEL=haiku to drop further if needed.

What's covered (in order):

  test_ensure_explorer_spawns_and_persists
    Real claude-p call through dispatch.ensure_explorer; verify session
    record on disk has all required fields.

  test_ensure_explorer_reuses_within_ttl
    Second call with the same parent reuses the record (created_at
    unchanged, same session_id) — proves the cache predicate works.

  test_ensure_explorer_rebuilds_when_skill_changes
    Edit subagent/SKILL.md → next ensure_explorer rebuilds with a new
    session_id. Verifies the skill-hash invalidation path. Restores the
    skill in a finally block.

  test_orchestrator_runs_step01_auto_migration
    Dogfood: spawn claude -p as a real orchestrator, point it at
    optimize/SKILL.md step 0.1, observe that it runs `evo host set
    claude-code` on a workspace simulating a pre-upgrade state. Asserts
    the workspace's host field is correctly set afterward.

Run: `EVO_LIVE_TEST_CLAUDE=1 python tests/e2e_dispatch.py`
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "evo"
PLUGIN_SRC = PLUGIN_ROOT / "src"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _run(args: list[str], cwd: Path, check: bool = True, **kwargs):
    return subprocess.run(args, cwd=cwd, check=check, capture_output=True, text=True, **kwargs)


def _evo(args: list[str], cwd: Path, check: bool = True):
    return _run(["uv", "run", "--project", str(PLUGIN_ROOT), "evo", *args], cwd=cwd, check=check)


def _init_repo(root: Path) -> None:
    _run(["git", "init", "-b", "main"], cwd=root)
    _run(["git", "config", "user.name", "evo"], cwd=root)
    _run(["git", "config", "user.email", "evo@example.com"], cwd=root)
    _run(["git", "commit", "--allow-empty", "-m", "initial"], cwd=root)


def _setup_minimal_workspace(root: Path, *, host: str | None) -> None:
    """Trivial workspace with bench.sh always returning score:1.0. Just
    enough to satisfy `evo init`. Not run during these tests."""
    (root / "bench.sh").write_text("echo score:1.0\n")
    (root / "bench.sh").chmod(0o755)
    args = [
        "init",
        "--target", "bench.sh",
        "--benchmark", "./bench.sh",
        "--metric", "max",
    ]
    if host is not None:
        args += ["--host", host]
    _evo(args, cwd=root)


def _strip_host_from_meta(root: Path) -> None:
    """Simulate a workspace built before the host signature field existed."""
    p = root / ".evo" / "meta.json"
    meta = json.loads(p.read_text(encoding="utf-8"))
    meta.pop("host", None)
    p.write_text(json.dumps(meta), encoding="utf-8")


def _import_dispatch():
    """Importing evo.dispatch needs PLUGIN_SRC on sys.path; do it lazily so
    the module loads only when these tests actually run."""
    if str(PLUGIN_SRC) not in sys.path:
        sys.path.insert(0, str(PLUGIN_SRC))
    import evo.dispatch as dispatch  # noqa: WPS433
    import evo.core as core  # noqa: WPS433
    return dispatch, core


def _shutdown_dashboard(root: Path) -> None:
    pid_file = root / ".evo" / "dashboard.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 15)
        except (ValueError, ProcessLookupError, OSError):
            pass


# ---------------------------------------------------------------------------
# Test 1: ensure_explorer spawn + persist
# ---------------------------------------------------------------------------


def test_ensure_explorer_spawns_and_persists(root: Path) -> None:
    dispatch, core = _import_dispatch()
    _setup_minimal_workspace(root, host="claude-code")
    record = dispatch.ensure_explorer(root, parent_id="root")
    assert record["host"] == "claude-code", record
    assert record["session_id"], record
    assert record["worktree_commit"], record
    assert record["skill_hash"], record
    assert record["ttl_expires_at"], record
    on_disk = json.loads(dispatch.explorer_record_path(root, "root").read_text())
    assert on_disk["session_id"] == record["session_id"]
    print("  PASS test_ensure_explorer_spawns_and_persists")


# ---------------------------------------------------------------------------
# Test 2: reuse within TTL
# ---------------------------------------------------------------------------


def test_ensure_explorer_reuses_within_ttl(root: Path) -> None:
    dispatch, core = _import_dispatch()
    _setup_minimal_workspace(root, host="claude-code")
    rec1 = dispatch.ensure_explorer(root, parent_id="root")
    rec2 = dispatch.ensure_explorer(root, parent_id="root")
    assert rec2["session_id"] == rec1["session_id"], (rec1["session_id"], rec2["session_id"])
    assert rec2["created_at"] == rec1["created_at"]
    print("  PASS test_ensure_explorer_reuses_within_ttl")


# ---------------------------------------------------------------------------
# Test 3: skill hash change invalidates
# ---------------------------------------------------------------------------


def test_ensure_explorer_rebuilds_when_skill_changes(root: Path) -> None:
    dispatch, core = _import_dispatch()
    _setup_minimal_workspace(root, host="claude-code")
    rec1 = dispatch.ensure_explorer(root, parent_id="root")

    skill = dispatch.subagent_skill_path()
    original = skill.read_text(encoding="utf-8")
    try:
        skill.write_text(original + "\n<!-- live e2e invalidation marker -->\n", encoding="utf-8")
        rec2 = dispatch.ensure_explorer(root, parent_id="root")
        assert rec2["session_id"] != rec1["session_id"], "skill change must rebuild explorer"
        assert rec2["skill_hash"] != rec1["skill_hash"]
    finally:
        skill.write_text(original, encoding="utf-8")
    print("  PASS test_ensure_explorer_rebuilds_when_skill_changes")


# ---------------------------------------------------------------------------
# Test 4: orchestrator dogfood — step 0.1 auto-migration
# ---------------------------------------------------------------------------


ORCHESTRATOR_PROMPT_TEMPLATE = (
    "You are an evo optimization orchestrator running in Claude Code. "
    "The workspace at {workspace} predates the host signature field. "
    "Read {plugin_root}/skills/optimize/SKILL.md and follow step 0.1 "
    "EXACTLY ONCE on that workspace, then stop. Use exactly this binary "
    "for every evo invocation (do not rely on PATH): {evo_bin}. "
    "After you run the migration command, run '{evo_bin} host show' from "
    "that workspace and report what it returns. Do not do anything else "
    "from the optimize loop beyond step 0.1."
)


def _resolve_test_evo_bin() -> Path:
    """Pick the evo binary the test should hand to the spawned orchestrator.

    Priority: EVO_BIN env override → plugin's .venv/bin/evo → system PATH.
    Spawned claude -p inherits the test's PATH, but the user may have an
    older `evo` installed globally that lacks the host subcommand. Always
    pass an explicit absolute path to make the test deterministic."""
    override = os.environ.get("EVO_BIN")
    if override:
        return Path(override)
    venv_bin = PLUGIN_ROOT / ".venv" / "bin" / "evo"
    if venv_bin.exists():
        return venv_bin
    found = shutil.which("evo")
    if not found:
        raise RuntimeError("no evo binary found; set EVO_BIN or install plugins/evo")
    return Path(found)


def test_orchestrator_runs_step01_auto_migration(root: Path) -> None:
    """Spawn a real claude -p as the orchestrator, point it at step 0.1
    of optimize/SKILL.md, and verify the workspace's host field is set
    correctly afterward."""
    dispatch, core = _import_dispatch()

    # Set up workspace with --host claude-code (required), then strip the
    # field to simulate a pre-upgrade workspace.
    _setup_minimal_workspace(root, host="claude-code")
    _strip_host_from_meta(root)
    _shutdown_dashboard(root)
    assert core.get_host(root) is None, "pre-state: host should be unset"

    evo_bin = _resolve_test_evo_bin()
    prompt = ORCHESTRATOR_PROMPT_TEMPLATE.format(
        workspace=str(root),
        plugin_root=str(PLUGIN_ROOT),
        evo_bin=str(evo_bin),
    )

    # bypassPermissions is appropriate inside an isolated tmpdir test;
    # would never be set in real user runs.
    proc = subprocess.run(
        [
            "claude", "-p",
            "--output-format", "json",
            "--permission-mode", "bypassPermissions",
            "--add-dir", str(root),
            "--add-dir", str(PLUGIN_ROOT),
        ],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, f"claude -p failed: {proc.stderr[:500]}"

    events = json.loads(proc.stdout)
    result = next((e for e in events if e.get("type") == "result"), None)
    assert result is not None, "no result event in claude -p output"
    assert result.get("subtype") == "success", f"orchestrator failed: {result.get('result', '')[:300]}"

    # The load-bearing assertion: the orchestrator must have set host
    # to claude-code on the workspace.
    final_host = core.get_host(root)
    assert final_host == "claude-code", (
        f"orchestrator did not run step 0.1 correctly. "
        f"final host: {final_host!r}; "
        f"orchestrator final message: {result.get('result', '')[:400]}"
    )
    print(f"  PASS test_orchestrator_runs_step01_auto_migration (cost ${result.get('total_cost_usd', 0):.2f})")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def main() -> int:
    if os.environ.get("EVO_LIVE_TEST_CLAUDE") != "1":
        print("SKIP: set EVO_LIVE_TEST_CLAUDE=1 to enable live dispatch e2e tests")
        return 0

    if shutil.which(os.environ.get("EVO_CLAUDE_BIN", "claude")) is None:
        print("SKIP: claude CLI not on PATH")
        return 0

    temp_root = Path(tempfile.mkdtemp(prefix="evo-e2e-dispatch-"))
    print(f"Live e2e dispatch tests under {temp_root}")
    try:
        for fn in (
            test_ensure_explorer_spawns_and_persists,
            test_ensure_explorer_reuses_within_ttl,
            test_ensure_explorer_rebuilds_when_skill_changes,
            test_orchestrator_runs_step01_auto_migration,
        ):
            sub = temp_root / fn.__name__
            sub.mkdir()
            _init_repo(sub)
            try:
                fn(sub)
            finally:
                _shutdown_dashboard(sub)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    print("E2E dispatch OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
