"""`evo grep` must actually use ripgrep when it is available (#94).

The `rg`-availability test was `(which.exit_code or 1) == 0`, which is False
even when `rg` is present (0 is falsy, so `0 or 1` -> 1, and `1 == 0` is
False). The tool therefore always fell back to `grep`. These tests assert
that a present `rg` is selected and an absent one falls back.
"""
from __future__ import annotations

import argparse
import io
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))

from evo import cli


class _Result:
    def __init__(self, exit_code, stdout="", stderr=""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class _FakeExecutor:
    """Records commands; answers `which rg` per `rg_present`."""

    def __init__(self, rg_present: bool):
        self._rg_present = rg_present
        self.commands: list[list[str]] = []

    def run(self, cmd, cwd=None, env=None, timeout=None):
        self.commands.append(cmd)
        if cmd[:2] == ["which", "rg"]:
            return _Result(0, "/usr/bin/rg\n") if self._rg_present else _Result(1, "")
        return _Result(0, "match\n")


def _run_grep(rg_present: bool) -> _FakeExecutor:
    executor = _FakeExecutor(rg_present)

    @contextmanager
    def _ctx():
        yield executor

    node = {"worktree": "/tmp/wt"}
    with patch.object(cli, "_open_workspace_executor",
                      return_value=(Path("/tmp"), node, _ctx())):
        args = argparse.Namespace(path="/tmp/wt", pattern="foo")
        with patch("sys.stdout", io.StringIO()):
            cli.cmd_ws_grep(args)
    return executor


def test_uses_ripgrep_when_available():
    executor = _run_grep(rg_present=True)
    search_cmd = executor.commands[-1]
    assert search_cmd[0] == "rg", f"expected rg, got {search_cmd!r}"


def test_falls_back_to_grep_when_rg_absent():
    executor = _run_grep(rg_present=False)
    search_cmd = executor.commands[-1]
    assert search_cmd[0] == "grep", f"expected grep, got {search_cmd!r}"
