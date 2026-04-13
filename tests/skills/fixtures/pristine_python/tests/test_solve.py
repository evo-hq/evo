"""Minimal smoke test. Must keep passing during optimization."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.solve import solve


def test_returns_string():
    assert isinstance(solve("what is 3 plus 4?"), str)


def test_addition_still_works():
    assert solve("what is 3 plus 4?") == "7"
