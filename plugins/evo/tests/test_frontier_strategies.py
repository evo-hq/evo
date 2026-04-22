"""Tests for evo.frontier_strategies.

Run from `plugins/evo/`:

    PYTHONPATH=src python3 -m unittest discover tests -v
"""
from __future__ import annotations

import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from evo import frontier_strategies as fs


# Fixture: 5 frontier nodes spanning a range of scores and epochs.
NODES = [
    {"id": "exp_A", "score": 0.82, "eval_epoch": 2, "hypothesis": "a"},
    {"id": "exp_B", "score": 0.79, "eval_epoch": 5, "hypothesis": "b"},
    {"id": "exp_C", "score": 0.75, "eval_epoch": 3, "hypothesis": "c"},
    {"id": "exp_D", "score": 0.71, "eval_epoch": 5, "hypothesis": "d"},
    {"id": "exp_E", "score": 0.68, "eval_epoch": 1, "hypothesis": "e"},
]

# Per-task scores for pareto_per_task. A excels on t1, B on t2, D on t3; C is
# strictly dominated (worse than A on every task).
OUTCOMES = {
    "exp_A": {"benchmark": {"result": {"tasks": {"t1": 0.90, "t2": 0.50, "t3": 0.80}}}},
    "exp_B": {"benchmark": {"result": {"tasks": {"t1": 0.50, "t2": 0.95, "t3": 0.60}}}},
    "exp_C": {"benchmark": {"result": {"tasks": {"t1": 0.40, "t2": 0.40, "t3": 0.40}}}},
    "exp_D": {"benchmark": {"result": {"tasks": {"t1": 0.30, "t2": 0.30, "t3": 0.95}}}},
}


class TestValidator(unittest.TestCase):
    def test_fills_defaults(self):
        got = fs.validate_frontier_strategy({"kind": "top_k"})
        self.assertEqual(got, {"kind": "top_k", "params": {"k": 5}})

    def test_normalizes_types(self):
        # String that casts cleanly to int is accepted.
        got = fs.validate_frontier_strategy({"kind": "top_k", "params": {"k": "7"}})
        self.assertEqual(got["params"]["k"], 7)

    def test_rejects_unknown_kind(self):
        with self.assertRaises(ValueError):
            fs.validate_frontier_strategy({"kind": "nonsense"})

    def test_rejects_out_of_range(self):
        with self.assertRaises(ValueError):
            fs.validate_frontier_strategy({"kind": "top_k", "params": {"k": 999}})
        with self.assertRaises(ValueError):
            fs.validate_frontier_strategy({"kind": "epsilon_greedy", "params": {"epsilon": 2.0}})

    def test_rejects_non_dict(self):
        with self.assertRaises(ValueError):
            fs.validate_frontier_strategy(["argmax"])

    def test_resolve_from_config_default(self):
        self.assertEqual(fs.resolve_from_config({}), fs.DEFAULT_FRONTIER_STRATEGY)

    def test_resolve_from_config_user_value(self):
        got = fs.resolve_from_config({"frontier_strategy": {"kind": "top_k", "params": {"k": 3}}})
        self.assertEqual(got, {"kind": "top_k", "params": {"k": 3}})


class TestArgmax(unittest.TestCase):
    def test_picks_highest_score(self):
        out, _ = fs.pick(NODES, {"kind": "argmax"}, "max")
        self.assertEqual([n["id"] for n in out], ["exp_A"])
        self.assertEqual(out[0]["rank"], 1)

    def test_min_metric_flips_direction(self):
        out, _ = fs.pick(NODES, {"kind": "argmax"}, "min")
        self.assertEqual(out[0]["id"], "exp_E")  # lowest score wins under min

    def test_empty_frontier(self):
        out, _ = fs.pick([], {"kind": "argmax"}, "max")
        self.assertEqual(out, [])


class TestTopK(unittest.TestCase):
    def test_picks_top_k_ordered(self):
        out, _ = fs.pick(NODES, {"kind": "top_k", "params": {"k": 3}}, "max")
        self.assertEqual([n["id"] for n in out], ["exp_A", "exp_B", "exp_C"])
        self.assertEqual([n["rank"] for n in out], [1, 2, 3])

    def test_k_larger_than_frontier(self):
        out, _ = fs.pick(NODES, {"kind": "top_k", "params": {"k": 50}}, "max")
        self.assertEqual(len(out), 5)


class TestEpsilonGreedy(unittest.TestCase):
    def test_epsilon_zero_collapses_to_argmax(self):
        for seed in range(5):
            out, _ = fs.pick(NODES, {"kind": "epsilon_greedy", "params": {"epsilon": 0.0}}, "max", seed=seed)
            self.assertEqual(out[0]["id"], "exp_A")

    def test_epsilon_one_always_random(self):
        # With eps=1.0 every draw is uniform; across many seeds we see variety.
        seen = set()
        for seed in range(50):
            out, _ = fs.pick(NODES, {"kind": "epsilon_greedy", "params": {"epsilon": 1.0}}, "max", seed=seed)
            seen.add(out[0]["id"])
        self.assertGreater(len(seen), 1)

    def test_deterministic_given_seed(self):
        out1, _ = fs.pick(NODES, {"kind": "epsilon_greedy", "params": {"epsilon": 0.5}}, "max", seed=42)
        out2, _ = fs.pick(NODES, {"kind": "epsilon_greedy", "params": {"epsilon": 0.5}}, "max", seed=42)
        self.assertEqual(out1, out2)


