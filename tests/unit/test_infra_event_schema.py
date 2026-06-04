"""Infra-log schema regression tests.

Issue #24: infra-log writers should share one event constructor so scratchpad
readers do not depend on multiple subtly different dict shapes.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from evo import frontier_strategies as fs
from evo.core import append_infra_event, init_workspace, make_infra_event
from evo.scratchpad import build_scratchpad


class TestInfraEventSchema(unittest.TestCase):
    def test_generic_and_frontier_events_share_required_fields(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            init_workspace(
                root,
                target="target.py",
                benchmark="python bench.py",
                metric="max",
                gate=None,
            )

            generic = append_infra_event(root, "runtime changed", breaking=True)
            frontier = fs.append_frontier_log(
                root,
                {"kind": "softmax", "params": {"temperature": 1, "k": 1}},
                ["exp_0001"],
                seed=7,
            )

            for event in (generic, frontier):
                self.assertIn("kind", event)
                self.assertIn("message", event)
                self.assertIn("timestamp", event)
                self.assertNotIn("at", event)

            self.assertEqual(generic["kind"], "infra")
            self.assertEqual(generic["breaking"], True)
            self.assertEqual(frontier["kind"], "frontier")
            self.assertEqual(frontier["returned_ids"], ["exp_0001"])
            self.assertEqual(frontier["seed"], 7)

            text = build_scratchpad(root)
            self.assertIn("runtime changed (breaking)", text)
            self.assertIn("frontier(softmax)", text)

    def test_extra_fields_cannot_override_required_schema(self):
        with self.assertRaises(ValueError):
            make_infra_event("frontier", "msg", timestamp="later")
        with self.assertRaises(ValueError):
            make_infra_event("frontier", "msg", **{"kind": "other"})
        with self.assertRaises(ValueError):
            make_infra_event("frontier", "msg", **{"message": "other"})


if __name__ == "__main__":
    unittest.main()
