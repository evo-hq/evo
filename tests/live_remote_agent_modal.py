"""Live test: real `claude` subagent against a Modal-backed evo workspace.

Spawns an actual `claude -p` process with the subagent skill loaded as
its system prompt and a per-experiment brief as its user message. Captures
the full tool-call transcript and verifies the agent:
  - Uses `evo write --exp-id <id>` (or evo edit) to edit files in the
    sandbox, NOT native Write/Edit on the (nonexistent local) sandbox path
  - Always passes --exp-id explicitly
  - Calls `evo run <exp_id>` to execute the experiment
  - Reports a committed score that beats the parent

Skipped unless BOTH `EVO_LIVE_TEST_MODAL=1` AND `EVO_LIVE_TEST_CLAUDE=1`.
Requires:
  - `modal` SDK installed + Modal authenticated (~/.modal.toml)
  - `claude` CLI on PATH and authenticated

Cost: ~2 Modal sandboxes (~$0.02-0.05 in Modal credits) +
~$0.05-0.20 in Claude API costs per run.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "evo"
PLUGIN_SRC = PLUGIN_ROOT / "src"
SUBAGENT_SKILL = PLUGIN_ROOT / "skills" / "subagent" / "SKILL.md"
sys.path.insert(0, str(PLUGIN_SRC))


CLAUDE_BIN = os.environ.get("EVO_CLAUDE_BIN", "claude")


def _gate() -> None:
    if os.environ.get("EVO_LIVE_TEST_MODAL") != "1":
        print("SKIPPED (set EVO_LIVE_TEST_MODAL=1 to enable)")
        sys.exit(0)
    if os.environ.get("EVO_LIVE_TEST_CLAUDE") != "1":
        print("SKIPPED (set EVO_LIVE_TEST_CLAUDE=1 to enable)")
        sys.exit(0)
    try:
        import modal  # noqa: F401
    except ImportError:
        print("SKIPPED (modal SDK not installed)")
        sys.exit(0)
    if shutil.which(CLAUDE_BIN) is None:
        print(f"SKIPPED (claude CLI {CLAUDE_BIN!r} not on PATH)")
        sys.exit(0)


def _evo(args: list[str], cwd: Path, check: bool = True, timeout: int = 600):
    result = subprocess.run(
        ["uv", "run", "--project", str(PLUGIN_ROOT), "evo", *args],
        cwd=cwd, check=False, capture_output=True, text=True, timeout=timeout,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"evo {' '.join(args)} failed (rc={result.returncode}):\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result


def _build_repo(workdir: Path) -> Path:
    """Tiny fixture: agent.py + eval.py that scores by GOOD-token count."""
    repo = workdir / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "agent.py").write_text("STATE = ''\n", encoding="utf-8")
    (repo / "eval.py").write_text(
        "import os, json\n"
        "from pathlib import Path\n"
        "agent = Path('agent.py').read_text()\n"
        "score = float(agent.count('GOOD'))\n"
        "result_path = os.environ['EVO_RESULT_PATH']\n"
        "Path(result_path).parent.mkdir(parents=True, exist_ok=True)\n"
        "Path(result_path).write_text(json.dumps({'score': score, 'tasks': {}}))\n",
        encoding="utf-8",
    )
    (repo / "gate.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "sys.exit(1 if 'FORBIDDEN' in Path('agent.py').read_text() else 0)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    return repo


def _bootstrap_parent(repo: Path, provider_config: str) -> tuple[str, float]:
    """Set up a baseline experiment exp_0000 with score=1.0 so the agent
    has a meaningful target to beat. Returns (exp_id, score)."""
    print("--- bootstrap: provisioning parent exp_0000 (score=1.0) ---")
    _evo(
        ["new", "--parent", "root", "-m", "baseline",
         "--remote", "modal",
         "--provider-config", provider_config],
        cwd=repo,
        timeout=300,
    )
    _evo(["write", "--exp-id", "exp_0000",
          "/workspace/repo/agent.py",
          "--content", "STATE = 'GOOD'\n"], cwd=repo)
    out = _evo(["run", "exp_0000"], cwd=repo, timeout=300)
    assert "COMMITTED exp_0000 1.0" in out.stdout, out.stdout
    print(f"    parent ready: exp_0000 score=1.0")
    return "exp_0000", 1.0


def _allocate_test_experiment(repo: Path, parent: str, provider_config: str) -> str:
    """Allocate exp_0001 (or whatever id) under `parent`. Returns the id."""
    print("--- allocating test experiment under parent ---")
    out = _evo(
        ["new", "--parent", parent, "-m", "real-agent test",
         "--remote", "modal",
         "--provider-config", provider_config],
        cwd=repo,
        timeout=300,
    )
    new_data = json.loads(out.stdout)
    print(f"    allocated: {new_data['id']} (worktree={new_data['worktree']})")
    return new_data["id"]


def _build_brief(exp_id: str, parent: str, parent_score: float) -> str:
    """Per-experiment brief the orchestrator would send to a subagent.

    Production-realistic shape -- includes the four pieces the skill
    expects (objective, parent, boundaries, budget). Everything an
    orchestrator would actually pass.
    """
    return (
        f"Your experiment: {exp_id} (pre-allocated; do not call `evo new`).\n"
        f"Iteration budget: 1.\n"
        f"\n"
        f"Objective: make the benchmark score higher than the parent "
        f"(parent: {parent}, score={parent_score}).\n"
        f"\n"
        f'The benchmark scores agent.py by counting occurrences of the literal '
        f'token "GOOD" in the file\'s contents -- so each "GOOD" you add to '
        f"agent.py adds 1 to the score.\n"
        f"\n"
        f"Workspace target file: /workspace/repo/agent.py\n"
        f"\n"
        f"When you're done editing, run `evo run {exp_id}` and report the outcome.\n"
        f"\n"
        f'Boundaries / anti-patterns: do not modify benchmark.py or gate.py. '
        f'The gate fails if agent.py contains the literal token "FORBIDDEN" -- '
        f"avoid that token entirely.\n"
    )


def _spawn_claude(brief: str, repo: Path) -> dict:
    """Run claude -p with the subagent skill as system prompt and `brief`
    as user message. Streams events as they arrive (so a stalled run is
    diagnosable) and assembles the full transcript."""
    skill_content = SUBAGENT_SKILL.read_text(encoding="utf-8")
    cmd = [
        CLAUDE_BIN, "-p", brief,
        "--append-system-prompt", skill_content,
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", "bypassPermissions",
    ]
    print(f"--- spawning claude (system prompt = subagent skill, "
          f"{len(skill_content)} chars) ---")
    t0 = time.monotonic()

    proc = subprocess.Popen(
        cmd, cwd=repo,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )

    events: list[dict] = []
    final_payload: dict = {"messages": [], "events": events}

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append(ev)

            # Surface tool calls live so a stalled agent is diagnosable.
            ev_type = ev.get("type")
            if ev_type == "assistant":
                msg = ev.get("message", {})
                for block in msg.get("content", []) or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        name = block.get("name", "?")
                        inp = block.get("input", {})
                        if name == "Bash":
                            preview = inp.get("command", "")[:120]
                            print(f"    [t+{time.monotonic()-t0:5.1f}s] Bash: {preview}")
                        elif name in ("Read", "Write", "Edit", "MultiEdit"):
                            print(f"    [t+{time.monotonic()-t0:5.1f}s] {name}: {inp.get('file_path', '?')}")
                        else:
                            print(f"    [t+{time.monotonic()-t0:5.1f}s] {name}")
            elif ev_type == "result":
                # Final summary event from stream-json.
                final_payload["result"] = ev

        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError(
            f"claude wedged after stdout closed; killed at "
            f"t+{time.monotonic()-t0:.1f}s"
        )

    print(f"    claude returned in {time.monotonic() - t0:.1f}s "
          f"(exit={proc.returncode}, {len(events)} events)")
    if proc.returncode != 0:
        stderr_tail = proc.stderr.read()[-2000:] if proc.stderr else ""
        raise RuntimeError(
            f"claude exited {proc.returncode}\n"
            f"STDERR (last 2000 chars):\n{stderr_tail}"
        )
    final_payload["messages"] = events
    return final_payload


def _extract_tool_calls(payload: dict) -> list[dict]:
    """Pull the agent's tool_use blocks from the claude -p JSON output."""
    calls: list[dict] = []
    # Two shapes seen in claude -p output:
    # - top-level dict with "messages" or "result" + nested events
    # - JSONL where each line is an event
    candidates = []
    if isinstance(payload, dict):
        # Newer schema: "messages" carries the full transcript.
        if "messages" in payload:
            candidates = payload["messages"]
        elif "events" in payload:
            candidates = payload["events"]
        else:
            # Single-message shape -- the result is a transcript itself.
            candidates = [payload]
    elif isinstance(payload, list):
        candidates = payload

    for ev in candidates:
        if not isinstance(ev, dict):
            continue
        # tool_use can appear in `message.content[]` for assistant messages
        msg = ev.get("message") or ev
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    calls.append({
                        "name": block.get("name"),
                        "input": block.get("input", {}),
                    })
    return calls


