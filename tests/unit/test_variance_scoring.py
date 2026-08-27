"""Tests for variance-aware scoring aggregation (#4).

`aggregate_trial_scores` collapses N noisy benchmark-trial scores into one
comparable score. Pure function, no I/O.
"""
from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path

from evo.cli import persist_aggregate_result, run_extra_benchmark_trials
from evo.core import aggregate_trial_scores, load_result


class TestAggregateTrialScores(unittest.TestCase):
    def test_median_odd_count(self):
        agg, stats = aggregate_trial_scores([0.7, 0.9, 0.8], "median", "max")
        self.assertEqual(agg, 0.8)
        self.assertEqual(stats["method"], "median")
        self.assertEqual(stats["n"], 3)

    def test_median_resists_lucky_outlier(self):
        # The issue's core case: one lucky-high run shouldn't move the score.
        without = aggregate_trial_scores([0.50, 0.51, 0.49], "median", "max")[0]
        with_lucky = aggregate_trial_scores([0.50, 0.51, 0.49, 0.99], "median", "max")[0]
        # Median barely moves; a mean would jump toward the outlier.
        self.assertLess(abs(with_lucky - without), 0.02)

    def test_mean(self):
        agg, _ = aggregate_trial_scores([0.6, 0.8], "mean", "max")
        self.assertAlmostEqual(agg, 0.7)

    def test_worst_is_min_when_metric_max(self):
        agg, _ = aggregate_trial_scores([0.6, 0.9, 0.8], "worst", "max")
        self.assertEqual(agg, 0.6)

    def test_worst_is_max_when_metric_min(self):
        # For metric=min, lower is better, so worst-case is the highest score.
        agg, _ = aggregate_trial_scores([0.6, 0.9, 0.8], "worst", "min")
        self.assertEqual(agg, 0.9)

    def test_stats_payload(self):
        _, stats = aggregate_trial_scores([1.0, 2.0, 3.0], "mean", "max")
        self.assertEqual(stats["min"], 1.0)
        self.assertEqual(stats["max"], 3.0)
        self.assertEqual(stats["median"], 2.0)
        self.assertAlmostEqual(stats["mean"], 2.0)
        self.assertGreater(stats["stdev"], 0.0)

    def test_single_trial_zero_stdev(self):
        agg, stats = aggregate_trial_scores([0.42], "median", "max")
        self.assertEqual(agg, 0.42)
        self.assertEqual(stats["n"], 1)
        self.assertEqual(stats["stdev"], 0.0)

    def test_rejects_empty_scores(self):
        with self.assertRaises(ValueError):
            aggregate_trial_scores([], "median", "max")

    def test_rejects_unknown_method(self):
        with self.assertRaises(ValueError):
            aggregate_trial_scores([0.5], "p95", "max")

    def test_rejects_unknown_metric(self):
        with self.assertRaises(ValueError):
            aggregate_trial_scores([0.5], "worst", "sideways")


class _FakeExecutor:
    """Local-path fake: each stream() writes the next queued score to
    result_path (mimicking a benchmark that publishes result.json) and returns
    a stream-result object with the given exit_code/timed_out."""

    def __init__(self, result_path: Path, trials):
        self.result_path = result_path
        self.trials = list(trials)  # list of (score_or_None, exit_code, timed_out)
        self.stream_calls = 0
        self.result_existed_at_call = []

    def stream(self, cmd, **kwargs):
        self.result_existed_at_call.append(self.result_path.exists())
        score, exit_code, timed_out = self.trials[self.stream_calls]
        self.stream_calls += 1
        if score is not None:
            self.result_path.write_text(json.dumps({"score": score}))
        return types.SimpleNamespace(
            stdout="", exit_code=exit_code, timed_out=timed_out
        )


