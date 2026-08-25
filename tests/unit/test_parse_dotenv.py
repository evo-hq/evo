"""`parse_dotenv` must not corrupt non-ASCII UTF-8 in quoted values (#96).

The double-quoted branch used to do
    bytes(value[1:-1], "utf-8").decode("unicode_escape")
which reinterprets each UTF-8 byte as a Latin-1 code point, mangling any
multi-byte character (café -> caf\xc3\xa9). The fix must keep non-ASCII
intact while still honoring the common backslash escapes.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))

from evo.core import parse_dotenv


def test_double_quoted_non_ascii_is_preserved():
    assert parse_dotenv('X="café"')["X"] == "café"
    assert parse_dotenv('T="токен"')["T"] == "токен"
    assert parse_dotenv('E="a—b 🙂"')["E"] == "a—b 🙂"


def test_unquoted_non_ascii_is_preserved():
    assert parse_dotenv("Y=café")["Y"] == "café"


def test_double_quoted_common_escapes_still_work():
    # Source contains literal backslash-n / -t / -" / -\ inside the quotes.
    assert parse_dotenv(r'X="a\nb"')["X"] == "a\nb"
    assert parse_dotenv(r'X="a\tb"')["X"] == "a\tb"
    assert parse_dotenv(r'X="a\"b"')["X"] == 'a"b'
    assert parse_dotenv(r'X="a\\b"')["X"] == "a\\b"


def test_escaped_and_unicode_combined():
    assert parse_dotenv(r'X="café\nnext"')["X"] == "café\nnext"


def test_single_quoted_is_literal():
    # Single quotes do not process escapes.
    assert parse_dotenv(r"Z='a\nb'")["Z"] == r"a\nb"
