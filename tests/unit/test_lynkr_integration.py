"""Tests for Lynkr tier-routing integration."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from evo.core import resolve_runtime_env, atomic_write_json, config_path


def test_lynkr_config_round_trip(tmp_path):
    """Lynkr configuration persists and loads correctly."""
    root = tmp_path
    cfg_path = root / ".evo" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    config = {
        "runtime_env": {
            "lynkr": {
                "enabled": True,
                "url": "http://localhost:8081",
                "auto_start": True,
                "lynkr_path": "/home/user/lynkr",
            }
        }
    }

    atomic_write_json(cfg_path, config)
    loaded = json.loads(cfg_path.read_text())

    assert loaded["runtime_env"]["lynkr"]["enabled"] is True
    assert loaded["runtime_env"]["lynkr"]["url"] == "http://localhost:8081"
    assert loaded["runtime_env"]["lynkr"]["auto_start"] is True
    assert loaded["runtime_env"]["lynkr"]["lynkr_path"] == "/home/user/lynkr"


def test_lynkr_env_injection_enabled(tmp_path):
    """When Lynkr is enabled, ANTHROPIC_BASE_URL and OPENAI_BASE_URL are injected."""
    root = tmp_path
    cfg_path = root / ".evo" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    config = {
        "runtime_env": {
            "inherit_shell": False,
            "lynkr": {
                "enabled": True,
                "url": "http://localhost:9000",
            }
        }
    }

    atomic_write_json(cfg_path, config)
    env = resolve_runtime_env(root, config)

    assert env["ANTHROPIC_BASE_URL"] == "http://localhost:9000/v1"
    assert env["OPENAI_BASE_URL"] == "http://localhost:9000/v1"
    assert env["LYNKR_VERIFY_ESCALATE"] == "false"
    assert env["LYNKR_CLIENT_HINT"] == "evo-experiment"


def test_lynkr_env_injection_disabled(tmp_path):
    """When Lynkr is disabled, no Lynkr env vars are injected."""
    root = tmp_path
    cfg_path = root / ".evo" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    config = {
        "runtime_env": {
            "inherit_shell": False,
            "lynkr": {
                "enabled": False,
            }
        }
    }

    atomic_write_json(cfg_path, config)
    env = resolve_runtime_env(root, config)

    assert "ANTHROPIC_BASE_URL" not in env
    assert "OPENAI_BASE_URL" not in env
    assert "LYNKR_VERIFY_ESCALATE" not in env


def test_lynkr_env_injection_no_config(tmp_path):
    """When no Lynkr config exists, no env vars are injected."""
    root = tmp_path
    cfg_path = root / ".evo" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    config = {
        "runtime_env": {
            "inherit_shell": False,
        }
    }

    atomic_write_json(cfg_path, config)
    env = resolve_runtime_env(root, config)

    assert "ANTHROPIC_BASE_URL" not in env
    assert "OPENAI_BASE_URL" not in env


def test_lynkr_inherits_api_keys_from_shell(tmp_path):
    """Lynkr env injection includes API keys from orchestrator environment."""
    root = tmp_path
    cfg_path = root / ".evo" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    config = {
        "runtime_env": {
            "inherit_shell": True,  # This causes os.environ to be inherited
            "lynkr": {
                "enabled": True,
                "url": "http://localhost:8081",
            }
        }
    }

    with patch.dict(os.environ, {
        "ANTHROPIC_API_KEY": "sk-ant-test-123",
        "MOONSHOT_API_KEY": "sk-moon-456",
    }):
        atomic_write_json(cfg_path, config)
        env = resolve_runtime_env(root, config)

        # Lynkr URLs injected
        assert env["ANTHROPIC_BASE_URL"] == "http://localhost:8081/v1"
        assert env["OPENAI_BASE_URL"] == "http://localhost:8081/v1"

        # API keys inherited from shell
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-test-123"
        assert env["MOONSHOT_API_KEY"] == "sk-moon-456"


def test_lynkr_custom_url(tmp_path):
    """Lynkr respects custom URL configuration."""
    root = tmp_path
    cfg_path = root / ".evo" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    config = {
        "runtime_env": {
            "inherit_shell": False,
            "lynkr": {
                "enabled": True,
                "url": "http://192.168.1.100:5000",
            }
        }
    }

    atomic_write_json(cfg_path, config)
    env = resolve_runtime_env(root, config)

    assert env["ANTHROPIC_BASE_URL"] == "http://192.168.1.100:5000/v1"
    assert env["OPENAI_BASE_URL"] == "http://192.168.1.100:5000/v1"


@patch("evo.cli._lynkr_is_healthy")
def test_ensure_lynkr_running_already_running(mock_healthy, tmp_path):
    """_ensure_lynkr_running does nothing when Lynkr is already healthy."""
    from evo.cli import _ensure_lynkr_running

    mock_healthy.return_value = True
    config = {
        "runtime_env": {
            "lynkr": {
                "enabled": True,
                "auto_start": True,
                "url": "http://localhost:8081",
                "lynkr_path": "/fake/path",
            }
        }
    }

    # Should not raise, should not attempt to start
    _ensure_lynkr_running(tmp_path, config)
    mock_healthy.assert_called_once_with("http://localhost:8081")


@patch("evo.cli._lynkr_is_healthy")
def test_ensure_lynkr_running_disabled(mock_healthy, tmp_path):
    """_ensure_lynkr_running does nothing when Lynkr is disabled."""
    from evo.cli import _ensure_lynkr_running

    config = {
        "runtime_env": {
            "lynkr": {
                "enabled": False,
            }
        }
    }

    _ensure_lynkr_running(tmp_path, config)
    mock_healthy.assert_not_called()


@patch("evo.cli._lynkr_is_healthy")
def test_ensure_lynkr_running_auto_start_off(mock_healthy, tmp_path):
    """_ensure_lynkr_running does nothing when auto_start=false."""
    from evo.cli import _ensure_lynkr_running

    mock_healthy.return_value = False
    config = {
        "runtime_env": {
            "lynkr": {
                "enabled": True,
                "auto_start": False,
            }
        }
    }

    _ensure_lynkr_running(tmp_path, config)
    mock_healthy.assert_not_called()
