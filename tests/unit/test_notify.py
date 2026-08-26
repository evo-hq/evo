"""Webhook notifications for autoresearch events (#106)."""
from __future__ import annotations

import argparse
import io
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))

from evo import cli, notify
from evo.core import init_workspace, load_config


def _init_ws(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@evo"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
    (root / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    init_workspace(root, target="t.py", benchmark="echo hi", metric="max", gate=None)


# --- payload ---------------------------------------------------------------

def test_payload_carries_slack_and_discord_keys():
    p = notify.build_payload("new_best", "hello", {"exp_id": "exp_0001"})
    assert p["text"] == "hello"       # Slack incoming-webhook key
    assert p["content"] == "hello"    # Discord webhook key
    assert p["event"] == "new_best"
    assert p["source"] == "evo"
    assert p["data"]["exp_id"] == "exp_0001"


# --- send_notification (never raises) -------------------------------------

def test_send_posts_json_and_returns_true_on_2xx():
    resp = MagicMock(status_code=200)
    with patch.object(notify, "requests") as rq:
        rq.post.return_value = resp
        ok = notify.send_notification("https://hooks.example/x", "test", "hi")
    assert ok is True
    args, kwargs = rq.post.call_args
    assert args[0] == "https://hooks.example/x"
    assert kwargs["json"]["text"] == "hi"


def test_send_returns_false_on_non_2xx():
    with patch.object(notify, "requests") as rq:
        rq.post.return_value = MagicMock(status_code=500)
        assert notify.send_notification("https://hooks.example/x", "t", "hi") is False


def test_send_swallows_exceptions_and_returns_false():
    with patch.object(notify, "requests") as rq:
        rq.post.side_effect = Exception("network down")
        assert notify.send_notification("https://hooks.example/x", "t", "hi") is False


# --- config field ----------------------------------------------------------

def test_config_set_notify_webhook(tmp_path, monkeypatch):
    root = tmp_path.resolve(); _init_ws(root); monkeypatch.chdir(root)
    cli.cmd_config_set(argparse.Namespace(field="notify-webhook", value="https://hooks.slack.com/services/x"))
    assert load_config(root)["notify_webhook"] == "https://hooks.slack.com/services/x"


def test_config_set_notify_webhook_rejects_non_url(tmp_path, monkeypatch):
    root = tmp_path.resolve(); _init_ws(root); monkeypatch.chdir(root)
    import pytest
    with pytest.raises(RuntimeError):
        cli.cmd_config_set(argparse.Namespace(field="notify-webhook", value="not-a-url"))


def test_config_set_notify_webhook_clears_on_empty(tmp_path, monkeypatch):
    root = tmp_path.resolve(); _init_ws(root); monkeypatch.chdir(root)
    cli.cmd_config_set(argparse.Namespace(field="notify-webhook", value="https://hooks.example/x"))
    cli.cmd_config_set(argparse.Namespace(field="notify-webhook", value=""))
    assert load_config(root).get("notify_webhook") is None


# --- _maybe_notify gate ----------------------------------------------------

def test_maybe_notify_noop_without_webhook(tmp_path):
    root = tmp_path.resolve(); _init_ws(root)
    with patch.object(notify, "send_notification") as send:
        result = cli._maybe_notify(root, load_config(root), "new_best", "msg")
    assert result is False
    send.assert_not_called()


def test_maybe_notify_sends_when_webhook_set(tmp_path, monkeypatch):
    root = tmp_path.resolve(); _init_ws(root); monkeypatch.chdir(root)
    cli.cmd_config_set(argparse.Namespace(field="notify-webhook", value="https://hooks.example/x"))
    with patch.object(notify, "send_notification", return_value=True) as send:
        result = cli._maybe_notify(root, load_config(root), "new_best", "msg", {"k": "v"})
    assert result is True
    send.assert_called_once()
    assert send.call_args[0][0] == "https://hooks.example/x"


# --- evo notify test -------------------------------------------------------

def test_cmd_notify_test_posts(tmp_path, monkeypatch):
    root = tmp_path.resolve(); _init_ws(root); monkeypatch.chdir(root)
    cli.cmd_config_set(argparse.Namespace(field="notify-webhook", value="https://hooks.example/x"))
    with patch.object(notify, "send_notification", return_value=True) as send:
        rc = cli.cmd_notify(argparse.Namespace(notify_action="test"))
    assert rc == 0
    send.assert_called_once()


def test_cmd_notify_test_errors_without_webhook(tmp_path, monkeypatch):
    root = tmp_path.resolve(); _init_ws(root); monkeypatch.chdir(root)
    err = io.StringIO()
    with patch("sys.stderr", err):
        rc = cli.cmd_notify(argparse.Namespace(notify_action="test"))
    assert rc != 0
    assert "notify-webhook" in err.getvalue()
