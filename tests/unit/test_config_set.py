"""Tests for `evo config set` field handling.

Covers the basic fields plus the three setters added later:
max-attempts, gate, frontier-strategy.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from evo.cli import cmd_config_set
from evo.core import init_workspace, load_config


def _args(field: str, value: str) -> argparse.Namespace:
    return argparse.Namespace(field=field, value=value)


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@evo"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
    (root / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)


class TestConfigSet(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # Resolve to defeat /var -> /private/var symlink on macOS so chdir-based
        # repo_root() comparisons line up with init_workspace's path.
        self.root = Path(self._tmp.name).resolve()
        _init_git_repo(self.root)
        init_workspace(
            self.root,
            target="t.py",
            benchmark="python bench.py",
            metric="max",
            gate=None,
        )
        # cmd_config_set uses repo_root() which walks up from cwd.
        self._old_cwd = Path.cwd()
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    # --- existing fields (smoke) ----------------------------------------

    def test_set_project_name(self):
        cmd_config_set(_args("project-name", "demo"))
        self.assertEqual(load_config(self.root)["project_name"], "demo")

    def test_set_metric_validates(self):
        with self.assertRaises(RuntimeError):
            cmd_config_set(_args("metric", "bogus"))

    # --- max-attempts ---------------------------------------------------

    def test_set_max_attempts(self):
        cmd_config_set(_args("max-attempts", "7"))
        self.assertEqual(load_config(self.root)["max_attempts"], 7)

    def test_max_attempts_rejects_zero(self):
        with self.assertRaises(RuntimeError):
            cmd_config_set(_args("max-attempts", "0"))

    def test_max_attempts_rejects_non_integer(self):
        with self.assertRaises(RuntimeError):
            cmd_config_set(_args("max-attempts", "abc"))

    # --- gate -----------------------------------------------------------

    def test_set_gate(self):
        cmd_config_set(_args("gate", "pytest -q"))
        self.assertEqual(load_config(self.root)["gate"], "pytest -q")

    def test_clear_gate_with_empty_string(self):
        cmd_config_set(_args("gate", "pytest -q"))
        cmd_config_set(_args("gate", ""))
        self.assertIsNone(load_config(self.root)["gate"])

    # --- frontier-strategy ---------------------------------------------

    def test_set_frontier_strategy_kind_only(self):
        cmd_config_set(_args("frontier-strategy", "epsilon_greedy"))
        fs = load_config(self.root)["frontier_strategy"]
        self.assertEqual(fs["kind"], "epsilon_greedy")
        # Defaults filled in by validate_frontier_strategy.
        self.assertIn("epsilon", fs["params"])

    def test_set_frontier_strategy_with_json_params(self):
        cmd_config_set(_args(
            "frontier-strategy",
            '{"kind": "top_k", "params": {"k": 4}}',
        ))
        fs = load_config(self.root)["frontier_strategy"]
        self.assertEqual(fs["kind"], "top_k")
        self.assertEqual(fs["params"]["k"], 4)

    def test_frontier_strategy_rejects_unknown_kind(self):
        with self.assertRaises(RuntimeError) as ctx:
            cmd_config_set(_args("frontier-strategy", "no_such_kind"))
        self.assertIn("unknown frontier_strategy.kind", str(ctx.exception))

    def test_frontier_strategy_rejects_invalid_json(self):
        with self.assertRaises(RuntimeError) as ctx:
            cmd_config_set(_args("frontier-strategy", "{not valid json"))
        self.assertIn("valid JSON", str(ctx.exception))

    # --- run-behavior defaults (on/off) --------------------------------

    def test_set_default_autonomous_on(self):
        cmd_config_set(_args("default-autonomous", "on"))
        self.assertIs(load_config(self.root)["default_autonomous"], True)

    def test_set_default_subagents_only_on(self):
        cmd_config_set(_args("default-subagents-only", "on"))
        self.assertIs(load_config(self.root)["default_subagents_only"], True)

    def test_default_flags_accept_off_and_aliases(self):
        for value in ("off", "false", "no", "0"):
            cmd_config_set(_args("default-autonomous", value))
            self.assertIs(load_config(self.root)["default_autonomous"], False)
        for value in ("on", "true", "yes", "1"):
            cmd_config_set(_args("default-autonomous", value))
            self.assertIs(load_config(self.root)["default_autonomous"], True)

    def test_default_flags_reject_non_boolean(self):
        with self.assertRaises(RuntimeError) as ctx:
            cmd_config_set(_args("default-subagents-only", "maybe"))
        self.assertIn("on/off", str(ctx.exception))

    # --- variance-aware scoring (#4) ------------------------------------

    def test_set_bench_repeat(self):
        cmd_config_set(_args("bench-repeat", "5"))
        self.assertEqual(load_config(self.root)["bench_repeat"], 5)

    def test_bench_repeat_rejects_zero(self):
        with self.assertRaises(RuntimeError):
            cmd_config_set(_args("bench-repeat", "0"))

    def test_bench_repeat_rejects_non_integer(self):
        with self.assertRaises(RuntimeError):
            cmd_config_set(_args("bench-repeat", "lots"))

    def test_set_score_aggregation(self):
        for method in ("median", "mean", "worst"):
            cmd_config_set(_args("score-aggregation", method))
            self.assertEqual(load_config(self.root)["score_aggregation"], method)

    def test_score_aggregation_rejects_unknown(self):
        with self.assertRaises(RuntimeError) as ctx:
            cmd_config_set(_args("score-aggregation", "p95"))
        self.assertIn("median", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