class TestSoftmax(unittest.TestCase):
    def test_cold_temperature_concentrates_on_best(self):
        # Over many seeds the first pick should be A the overwhelming majority of the time.
        counter = Counter()
        for seed in range(200):
            out, _ = fs.pick(NODES, {"kind": "softmax", "params": {"temperature": 0.02, "k": 1}}, "max", seed=seed)
            counter[out[0]["id"]] += 1
        self.assertGreater(counter["exp_A"], 150)

    def test_warm_temperature_spreads(self):
        counter = Counter()
        for seed in range(200):
            out, _ = fs.pick(NODES, {"kind": "softmax", "params": {"temperature": 2.0, "k": 1}}, "max", seed=seed)
            counter[out[0]["id"]] += 1
        # Every node should get picked at least once with high temperature.
        self.assertEqual(len(counter), 5)

    def test_samples_without_replacement(self):
        out, _ = fs.pick(NODES, {"kind": "softmax", "params": {"temperature": 1.0, "k": 3}}, "max", seed=1)
        ids = [n["id"] for n in out]
        self.assertEqual(len(ids), len(set(ids)))


class TestParetoPerTask(unittest.TestCase):
    def test_preserves_specialists_drops_dominated(self):
        out, _ = fs.pick(NODES[:4], {"kind": "pareto_per_task", "params": {"k": 4, "task_floor": 0.0}},
                         "max", outcomes=OUTCOMES, seed=1)
        picked = {n["id"] for n in out}
        # A, B, D each win a task; C is dominated by A on every task where C ties/wins.
        self.assertIn("exp_A", picked)
        self.assertIn("exp_B", picked)
        self.assertIn("exp_D", picked)
        self.assertNotIn("exp_C", picked)

    def test_missing_outcomes_fall_back_to_argmax(self):
        out, _ = fs.pick(NODES, {"kind": "pareto_per_task", "params": {"k": 3, "task_floor": 0.0}},
                         "max", outcomes={}, seed=1)
        # Nothing to Pareto over -> argmax fallback.
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], "exp_A")

    def test_task_floor_skips_all_zeros(self):
        outcomes = {
            "exp_A": {"benchmark": {"result": {"tasks": {"t1": 0.9, "all_fail": 0.0}}}},
            "exp_B": {"benchmark": {"result": {"tasks": {"t1": 0.5, "all_fail": 0.0}}}},
        }
        out, _ = fs.pick(NODES[:2], {"kind": "pareto_per_task", "params": {"k": 2, "task_floor": 0.0}},
                         "max", outcomes=outcomes, seed=1)
        picked = {n["id"] for n in out}
        # all_fail is skipped (max <= floor); only t1 contributes. A wins t1 alone.
        self.assertEqual(picked, {"exp_A"})

    def test_min_direction_flips_winner(self):
        # accuracy = max, latency = min. A wins accuracy, B wins latency.
        outcomes = {
            "exp_A": {"benchmark": {"result": {
                "tasks": {"accuracy": 0.9, "latency_ms": 300},
                "tasks_meta": {"accuracy": {"direction": "max"},
                               "latency_ms": {"direction": "min"}},
            }}},
            "exp_B": {"benchmark": {"result": {
                "tasks": {"accuracy": 0.6, "latency_ms": 50},
                "tasks_meta": {"accuracy": {"direction": "max"},
                               "latency_ms": {"direction": "min"}},
            }}},
        }
        out, _ = fs.pick(NODES[:2], {"kind": "pareto_per_task", "params": {"k": 2, "task_floor": 0.0}},
                         "max", outcomes=outcomes, seed=1)
        picked = {n["id"] for n in out}
        # Both are specialists: A wins accuracy, B wins latency. Neither
        # dominates the other, so both land in the sample.
        self.assertEqual(picked, {"exp_A", "exp_B"})

    def test_intersection_of_task_keys_when_drifting(self):
        # A and B agree on t1, t2; B also has t3 that A doesn't. Only the
        # intersection (t1, t2) should drive the Pareto front, so t3 doesn't
        # unfairly credit B as "winning" a task A couldn't report.
        outcomes = {
            "exp_A": {"benchmark": {"result": {"tasks": {"t1": 0.9, "t2": 0.4}}}},
            "exp_B": {"benchmark": {"result": {"tasks": {"t1": 0.4, "t2": 0.9, "t3": 0.99}}}},
        }
        out, _ = fs.pick(NODES[:2], {"kind": "pareto_per_task", "params": {"k": 2, "task_floor": 0.0}},
                         "max", outcomes=outcomes, seed=1)
        picked = {n["id"] for n in out}
        # Both are specialists on the intersection; neither dominates.
        self.assertEqual(picked, {"exp_A", "exp_B"})


