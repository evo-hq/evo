"""Dashboard API tests: POST a strategy change, verify it persists to
config.json on disk, and verify the next `evo frontier` invocation reads
the new strategy. Exercises the full dashboard -> config -> picker chain.

Run from `plugins/evo/` with the plugin venv (needs flask):

    .venv/bin/python -m unittest discover tests -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from evo.core import init_workspace, load_config
from evo.dashboard import create_app
from evo import frontier_strategies as fs


NODES = [
    {"id": "exp_A", "score": 0.82, "eval_epoch": 2, "hypothesis": "h"},
    {"id": "exp_B", "score": 0.79, "eval_epoch": 5, "hypothesis": "h"},
    {"id": "exp_C", "score": 0.75, "eval_epoch": 3, "hypothesis": "h"},
]


class TestDashboardFrontierStrategy(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        init_workspace(
            self.root,
            target="t.py",
            benchmark="python bench.py",
            metric="max",
            gate=None,
        )
        self.app = create_app(self.root)
        self.client = self.app.test_client()

    def tearDown(self):
        self._tmp.cleanup()

    # ---- GET ----

    def test_get_returns_registry_current_default(self):
        res = self.client.get("/api/frontier-strategy")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("registry", data)
        self.assertIn("current", data)
        self.assertIn("default", data)
        # Every registered strategy appears in the registry payload.
        for kind in fs.FRONTIER_STRATEGIES:
            self.assertIn(kind, data["registry"])
        # Fresh workspace -> current matches the default.
        self.assertEqual(data["current"], fs.DEFAULT_FRONTIER_STRATEGY)
        self.assertEqual(data["default"], fs.DEFAULT_FRONTIER_STRATEGY)

    # ---- POST validation ----

    def test_post_unknown_kind_returns_400(self):
        res = self.client.post("/api/frontier-strategy", json={"kind": "nonsense"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("error", res.get_json())

    def test_post_param_out_of_range_returns_400(self):
        res = self.client.post(
            "/api/frontier-strategy",
            json={"kind": "top_k", "params": {"k": 999}},
        )
        self.assertEqual(res.status_code, 400)

    def test_post_missing_params_fills_defaults(self):
        res = self.client.post("/api/frontier-strategy", json={"kind": "top_k"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["params"]["k"], 5)

    def test_post_coerces_string_numbers(self):
        # Dashboard form posts JSON; browsers sometimes send stringified numbers.
        res = self.client.post(
            "/api/frontier-strategy",
            json={"kind": "epsilon_greedy", "params": {"epsilon": "0.25"}},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["params"]["epsilon"], 0.25)

    # ---- POST persistence ----

    def test_post_writes_config_json(self):
        self.client.post(
            "/api/frontier-strategy",
            json={"kind": "top_k", "params": {"k": 7}},
        )
        cfg = load_config(self.root)
        self.assertEqual(cfg["frontier_strategy"], {"kind": "top_k", "params": {"k": 7}})

    def test_get_after_post_reflects_new_value(self):
        self.client.post(
            "/api/frontier-strategy",
            json={"kind": "epsilon_greedy", "params": {"epsilon": 0.3}},
        )
        data = self.client.get("/api/frontier-strategy").get_json()
        self.assertEqual(
            data["current"],
            {"kind": "epsilon_greedy", "params": {"epsilon": 0.3}},
        )

    def test_failed_post_does_not_mutate_config(self):
        # Snapshot the config, submit a bad POST, verify config unchanged.
        before = load_config(self.root).get("frontier_strategy")
        self.client.post("/api/frontier-strategy", json={"kind": "nonsense"})
        after = load_config(self.root).get("frontier_strategy")
        self.assertEqual(before, after)

    # ---- End-to-end: API change -> next pick honors it ----

    def test_next_pick_uses_new_strategy_from_config(self):
        """The crucial flow: POST a strategy, then resolving from config and
        picking returns behavior matching the posted strategy -- not the
        default that was in place at app startup."""
        # Baseline: argmax returns the single top node.
        strat = fs.resolve_from_config(load_config(self.root))
        out, _ = fs.pick(NODES, strat, "max")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], "exp_A")

        # Change via the dashboard endpoint.
        res = self.client.post(
            "/api/frontier-strategy",
            json={"kind": "top_k", "params": {"k": 3}},
        )
        self.assertEqual(res.status_code, 200)

        # Re-resolve from config -- a fresh pick must see the new strategy.
        strat = fs.resolve_from_config(load_config(self.root))
        self.assertEqual(strat, {"kind": "top_k", "params": {"k": 3}})
        out, _ = fs.pick(NODES, strat, "max")
        self.assertEqual([n["id"] for n in out], ["exp_A", "exp_B", "exp_C"])

    def test_multiple_consecutive_changes(self):
        # Simulate a user flipping through strategies in the dashboard. Every
        # change should land on disk and be observable by the picker.
        for spec in [
            {"kind": "argmax", "params": {}},
            {"kind": "top_k", "params": {"k": 2}},
            {"kind": "epsilon_greedy", "params": {"epsilon": 0.5}},
            {"kind": "softmax", "params": {"temperature": 0.8, "k": 2}},
            {"kind": "argmax", "params": {}},
        ]:
            self.client.post("/api/frontier-strategy", json=spec)
            cfg = load_config(self.root)
            resolved = fs.resolve_from_config(cfg)
            # params get normalized (missing -> defaults), but what we sent
            # must round-trip exactly since we sent complete spec.
            self.assertEqual(resolved, spec)


if __name__ == "__main__":
    unittest.main()