def _summarize_tool_calls(calls: list[dict]) -> None:
    print(f"--- agent made {len(calls)} tool calls ---")
    for i, c in enumerate(calls):
        name = c["name"]
        inp = c["input"]
        if name == "Bash":
            cmd = inp.get("command", "")
            preview = cmd if len(cmd) <= 100 else cmd[:97] + "..."
            print(f"  [{i:02d}] Bash: {preview}")
        elif name in ("Write", "Edit", "Read"):
            print(f"  [{i:02d}] {name}: {inp.get('file_path', '?')}")
        else:
            print(f"  [{i:02d}] {name}: {json.dumps(inp)[:80]}")


def test_real_subagent_against_modal() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="evo-modal-agent-"))
    repo = _build_repo(workdir)

    try:
        provider_config = (
            "app_name=evo-live-agent,"
            "timeout_seconds=300,"
            "health_timeout_seconds=90.0"
        )
        _evo(
            ["init", "--target", "agent.py",
             "--benchmark", "python eval.py",
             "--gate", "python gate.py",
             "--metric", "max", "--host", "claude-code"],
            cwd=repo,
        )

        parent_id, parent_score = _bootstrap_parent(repo, provider_config)
        exp_id = _allocate_test_experiment(repo, parent_id, provider_config)
        brief = _build_brief(exp_id, parent_id, parent_score)

        print(f"\n--- BRIEF (USER message to subagent) ---")
        print(brief)
        print(f"--- end brief ---\n")

        payload = _spawn_claude(brief, repo)
        tool_calls = _extract_tool_calls(payload)
        _summarize_tool_calls(tool_calls)

        # ---- Verifications ----
        bash_calls = [c for c in tool_calls if c["name"] == "Bash"]
        bash_commands = [c["input"].get("command", "") for c in bash_calls]
        evo_write_or_edit = [
            cmd for cmd in bash_commands
            if " evo write " in f" {cmd} " or " evo edit " in f" {cmd} "
        ]
        evo_run = [cmd for cmd in bash_commands if " evo run " in f" {cmd} "]

        print(f"\n--- verification ---")
        print(f"    evo write/edit calls: {len(evo_write_or_edit)}")
        print(f"    evo run calls:        {len(evo_run)}")

        # 1. Agent must have used evo write or edit (not native Write/Edit).
        assert evo_write_or_edit, (
            f"agent did not use evo write or evo edit; bash commands were:\n"
            + "\n".join(f"  {c}" for c in bash_commands)
        )

        # 2. Every evo write/edit must include --exp-id <our_exp_id>.
        for cmd in evo_write_or_edit:
            assert "--exp-id" in cmd, (
                f"agent omitted --exp-id from evo command: {cmd}"
            )
            assert exp_id in cmd, (
                f"agent passed wrong --exp-id (expected {exp_id} in: {cmd})"
            )

        # 3. Native Write/Edit on the sandbox path = bug. Native ops on
        # local files are fine (e.g., the agent could Read a local note).
        for c in tool_calls:
            if c["name"] in ("Write", "Edit", "MultiEdit"):
                path = c["input"].get("file_path", "")
                assert "/workspace/" not in path, (
                    f"agent used native {c['name']} on sandbox path "
                    f"{path}; should have used evo write/edit"
                )

        # 4. evo run was called with the right exp_id.
        assert evo_run, "agent didn't call evo run"
        run_cmd = next(c for c in evo_run)
        assert exp_id in run_cmd, f"evo run with wrong exp_id: {run_cmd}"

        # 5. The experiment committed with score > parent.
        graph = json.loads(
            (repo / ".evo" / "run_0000" / "graph.json").read_text(encoding="utf-8")
        )
        node = graph["nodes"][exp_id]
        assert node["status"] == "committed", (
            f"experiment did not commit: {node}"
        )
        assert node["score"] > parent_score, (
            f"score {node['score']} did not beat parent {parent_score}"
        )
        print(f"    committed: {node['commit'][:12]} score={node['score']} "
              f"(parent={parent_score})")

    finally:
        print("\n--- backstop cleanup ---")
        _evo(["reset", "--yes"], cwd=repo, check=False)
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> None:
    _gate()
    print("=== Live: real claude subagent + Modal sandbox ===\n")
    test_real_subagent_against_modal()
    print("\nLIVE REMOTE AGENT MODAL OK")


if __name__ == "__main__":
    main()