class TestEndToEndFromBenchmarkStdout(unittest.TestCase):
    """Exercise the full path: benchmark stdout -> parse_score ->
    outcome.json shape -> pareto_per_task picker. Verifies that tasks_meta
    flows through every layer without being dropped or reshaped."""

    def test_tasks_meta_survives_parse_and_reaches_picker(self):
        from evo.core import parse_score

        # Two fake benchmark results. A wins accuracy (max); B wins latency (min).
        # Without honoring direction, the picker would reward high latency.
        stdout_a = json.dumps({
            "score": 0.72,
            "tasks": {"accuracy": 0.90, "latency_ms": 320},
            "tasks_meta": {
                "accuracy": {"direction": "max"},
                "latency_ms": {"direction": "min"},
            },
        })
        stdout_b = json.dumps({
            "score": 0.60,
            "tasks": {"accuracy": 0.50, "latency_ms": 80},
            "tasks_meta": {
                "accuracy": {"direction": "max"},
                "latency_ms": {"direction": "min"},
            },
        })
        score_a, parsed_a = parse_score(stdout_a)
        score_b, parsed_b = parse_score(stdout_b)

        # Shape `parsed_*` the way cli.py nests it inside outcome.json.
        outcomes = {
            "exp_A": {"benchmark": {"result": parsed_a}},
            "exp_B": {"benchmark": {"result": parsed_b}},
        }
        nodes = [
            {"id": "exp_A", "score": score_a, "eval_epoch": 1, "hypothesis": "h"},
            {"id": "exp_B", "score": score_b, "eval_epoch": 1, "hypothesis": "h"},
        ]
        out, _ = fs.pick(
            nodes,
            {"kind": "pareto_per_task", "params": {"k": 2, "task_floor": 0.0}},
            "max", outcomes=outcomes, seed=1,
        )
        picked = {n["id"] for n in out}
        # Both should survive: A wins accuracy, B wins latency. Without
        # direction handling, B would look strictly worse and be dropped.
        self.assertEqual(picked, {"exp_A", "exp_B"})

    def test_missing_tasks_meta_falls_back_to_top_level_metric(self):
        from evo.core import parse_score

        # Benchmark emitted no tasks_meta. Under metric="max" every task is
        # treated max-sense, so the picker still works coherently.
        stdout_a = json.dumps({"score": 0.72, "tasks": {"t1": 0.9, "t2": 0.4}})
        stdout_b = json.dumps({"score": 0.60, "tasks": {"t1": 0.3, "t2": 0.8}})
        _, parsed_a = parse_score(stdout_a)
        _, parsed_b = parse_score(stdout_b)
        outcomes = {
            "exp_A": {"benchmark": {"result": parsed_a}},
            "exp_B": {"benchmark": {"result": parsed_b}},
        }
        nodes = [
            {"id": "exp_A", "score": 0.72, "eval_epoch": 1, "hypothesis": "h"},
            {"id": "exp_B", "score": 0.60, "eval_epoch": 1, "hypothesis": "h"},
        ]
        out, _ = fs.pick(
            nodes,
            {"kind": "pareto_per_task", "params": {"k": 2, "task_floor": 0.0}},
            "max", outcomes=outcomes, seed=1,
        )
        picked = {n["id"] for n in out}
        # Without direction metadata, both are treated max-sense; both
        # specialize on one of the tasks, so both survive.
        self.assertEqual(picked, {"exp_A", "exp_B"})


class TestLogging(unittest.TestCase):
    def test_append_creates_file_and_record(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".evo").mkdir()
            # Minimal meta so workspace_path resolves.
            (root / ".evo" / "meta.json").write_text(json.dumps({"active": None, "next_run": 0}))
            (root / ".evo" / "config.json").write_text(json.dumps({}))  # legacy fallback
            strat = {"kind": "argmax", "params": {}}
            ev = fs.append_frontier_log(root, strat, ["exp_A"])
            self.assertEqual(ev["kind"], "frontier")
            self.assertEqual(ev["returned_ids"], ["exp_A"])
            log_path = root / ".evo" / "infra_log.json"
            self.assertTrue(log_path.exists())
            data = json.loads(log_path.read_text())
            self.assertEqual(len(data["events"]), 1)
            self.assertEqual(data["events"][0]["strategy"], strat)

    def test_seed_only_logged_when_present(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".evo").mkdir()
            (root / ".evo" / "meta.json").write_text(json.dumps({"active": None, "next_run": 0}))
            (root / ".evo" / "config.json").write_text(json.dumps({}))
            ev1 = fs.append_frontier_log(root, {"kind": "argmax", "params": {}}, ["x"])
            ev2 = fs.append_frontier_log(root, {"kind": "softmax", "params": {"temperature": 1, "k": 1}},
                                          ["y"], seed=42)
            self.assertNotIn("seed", ev1)
            self.assertEqual(ev2["seed"], 42)


if __name__ == "__main__":
    unittest.main()
