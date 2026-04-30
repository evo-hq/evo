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

from evo.backends import backend_state_key
from evo.backends import pool_state, remote_state
from evo.core import (
    atomic_write_json,
    graph_path,
    init_workspace,
    load_config,
    load_graph,
    save_config,
    set_host,
)
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

    def test_workspace_endpoint_reports_backend_configs_and_redacts_secrets(self):
        set_host(self.root, "codex")
        cfg = load_config(self.root)
        cfg["commit_strategy"] = "tracked-only"
        cfg["execution_backend"] = "remote"
        cfg["execution_backend_config"] = {
            "provider": "e2b",
            "provider_config": {
                "api_key": "e2b-secret",
                "pool_size": 2,
                "template": "base",
            },
        }
        save_config(self.root, cfg)

        remote_cfg = cfg["execution_backend_config"]
        remote_key = backend_state_key("remote", remote_cfg)
        remote_state.init_state(
            self.root,
            provider="e2b",
            provider_config=dict(remote_cfg["provider_config"]),
            state_key=remote_key,
        )
        with remote_state.locked_state(self.root, remote_key) as state:
            state["sandboxes"].extend(
                [
                    {
                        "id": 0,
                        "native_id": "sb-live",
                        "base_url": "https://sandbox.example",
                        "bearer_token": "sandbox-token",
                        "leased_by": {"exp_id": "exp_0000", "pid": 123, "leased_at": "2026-04-30T00:00:00+00:00"},
                        "last_branch": "evo/run_0000/exp_0000",
                        "metadata": {"workspace_root": "/tmp/evo-e2b/sb-live/repo"},
                        "provisioned_at": "2026-04-30T00:00:00+00:00",
                    },
                    {
                        "id": 1,
                        "native_id": "sb-free",
                        "base_url": "https://sandbox-free.example",
                        "bearer_token": "sandbox-token-2",
                        "leased_by": None,
                        "last_branch": None,
                        "metadata": {"workspace_root": "/tmp/evo-e2b/sb-free/repo"},
                        "provisioned_at": "2026-04-30T00:01:00+00:00",
                    },
                ]
            )

        graph = load_graph(self.root)
        graph["next_id"] = 2
        graph["nodes"]["root"]["children"] = ["exp_0000", "exp_0001"]
        graph["nodes"]["exp_0000"] = {
            "id": "exp_0000",
            "parent": "root",
            "children": [],
            "status": "active",
            "score": None,
            "hypothesis": "remote node",
            "created_at": "2026-04-30T00:00:00+00:00",
            "eval_epoch": 1,
            "branch": "evo/run_0000/exp_0000",
            "commit": None,
            "worktree": "/tmp/evo-e2b/sb-live/repo",
            "benchmark_result": None,
            "gate_result": None,
            "gates": [],
        }
        pool_cfg = {"slots": ["/tmp/slot-a", "/tmp/slot-b"]}
        graph["nodes"]["exp_0001"] = {
            "id": "exp_0001",
            "parent": "root",
            "children": [],
            "status": "pending",
            "score": None,
            "hypothesis": "pool override",
            "created_at": "2026-04-30T00:02:00+00:00",
            "eval_epoch": 1,
            "branch": "evo/run_0000/exp_0001",
            "commit": None,
            "worktree": "/tmp/slot-a",
            "benchmark_result": None,
            "gate_result": None,
            "gates": [],
            "backend": "pool",
            "backend_config": pool_cfg,
        }
        atomic_write_json(graph_path(self.root), graph)

        pool_key = backend_state_key("pool", pool_cfg)
        pool_state.init_state(self.root, pool_cfg["slots"], pool_key)
        with pool_state.locked_state(self.root, pool_key) as state:
            state["slots"][0]["leased_by"] = {
                "exp_id": "exp_0001",
                "pid": 456,
                "leased_at": "2026-04-30T00:02:00+00:00",
            }

        res = self.client.get("/api/workspace")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["host"], "codex")
        self.assertEqual(data["commit_strategy"], "tracked-only")
        self.assertEqual(data["default_backend"]["name"], "remote")
        self.assertEqual(
            data["default_backend"]["config"]["provider_config"]["api_key"],
            "<redacted>",
        )
        for provider in ("modal", "e2b", "cloudflare", "daytona", "aws", "hetzner", "ssh", "manual"):
            self.assertIn(provider, data["provider_readiness"], provider)

        by_name = {
            (item["name"], item.get("provider")): item
            for item in data["backend_configs"]
        }
        remote_entry = by_name[("remote", "e2b")]
        self.assertTrue(remote_entry["is_default"])
        self.assertEqual(remote_entry["runtime"]["sandbox_count"], 2)
        self.assertEqual(remote_entry["runtime"]["leased_count"], 1)
        self.assertEqual(
            remote_entry["runtime"]["sandboxes"][0]["bearer_token"],
            "<redacted>",
        )

        pool_entry = by_name[("pool", None)]
        self.assertEqual(pool_entry["runtime"]["slot_count"], 2)
        self.assertEqual(pool_entry["runtime"]["leased_count"], 1)
        self.assertEqual(pool_entry["node_ids"], ["exp_0001"])

    def test_execution_settings_post_accepts_modal_gpu_and_pool_size(self):
        res = self.client.post(
            "/api/workspace/execution",
            json={
                "backend": "remote",
                "provider": "modal",
                "provider_config": {
                    "app_name": "evo-test",
                    "gpu": "L40S",
                    "pool_size": 2,
                },
            },
        )
        self.assertEqual(res.status_code, 200, res.get_json())
        cfg = load_config(self.root)
        self.assertEqual(cfg["execution_backend"], "remote")
        self.assertEqual(cfg["execution_backend_config"]["provider"], "modal")
        self.assertEqual(cfg["execution_backend_config"]["provider_config"]["gpu"], "L40S")
        self.assertEqual(cfg["execution_backend_config"]["provider_config"]["pool_size"], 2)
        self.assertEqual(res.get_json()["default_backend"]["config"]["provider_config"]["gpu"], "L40S")

    def test_execution_settings_post_accepts_cloudflare_bridge_config(self):
        res = self.client.post(
            "/api/workspace/execution",
            json={
                "backend": "remote",
                "provider": "cloudflare",
                "provider_config": {
                    "api_url": "https://bridge.example.workers.dev",
                    "api_key": "cf-secret",
                },
            },
        )
        self.assertEqual(res.status_code, 200, res.get_json())
        cfg = load_config(self.root)
        self.assertEqual(cfg["execution_backend"], "remote")
        self.assertEqual(cfg["execution_backend_config"]["provider"], "cloudflare")
        self.assertEqual(cfg["execution_backend_config"]["provider_config"]["api_url"], "https://bridge.example.workers.dev")
        self.assertEqual(cfg["execution_backend_config"]["provider_config"]["api_key"], "cf-secret")
        self.assertEqual(res.get_json()["default_backend"]["config"]["provider_config"]["api_url"], "https://bridge.example.workers.dev")

    def test_graph_and_node_endpoints_redact_backend_secrets_and_resolve_backend(self):
        graph = load_graph(self.root)
        graph["next_id"] = 1
        graph["nodes"]["root"]["children"] = ["exp_0000"]
        graph["nodes"]["exp_0000"] = {
            "id": "exp_0000",
            "parent": "root",
            "children": [],
            "status": "pending",
            "score": None,
            "hypothesis": "manual remote override",
            "created_at": "2026-04-30T00:00:00+00:00",
            "eval_epoch": 1,
            "branch": "evo/run_0000/exp_0000",
            "commit": None,
            "worktree": "/tmp/manual",
            "benchmark_result": None,
            "gate_result": None,
            "gates": [],
            "backend": "remote",
            "backend_config": {
                "provider": "manual",
                "provider_config": {
                    "base_url": "http://127.0.0.1:9999",
                    "bearer_token": "manual-secret",
                },
            },
        }
        atomic_write_json(graph_path(self.root), graph)

        graph_data = self.client.get("/api/graph").get_json()
        node = graph_data["nodes"]["exp_0000"]
        self.assertEqual(
            node["backend_config"]["provider_config"]["bearer_token"],
            "<redacted>",
        )
        self.assertEqual(node["resolved_backend"]["name"], "remote")
        self.assertEqual(node["resolved_backend"]["provider"], "manual")
        self.assertEqual(node["resolved_backend"]["source"], "override")
        self.assertEqual(
            node["resolved_backend"]["config"]["provider_config"]["bearer_token"],
            "<redacted>",
        )

        node_data = self.client.get("/api/node/exp_0000").get_json()
        self.assertEqual(
            node_data["resolved_backend"]["config"]["provider_config"]["bearer_token"],
            "<redacted>",
        )

    def test_execution_settings_post_updates_remote_backend_and_preserves_secret(self):
        first = self.client.post(
            "/api/workspace/execution",
            json={
                "backend": "remote",
                "provider": "manual",
                "provider_config": {
                    "base_url": "http://127.0.0.1:9999",
                    "bearer_token": "secret-token",
                    "workspace_root": "/tmp/manual-repo",
                },
            },
        )
        self.assertEqual(first.status_code, 200, first.get_json())
        cfg = load_config(self.root)
        self.assertEqual(cfg["execution_backend"], "remote")
        self.assertEqual(cfg["execution_backend_config"]["provider"], "manual")
        self.assertEqual(
            cfg["execution_backend_config"]["provider_config"]["bearer_token"],
            "secret-token",
        )

        second = self.client.post(
            "/api/workspace/execution",
            json={
                "backend": "remote",
                "provider": "manual",
                "provider_config": {
                    "base_url": "http://127.0.0.1:7777",
                    "workspace_root": "/tmp/manual-repo-2",
                },
            },
        )
        self.assertEqual(second.status_code, 200, second.get_json())
        cfg = load_config(self.root)
        self.assertEqual(
            cfg["execution_backend_config"]["provider_config"]["base_url"],
            "http://127.0.0.1:7777",
        )
        self.assertEqual(
            cfg["execution_backend_config"]["provider_config"]["workspace_root"],
            "/tmp/manual-repo-2",
        )
        self.assertEqual(
            cfg["execution_backend_config"]["provider_config"]["bearer_token"],
            "secret-token",
        )
        data = second.get_json()
        self.assertEqual(
            data["default_backend"]["config"]["provider_config"]["bearer_token"],
            "<redacted>",
        )

    def test_execution_settings_post_rejects_invalid_pool_paths(self):
        res = self.client.post(
            "/api/workspace/execution",
            json={
                "backend": "pool",
                "workspaces": ["relative/path"],
            },
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("absolute", res.get_json()["error"])

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
