"""Tests for evo.core.load_result and parse_score."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from evo.cli import _stop_dashboard
from evo.core import load_graph, load_result, parse_score


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


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
def test_rejects_non_finite_score(tmp_path: Path, raw: str) -> None:
    # json.dumps(float("nan")) writes a bare NaN, and json.loads reads it back.
    # A NaN baseline commits (nothing to compare against) and then every later
    # compare_scores() against it is False, so no experiment can ever commit.
    result = tmp_path / "result.json"
    result.write_text(f'{{"score": {raw}}}', encoding="utf-8")
    with pytest.raises(ValueError, match="not a finite number"):
        load_result(result, "")
    with pytest.raises(ValueError, match="not a finite number"):
        parse_score(f'{{"score": {raw}}}')


def _evo(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", "from evo.cli import main; import sys; sys.exit(main())", *args],
        cwd=root, check=False, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "plugins" / "evo" / "src")},
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: git holds files open on Windows")
@pytest.mark.parametrize(
    ("trace_scores", "expected"),
    [([0.5, "NaN", "Infinity"], 0.5), (["NaN", "-Infinity"], None)],
)
def test_failed_run_does_not_salvage_non_finite_trace_scores(
    tmp_path: Path, trace_scores: list, expected: float | None,
) -> None:
    # The benchmark score is rejected, so the attempt fails and evo averages the
    # per-task traces instead. That average lands on the failed node, which
    # /api/graph serves, so it has to be finite too.
    for cmd in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "T"],
                ["config", "commit.gpgsign", "false"]):
        subprocess.run(["git", *cmd], cwd=tmp_path, check=True)
    (tmp_path / "agent.py").write_text("# agent\n")
    lines = ["import os, pathlib", "t = pathlib.Path(os.environ['EVO_TRACES_DIR'])",
             "t.mkdir(parents=True, exist_ok=True)"]
    for i, raw in enumerate(trace_scores):
        lines.append(f"(t / 'task_{i}.json').write_text('{{\"task_id\": \"{i}\", \"score\": {raw}}}')")
    lines.append("pathlib.Path(os.environ['EVO_RESULT_PATH']).write_text('{\"score\": NaN}')")
    (tmp_path / "benchmark.py").write_text("\n".join(lines) + "\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)

    try:
        r = _evo(tmp_path, "init", "--target", "agent.py", "--benchmark", f"{sys.executable} benchmark.py",
                 "--metric", "max", "--host", "generic", "--per-exp-timeout", "1800")
        assert r.returncode == 0, r.stderr
        r = _evo(tmp_path, "new", "--parent", "root", "-m", "h")
        assert r.returncode == 0, r.stderr
        graph = load_graph(tmp_path)
        exp_id = next(nid for nid in graph["nodes"] if nid != "root")
        r = _evo(tmp_path, "run", exp_id)
        assert r.returncode == 1, r.stdout + r.stderr
        assert "not a finite number" in r.stdout

        node = load_graph(tmp_path)["nodes"][exp_id]
        assert node["status"] == "failed"
        assert node.get("score") == expected
        json.dumps(node, allow_nan=False)
    finally:
        # Stop the supervisor, not just its dashboard child: killing only the
        # child makes the supervisor respawn it on another port.
        _stop_dashboard(tmp_path)
