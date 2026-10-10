"""`evo budget` — cumulative spend rollup + warn-only cap (#104)."""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))

from evo import cli
from evo.core import (
    compute_spend,
    experiments_path,
    init_workspace,
    load_config,
)


def _init_ws(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@evo"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
    (root / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    init_workspace(root, target="t.py", benchmark="echo hi", metric="max", gate=None)


def _write_trace(root: Path, exp_id: str, attempt: int, task: str, cost) -> None:
    d = experiments_path(root) / exp_id / "attempts" / f"{attempt:03d}" / "traces"
    d.mkdir(parents=True, exist_ok=True)
    trace = {"experiment_id": exp_id, "task_id": task, "score": 1.0}
    if cost is not None:
        trace["cost"] = cost
    (d / f"task_{task}.json").write_text(json.dumps(trace), encoding="utf-8")


# --- compute_spend --------------------------------------------------------

def test_compute_spend_sums_usd_across_experiments_and_attempts(tmp_path):
    root = tmp_path.resolve()
    _init_ws(root)
    _write_trace(root, "exp_0000", 1, "0", {"usd": 0.10, "input_tokens": 100, "output_tokens": 20})
    _write_trace(root, "exp_0000", 1, "1", {"usd": 0.05, "input_tokens": 50, "output_tokens": 10})
    _write_trace(root, "exp_0000", 2, "0", {"usd": 0.20, "input_tokens": 200, "output_tokens": 40})
    _write_trace(root, "exp_0001", 1, "0", {"usd": 0.65, "input_tokens": 600, "output_tokens": 60})

    spend = compute_spend(root)
    assert round(spend["total_usd"], 2) == 1.00
    assert spend["input_tokens"] == 950
    assert spend["output_tokens"] == 130
    assert spend["per_experiment"]["exp_0000"] == 0.35
    assert spend["per_experiment"]["exp_0001"] == 0.65


def test_compute_spend_counts_unpriced_traces(tmp_path):
    root = tmp_path.resolve()
    _init_ws(root)
    _write_trace(root, "exp_0000", 1, "0", {"usd": 0.10})
    _write_trace(root, "exp_0000", 1, "1", {"input_tokens": 100})   # cost, no usd
    _write_trace(root, "exp_0000", 1, "2", None)                     # no cost at all

    spend = compute_spend(root)
    assert round(spend["total_usd"], 2) == 0.10
    assert spend["unpriced_traces"] == 1
    assert spend["priced_traces"] == 1


def test_compute_spend_empty_workspace_is_zero(tmp_path):
    root = tmp_path.resolve()
    _init_ws(root)
    spend = compute_spend(root)
    assert spend["total_usd"] == 0.0
    assert spend["trace_count"] == 0


# --- budget config field --------------------------------------------------

def test_config_set_budget(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    _init_ws(root)
    monkeypatch.chdir(root)
    cli.cmd_config_set(argparse.Namespace(field="budget", value="20"))
    assert load_config(root)["budget"] == 20.0


def test_config_set_budget_rejects_negative(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    _init_ws(root)
    monkeypatch.chdir(root)
    import pytest
    with pytest.raises(RuntimeError):
        cli.cmd_config_set(argparse.Namespace(field="budget", value="-5"))


def test_config_set_budget_clears_on_empty(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    _init_ws(root)
    monkeypatch.chdir(root)
    cli.cmd_config_set(argparse.Namespace(field="budget", value="20"))
    cli.cmd_config_set(argparse.Namespace(field="budget", value=""))
    assert load_config(root).get("budget") is None


# --- evo budget command ---------------------------------------------------

def test_cmd_budget_json(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    _init_ws(root)
    monkeypatch.chdir(root)
    cli.cmd_config_set(argparse.Namespace(field="budget", value="1.00"))
    _write_trace(root, "exp_0000", 1, "0", {"usd": 0.60})

    out = io.StringIO()
    with patch("sys.stdout", out):
        rc = cli.cmd_budget(argparse.Namespace(json=True))
    assert rc == 0
    payload = json.loads(out.getvalue())
    assert round(payload["spent_usd"], 2) == 0.60
    assert payload["cap_usd"] == 1.00
    assert round(payload["remaining_usd"], 2) == 0.40


# --- warn-only enforcement helper ----------------------------------------

def test_budget_warning_none_when_no_cap(tmp_path):
    root = tmp_path.resolve()
    _init_ws(root)
    _write_trace(root, "exp_0000", 1, "0", {"usd": 999.0})
    assert cli._budget_warning(root, load_config(root)) is None


def test_budget_warning_none_when_under_cap(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    _init_ws(root)
    monkeypatch.chdir(root)
    cli.cmd_config_set(argparse.Namespace(field="budget", value="10"))
    _write_trace(root, "exp_0000", 1, "0", {"usd": 3.0})
    assert cli._budget_warning(root, load_config(root)) is None


def test_budget_warning_fires_when_over_cap(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    _init_ws(root)
    monkeypatch.chdir(root)
    cli.cmd_config_set(argparse.Namespace(field="budget", value="10"))
    _write_trace(root, "exp_0000", 1, "0", {"usd": 11.0})
    msg = cli._budget_warning(root, load_config(root))
    assert msg is not None
    assert "budget cap" in msg.lower()