class TestRunExtraBenchmarkTrials(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)
        self.result_path = self.d / "result.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _call(self, executor, n_extra):
        return run_extra_benchmark_trials(
            executor,
            n_extra=n_extra,
            benchmark_cmd="python bench.py",
            run_cwd=self.d,
            env={},
            timeout=None,
            result_path=self.result_path,
            benchmark_log=self.d / "bench.log",
            benchmark_err=self.d / "bench_err.log",
        )

    def test_runs_n_extra_times_and_returns_scores(self):
        ex = _FakeExecutor(self.result_path, [(0.7, 0, False), (0.9, 0, False)])
        scores = self._call(ex, n_extra=2)
        self.assertEqual(scores, [0.7, 0.9])
        self.assertEqual(ex.stream_calls, 2)

    def test_clears_stale_result_before_each_trial(self):
        # Seed a stale result so trial 1 must clear it before streaming.
        self.result_path.write_text(json.dumps({"score": 0.1}))
        ex = _FakeExecutor(self.result_path, [(0.7, 0, False), (0.9, 0, False)])
        self._call(ex, n_extra=2)
        # result.json must not exist at the moment each trial streams.
        self.assertEqual(ex.result_existed_at_call, [False, False])

    def test_raises_on_nonzero_exit(self):
        ex = _FakeExecutor(self.result_path, [(None, 1, False)])
        with self.assertRaises(RuntimeError):
            self._call(ex, n_extra=1)

    def test_raises_on_timeout(self):
        ex = _FakeExecutor(self.result_path, [(None, 0, True)])
        with self.assertRaises(RuntimeError):
            self._call(ex, n_extra=1)


class TestPersistAggregateResult(unittest.TestCase):
    """The aggregate must be written back to result.json so a crash-recovery
    resume (which re-reads result.json without re-aggregating) is scored by the
    aggregate, not the last raw trial (Devin review, issue 1)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.rp = Path(self._tmp.name) / "result.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_overwrites_last_trial_score_with_aggregate(self):
        # Simulate the last remote trial having overwritten result.json.
        self.rp.write_text(json.dumps({"score": 0.99, "tasks": [{"id": "a"}]}))
        persist_aggregate_result(self.rp, {"score": 0.99, "tasks": [{"id": "a"}]}, 0.5)
        score, parsed = load_result(self.rp, "")
        self.assertEqual(score, 0.5)  # aggregate, not the 0.99 raw trial
        self.assertEqual(parsed["tasks"], [{"id": "a"}])  # other fields preserved

    def test_handles_none_parsed(self):
        persist_aggregate_result(self.rp, None, 0.42)
        score, _ = load_result(self.rp, "")
        self.assertEqual(score, 0.42)


class _RemoteFakeExecutor:
    """Remote-path fake: stream() returns the queued (exit_code, timed_out);
    file_exists/read_bytes/fetch_dir/run are no-ops so _fetch_remote_artifacts
    is exercised without a real sandbox."""

    def __init__(self, trials):
        self.trials = list(trials)
        self.stream_calls = 0

    def stream(self, cmd, **kwargs):
        exit_code, timed_out = self.trials[self.stream_calls]
        self.stream_calls += 1
        return types.SimpleNamespace(stdout="", exit_code=exit_code, timed_out=timed_out)

    def run(self, cmd, **kwargs):
        return types.SimpleNamespace(stdout="", exit_code=0, timed_out=False)

    def file_exists(self, path):
        return False

    def read_bytes(self, path):
        return b""

    def fetch_dir(self, remote, local):
        return None


class TestTrialLoopRemoteInfraClassification(unittest.TestCase):
    """A remote infra failure in an extra trial must be classified as
    remote_infra_failure (like the primary run), not benchmark_exit (issue 2)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)
        self.log = self.d / "bench.log"
        self.log.write_text("")

    def tearDown(self):
        self._tmp.cleanup()

    def _call(self, executor):
        return run_extra_benchmark_trials(
            executor, n_extra=1, benchmark_cmd="python bench.py",
            run_cwd=self.d, env={}, timeout=None,
            result_path=self.d / "result.json",
            benchmark_log=self.log, benchmark_err=self.d / "bench_err.log",
            remote=True, sandbox_result_path="/sb/result.json",
            sandbox_traces_dir="/sb/traces", traces_dir=self.d / "traces",
        )

    def test_infra_journal_raises_remote_infra_failure(self):
        # Journal companion marks the run as an infra failure.
        self.log.with_name(self.log.name + ".remote.json").write_text(
            json.dumps({"state": "failed_infra", "error": "sandbox died"})
        )
        ex = _RemoteFakeExecutor([(1, False)])
        with self.assertRaises(RuntimeError) as ctx:
            self._call(ex)
        self.assertIn("remote_infra_failure", str(ctx.exception))

    def test_plain_nonzero_exit_raises_benchmark_exit(self):
        # No infra journal -> ordinary benchmark failure.
        ex = _RemoteFakeExecutor([(3, False)])
        with self.assertRaises(RuntimeError) as ctx:
            self._call(ex)
        self.assertIn("benchmark_exit_3", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
