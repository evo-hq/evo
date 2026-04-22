"""Tests for the Python inline_instrumentation helper.

The helper is meant to be pasted into a benchmark script and used without
the SDK. Its module-global `_SCORES` / `_TASK_META` state means we cannot
re-import it repeatedly in-process for clean tests, so each case runs a
small driver script in a subprocess that imports the helper, calls it,
and lets us inspect stdout + the traces dir.

Run from `plugins/evo/` with the plugin venv:

    .venv/bin/python -m unittest tests.test_inline_instrumentation -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HELPER_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills" / "discover" / "references" / "inline_instrumentation.py"
)


_DRIVER_PREAMBLE = textwrap.dedent(f"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("inline_instr", r"{HELPER_PATH}")
    inline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inline)
""")


def _run_driver(body: str, traces_dir: Path, exp_id: str = "exp-inline") -> tuple[str, str]:
    """Exec a driver script that imports the inline helper then runs body."""
    # Both the preamble and body must sit at zero indentation so Python can
    # parse them as top-level statements.
    script = _DRIVER_PREAMBLE + textwrap.dedent(body)
    env = os.environ.copy()
    env["EVO_TRACES_DIR"] = str(traces_dir)
    env["EVO_EXPERIMENT_ID"] = exp_id
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=env, capture_output=True, text=True, timeout=15,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"driver crashed: {proc.stderr}")
    return proc.stdout, proc.stderr


class TestInlineInstrumentation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.traces = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_emits_score_and_tasks(self):
        stdout, _ = _run_driver(
            """
            inline.log_task("t1", 0.8)
            inline.log_task("t2", 0.4)
            inline.write_result()
            """,
            self.traces,
        )
        result = json.loads(stdout)
        self.assertAlmostEqual(result["score"], 0.6, places=4)
        self.assertEqual(result["tasks"], {"t1": 0.8, "t2": 0.4})

    def test_tasks_meta_included_when_direction_given(self):
        stdout, _ = _run_driver(
            """
            inline.log_task("accuracy", 0.91, direction="max")
            inline.log_task("latency_ms", 140.0, direction="min")
            inline.log_task("throughput", 12.5)  # no direction -> not in meta
            inline.write_result(score=0.5)
            """,
            self.traces,
        )
        result = json.loads(stdout)
        self.assertIn("tasks_meta", result)
        self.assertEqual(
            result["tasks_meta"],
            {
                "accuracy": {"direction": "max"},
                "latency_ms": {"direction": "min"},
            },
        )
        # Trace files written for every task...
        files = sorted(p.name for p in self.traces.iterdir())
        self.assertEqual(
            files, ["task_accuracy.json", "task_latency_ms.json", "task_throughput.json"]
        )
        # ...and the per-task trace carries `direction` only when provided.
        lat = json.loads((self.traces / "task_latency_ms.json").read_text())
        self.assertEqual(lat["direction"], "min")
        thr = json.loads((self.traces / "task_throughput.json").read_text())
        self.assertNotIn("direction", thr)

    def test_tasks_meta_omitted_when_all_tasks_use_default_direction(self):
        stdout, _ = _run_driver(
            """
            inline.log_task("t1", 0.5)
            inline.log_task("t2", 0.7)
            inline.write_result()
            """,
            self.traces,
        )
        result = json.loads(stdout)
        self.assertNotIn("tasks_meta", result)

    def test_invalid_direction_raises(self):
        proc = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(f"""
                import importlib.util
                spec = importlib.util.spec_from_file_location("inline_instr", r"{HELPER_PATH}")
                inline = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(inline)
                try:
                    inline.log_task("t1", 0.5, direction="bogus")
                except ValueError:
                    print("raised")
            """)],
            env={**os.environ,
                 "EVO_TRACES_DIR": str(self.traces),
                 "EVO_EXPERIMENT_ID": "exp-x"},
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(proc.stdout.strip(), "raised", proc.stderr)


if __name__ == "__main__":
    unittest.main()
