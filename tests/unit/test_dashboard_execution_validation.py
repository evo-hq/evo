"""Dashboard API tests: POST /api/workspace/execution with malformed
payloads must return 400 and leave the existing configuration untouched,
instead of 500ing or silently resetting saved settings."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from evo.core import init_workspace, load_config, save_config
from evo.dashboard import create_app


class TestExecutionSettingsValidation(unittest.TestCase):
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

    def _seed_remote_config(self) -> dict:
        cfg = load_config(self.root)
        cfg["execution_backend"] = "remote"
        cfg["execution_backend_config"] = {
            "provider": "manual",
            "provider_config": {
                "base_url": "http://127.0.0.1:9",
                "bearer_token": "seed-secret",
            },
        }
        save_config(self.root, cfg)
        return cfg["execution_backend_config"]

    def _post(self, payload: dict):
        return self.client.post("/api/workspace/execution", json=payload)

    def test_provider_config_list_is_rejected_and_config_untouched(self):
        seeded = self._seed_remote_config()
        resp = self._post(
            {"backend": "remote", "provider": "manual", "provider_config": []}
        )
        self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True))
        cfg = load_config(self.root)
        self.assertEqual(cfg["execution_backend_config"], seeded)

    def test_provider_config_string_is_rejected_not_500(self):
        resp = self._post(
            {"backend": "remote", "provider": "manual", "provider_config": "oops"}
        )
        self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True))
        self.assertIn("provider_config", resp.get_json()["error"])

    def test_provider_config_nested_object_value_is_rejected_not_500(self):
        resp = self._post(
            {
                "backend": "remote",
                "provider": "manual",
                "provider_config": {"base_url": {"nested": 1}},
            }
        )
        self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True))
        self.assertIn("base_url", resp.get_json()["error"])

    def test_provider_construction_failure_is_400_not_500(self):
        resp = self._post(
            {
                "backend": "remote",
                "provider": "definitely-not-a-provider",
                "provider_config": {},
            }
        )
        self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True))
        self.assertIn("provider", resp.get_json()["error"].lower())

    def test_valid_manual_save_still_works(self):
        resp = self._post(
            {
                "backend": "remote",
                "provider": "manual",
                "provider_config": {
                    "base_url": "http://127.0.0.1:9",
                    "bearer_token": "tok",
                },
            }
        )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        cfg = load_config(self.root)
        self.assertEqual(cfg["execution_backend"], "remote")
        self.assertEqual(
            cfg["execution_backend_config"]["provider_config"]["base_url"],
            "http://127.0.0.1:9",
        )


if __name__ == "__main__":
    unittest.main()
