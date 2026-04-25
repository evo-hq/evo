"""Python SDK tests. Mirrors sdk/node/test/run.test.js.

Run: `python3 sdk/python/test/test_run.py`
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "sdk" / "python" / "src"))

from evo_agent import Gate, Run  # noqa: E402


@contextmanager
def tmp_traces_dir(*, set_result_path: bool = False):
    """Yield a Path to a temp dir; sets EVO_TRACES_DIR/EVO_EXPERIMENT_ID and
    optionally EVO_RESULT_PATH. Restores prior env on exit.
    """
    with tempfile.TemporaryDirectory(prefix="evo-agent-test-") as d:
        prev = {
            "EVO_TRACES_DIR": os.environ.get("EVO_TRACES_DIR"),
            "EVO_EXPERIMENT_ID": os.environ.get("EVO_EXPERIMENT_ID"),
            "EVO_RESULT_PATH": os.environ.get("EVO_RESULT_PATH"),
        }
        os.environ["EVO_TRACES_DIR"] = d
        os.environ["EVO_EXPERIMENT_ID"] = "exp-123"
        if set_result_path:
            os.environ["EVO_RESULT_PATH"] = str(Path(d) / "result.json")
        else:
            os.environ.pop("EVO_RESULT_PATH", None)
        try:
            yield Path(d)
        finally:
            for k, v in prev.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


@contextmanager
def capture_stdout():
    buf = io.StringIO()
    original = sys.stdout
    sys.stdout = buf
    try:
        yield buf
    finally:
        sys.stdout = original


def test_run_writes_trace_files_and_emits_score_json() -> None:
    with tmp_traces_dir() as traces_dir, capture_stdout() as buf:
        # LocalBackend captures sys.stdout at Run() construction, so the
        # stdout redirect must wrap the whole lifecycle.
        run = Run()
        run.log("0", "starting")
        run.log("0", {"role": "user", "content": "hi"})
        run.report("0", score=1.0, summary="ok")
        run.report("1", score=0.0, failure_reason="bad")
        result = run.finish()
        emitted = json.loads(buf.getvalue())

        assert result["score"] == 0.5, result
        assert result["tasks"] == {"0": 1.0, "1": 0.0}, result["tasks"]
        assert emitted["score"] == 0.5, emitted

        files = sorted(p.name for p in traces_dir.iterdir())
        assert files == ["task_0.json", "task_1.json"], files

        t0 = json.loads((traces_dir / "task_0.json").read_text())
        assert t0["experiment_id"] == "exp-123"
        assert t0["status"] == "passed"
        assert t0["score"] == 1.0
        assert t0["log"] == ["starting", {"role": "user", "content": "hi"}]

        t1 = json.loads((traces_dir / "task_1.json").read_text())
        assert t1["status"] == "failed"
        assert t1["failure_reason"] == "bad"


def test_run_mean_score_when_finish_not_given_explicit_score() -> None:
    with tmp_traces_dir(), capture_stdout():
        run = Run()
        run.report("a", score=0.8)
        run.report("b", score=0.2)
        result = run.finish()
        assert result["score"] == 0.5, result


def test_run_respects_explicit_finish_score() -> None:
    with tmp_traces_dir(), capture_stdout():
        run = Run()
        run.report("a", score=1.0)
        result = run.finish(score=0.42)
        assert result["score"] == 0.42, result


def test_run_finish_idempotent() -> None:
    with tmp_traces_dir(), capture_stdout():
        run = Run()
        run.report("a", score=1.0)
        run.finish()
        second = run.finish()
        # Second call is a no-op and returns an empty dict.
        assert second == {}, second


def test_run_direction_propagates_to_tasks_meta_and_traces() -> None:
    with tmp_traces_dir() as traces_dir, capture_stdout():
        run = Run()
        run.report("accuracy", score=0.9, direction="max")
        run.report("latency_ms", score=140.0, direction="min")
        run.report("throughput", score=12.5)  # no direction -> no meta entry
        result = run.finish(score=0.5)  # explicit avoids mean over mixed-scale values

        assert result["tasks_meta"] == {
            "accuracy": {"direction": "max"},
            "latency_ms": {"direction": "min"},
        }, result.get("tasks_meta")

        # Per-task trace carries direction where it was given.
        lat = json.loads((traces_dir / "task_latency_ms.json").read_text())
        assert lat["direction"] == "min", lat
        t = json.loads((traces_dir / "task_throughput.json").read_text())
        assert "direction" not in t, t


def test_run_direction_rejects_invalid_value() -> None:
    with tmp_traces_dir(), capture_stdout():
        run = Run()
        try:
            run.report("t", score=1.0, direction="bogus")
        except ValueError:
            return
        raise AssertionError("expected ValueError for invalid direction")


def test_run_omits_tasks_meta_when_no_directions_given() -> None:
    with tmp_traces_dir(), capture_stdout():
        run = Run()
        run.report("a", score=0.5)
        result = run.finish()
        assert "tasks_meta" not in result, result


def test_run_writes_result_file_when_evo_result_path_set() -> None:
    """New channel: EVO_RESULT_PATH set -> result lands in file, NOT stdout."""
    with tmp_traces_dir(set_result_path=True) as traces_dir, capture_stdout() as buf:
        run = Run()
        run.report("0", score=1.0)
        run.report("1", score=0.0)
        run.finish()

        result_path = Path(os.environ["EVO_RESULT_PATH"])
        assert result_path.exists(), "expected result file to be written"
        emitted = json.loads(result_path.read_text(encoding="utf-8"))
        assert emitted["score"] == 0.5, emitted
        assert emitted["tasks"] == {"0": 1.0, "1": 0.0}, emitted

        # Stdout MUST be empty -- the whole point is freeing it for user output.
        assert buf.getvalue() == "", f"unexpected stdout: {buf.getvalue()!r}"

        # No leftover .tmp files (atomic rename).
        leftovers = [p.name for p in result_path.parent.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == [], f"leftover tmp files: {leftovers}"


def test_run_raises_when_result_file_already_exists() -> None:
    with tmp_traces_dir(set_result_path=True) as traces_dir, capture_stdout():
        result_path = Path(os.environ["EVO_RESULT_PATH"])
        # Pre-create the file as if a prior writer published.
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text('{"score": 0.0}', encoding="utf-8")

        run = Run()
        run.report("0", score=0.5555)
        try:
            run.finish()
        except RuntimeError as e:
            assert "already exists" in str(e), e
            return
        raise AssertionError("Expected RuntimeError on duplicate write")


def test_run_falls_back_to_stdout_when_evo_result_path_unset() -> None:
    """Backwards compat: no env var -> print to stdout (legacy CLI path)."""
    with tmp_traces_dir(set_result_path=False) as traces_dir, capture_stdout() as buf:
        run = Run()
        run.report("0", score=0.7)
        run.finish()
        emitted = json.loads(buf.getvalue())
        assert emitted["score"] == 0.7, emitted
        # And no result.json should appear next to traces (env var was unset).
        assert not (traces_dir / "result.json").exists()


def test_gate_check_accepts_score_or_explicit_passed() -> None:
    g = Gate()
    g.check("a", score=0.8)
    g.check("b", score=0.3)
    g.check("c", passed=True, detail="manual")
    assert len(g._checks) == 3
    assert g._checks[0]["passed"] is True
    assert g._checks[1]["passed"] is False
    assert g._checks[2]["passed"] is True
    assert g._checks[2]["detail"] == "manual"


def test_gate_check_requires_score_or_passed() -> None:
    g = Gate()
    try:
        g.check("a")
    except ValueError:
        return
    raise AssertionError("expected ValueError when neither score nor passed given")


TESTS = [fn for name, fn in globals().items() if name.startswith("test_") and callable(fn)]


def main() -> int:
    failed = 0
    for fn in TESTS:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
