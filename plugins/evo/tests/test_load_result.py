"""Tests for evo.core.load_result and parse_score."""
from __future__ import annotations

from pathlib import Path

import pytest

from evo.core import load_result, parse_score


def test_uses_file_when_valid(tmp_path: Path) -> None:
    good = tmp_path / "result.json"
    good.write_text('{"score": 0.42, "tasks": {"0": 0.42}}', encoding="utf-8")
    score, parsed = load_result(good, "ignored stdout")
    assert score == 0.42
    assert parsed == {"score": 0.42, "tasks": {"0": 0.42}}


def test_uses_stdout_when_file_missing(tmp_path: Path) -> None:
    score, parsed = load_result(tmp_path / "absent.json", '{"score": 0.3}')
    assert score == 0.3
    assert parsed == {"score": 0.3}


def test_raises_when_file_empty(tmp_path: Path) -> None:
    empty = tmp_path / "result.json"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        load_result(empty, '{"score": 0.7}')


def test_empty_file_does_not_fall_through_to_stdout_noise(tmp_path: Path) -> None:
    empty = tmp_path / "result.json"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        load_result(empty, "Starting...\nscore: 0.99\n0.42\nDone.\n")


def test_raises_when_file_malformed(tmp_path: Path) -> None:
    bad = tmp_path / "result.json"
    bad.write_text("not valid json {{", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_result(bad, '{"score": 0.5}')


def test_raises_when_score_field_missing(tmp_path: Path) -> None:
    bad = tmp_path / "result.json"
    bad.write_text('{"not_score": 1, "tasks": {}}', encoding="utf-8")
    with pytest.raises(ValueError, match="missing 'score'"):
        load_result(bad, "0.7")


def test_raises_when_file_is_not_an_object(tmp_path: Path) -> None:
    bad = tmp_path / "result.json"
    bad.write_text("[0.5]", encoding="utf-8")
    with pytest.raises(ValueError, match="missing 'score'"):
        load_result(bad, '{"score": 0.9}')


def test_parse_score_accepts_single_json_object() -> None:
    score, parsed = parse_score('{"score": 0.42, "tasks": {"0": 0.42}}')
    assert score == 0.42
    assert parsed == {"score": 0.42, "tasks": {"0": 0.42}}


def test_parse_score_rejects_score_shaped_log_line() -> None:
    with pytest.raises(ValueError, match="not a single JSON object"):
        parse_score("Starting...\nscore: 0.99 (warmup)\nDone.\n")


def test_parse_score_rejects_bare_number() -> None:
    with pytest.raises(ValueError, match="not a single JSON object|missing 'score'"):
        parse_score("0.5\n")


def test_parse_score_rejects_json_without_score() -> None:
    with pytest.raises(ValueError, match="missing 'score'"):
        parse_score('{"result": "ok", "tasks": {}}')


def test_parse_score_rejects_extra_print_after_json() -> None:
    with pytest.raises(ValueError, match="not a single JSON object"):
        parse_score('{"score": 0.5}\n{"score": 0.99}\n')


def test_parse_score_rejects_empty_stdout() -> None:
    with pytest.raises(ValueError, match="empty"):
        parse_score("")
