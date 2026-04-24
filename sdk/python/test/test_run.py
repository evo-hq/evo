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
def tmp_traces_dir():
    with tempfile.TemporaryDirectory(prefix="evo-agent-test-") as d:
        prev_traces = os.environ.get("EVO_TRACES_DIR")
        prev_exp = os.environ.get("EVO_EXPERIMENT_ID")
        os.environ["EVO_TRACES_DIR"] = d
        os.environ["EVO_EXPERIMENT_ID"] = "exp-123"
        try:
            yield Path(d)
        finally:
            if prev_traces is None:
                os.environ.pop("EVO_TRACES_DIR", None)
            else:
                os.environ["EVO_TRACES_DIR"] = prev_traces
            if prev_exp is None:
                os.environ.pop("EVO_EXPERIMENT_ID", None)
            else:
                os.environ["EVO_EXPERIMENT_ID"] = prev_exp


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
        assert set(files) == {"task_0.json", "task_1.json", "result.json"}, files

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
