"""Claude Code cache "latest version" selection must be numeric-aware (#95).

`_latest_cache_dir` used a plain lexicographic `sorted()`, so `0.9.0` sorts
after `0.10.0` and the wrong cache dir was picked once a two-digit minor or
patch existed. That stages the hook binary into the wrong version and makes
`doctor` report a false "cache stale".
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))

from evo.host_install import claude_code


def _make_cache(config_dir: Path, versions: list[str]) -> None:
    root = config_dir / "plugins" / "cache" / claude_code._MARKETPLACE_NAME / "evo"
    for v in versions:
        (root / v).mkdir(parents=True, exist_ok=True)


def test_latest_cache_dir_is_numeric_aware(tmp_path: Path):
    _make_cache(tmp_path, ["0.9.0", "0.10.0", "0.11.0"])
    with patch.object(claude_code, "_claude_config_dir", return_value=tmp_path):
        latest = claude_code._latest_cache_dir()
    assert latest is not None
    assert latest.name == "0.11.0", f"picked {latest.name}, expected 0.11.0"


def test_latest_cache_dir_two_digit_patch(tmp_path: Path):
    _make_cache(tmp_path, ["0.8.9", "0.8.10"])
    with patch.object(claude_code, "_claude_config_dir", return_value=tmp_path):
        latest = claude_code._latest_cache_dir()
    assert latest is not None
    assert latest.name == "0.8.10", f"picked {latest.name}, expected 0.8.10"


def test_latest_cache_dir_none_when_empty(tmp_path: Path):
    with patch.object(claude_code, "_claude_config_dir", return_value=tmp_path):
        assert claude_code._latest_cache_dir() is None
