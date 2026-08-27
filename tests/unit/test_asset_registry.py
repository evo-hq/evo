"""Tests for the workspace asset registry (#55, local-first).

Split in two: the pure registry logic (no I/O) and the `evo asset` CLI
round-trip against a temp workspace.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from evo.assets import (
    asset_env_for_exp,
    asset_env_var,
    empty_registry,
    parse_tag,
    registry_filter,
    registry_put,
    registry_record_use,
    registry_remove,
)


class TestPureRegistry(unittest.TestCase):
    def _entry(self, name, **kw):
        base = {
            "name": name, "kind": kw.get("kind", "model"),
            "path": kw.get("path", f"/data/{name}"), "tags": kw.get("tags", {}),
            "produced_by": kw.get("produced_by"), "consumed_by": kw.get("consumed_by", []),
            "copied": kw.get("copied", False), "created_at": "2026-08-27T00:00:00Z",
        }
        return base

    def test_put_then_present(self):
        reg = empty_registry()
        registry_put(reg, self._entry("base-model"))
        self.assertIn("base-model", reg["assets"])

    def test_put_rejects_empty_name(self):
        with self.assertRaises(ValueError):
            registry_put(empty_registry(), self._entry(""))

    def test_put_rejects_empty_kind(self):
        with self.assertRaises(ValueError):
            registry_put(empty_registry(), self._entry("m", kind=""))

    def test_filter_by_kind(self):
        reg = empty_registry()
        registry_put(reg, self._entry("m1", kind="model"))
        registry_put(reg, self._entry("d1", kind="dataset"))
        names = [e["name"] for e in registry_filter(reg, kind="dataset")]
        self.assertEqual(names, ["d1"])

    def test_filter_by_tags_is_and_match(self):
        reg = empty_registry()
        registry_put(reg, self._entry("a", tags={"verifier": "exact", "epoch": "2"}))
        registry_put(reg, self._entry("b", tags={"verifier": "exact"}))
        names = [e["name"] for e in registry_filter(reg, tags={"verifier": "exact", "epoch": "2"})]
        self.assertEqual(names, ["a"])

    def test_filter_by_produced_and_consumed(self):
        reg = empty_registry()
        registry_put(reg, self._entry("a", produced_by="exp_0001"))
        registry_put(reg, self._entry("b", consumed_by=["exp_0002"]))
        self.assertEqual([e["name"] for e in registry_filter(reg, produced_by="exp_0001")], ["a"])
        self.assertEqual([e["name"] for e in registry_filter(reg, consumed_by="exp_0002")], ["b"])

    def test_record_use_appends_once(self):
        reg = empty_registry()
        registry_put(reg, self._entry("a"))
        registry_record_use(reg, "a", "exp_0002")
        registry_record_use(reg, "a", "exp_0002")  # idempotent
        self.assertEqual(reg["assets"]["a"]["consumed_by"], ["exp_0002"])

    def test_record_use_unknown_raises(self):
        with self.assertRaises(KeyError):
            registry_record_use(empty_registry(), "nope", "exp_0002")

    def test_remove_refuses_when_consumed(self):
        reg = empty_registry()
        registry_put(reg, self._entry("a", consumed_by=["exp_0002"]))
        with self.assertRaises(RuntimeError):
            registry_remove(reg, "a")

    def test_remove_force_when_consumed(self):
        reg = empty_registry()
        registry_put(reg, self._entry("a", consumed_by=["exp_0002"]))
        registry_remove(reg, "a", force=True)
        self.assertNotIn("a", reg["assets"])

    def test_remove_unknown_raises(self):
        with self.assertRaises(KeyError):
            registry_remove(empty_registry(), "nope")

    def test_asset_env_var(self):
        self.assertEqual(asset_env_var("base-model"), "EVO_ASSET_BASE_MODEL")
        self.assertEqual(asset_env_var("numina.cot 5k"), "EVO_ASSET_NUMINA_COT_5K")

    def test_parse_tag(self):
        self.assertEqual(parse_tag("epoch=2"), ("epoch", "2"))
        self.assertEqual(parse_tag("uri=s3://a/b=c"), ("uri", "s3://a/b=c"))

    def test_parse_tag_rejects_missing_eq(self):
        with self.assertRaises(ValueError):
            parse_tag("noequals")

    def test_asset_env_for_exp_injects_consumed(self):
        reg = empty_registry()
        registry_put(reg, self._entry("base-model", path="/m", consumed_by=["exp_0002"]))
        registry_put(reg, self._entry("other", path="/o", consumed_by=["exp_0009"]))
        env = asset_env_for_exp(reg, "exp_0002")
        self.assertEqual(env, {"EVO_ASSET_BASE_MODEL": "/m"})

    def test_asset_env_for_exp_empty_when_none_consumed(self):
        reg = empty_registry()
        registry_put(reg, self._entry("base-model", consumed_by=["exp_0009"]))
        self.assertEqual(asset_env_for_exp(reg, "exp_0002"), {})

    def test_asset_env_for_exp_remote_injects_uri(self):
        # Remote assets have no local path yet; expose the uri so the recipe
        # can `evo asset get` it.
        reg = empty_registry()
        entry = self._entry("model", path=None, consumed_by=["exp_0002"])
        entry["uri"] = "s3://b/model.bin"
        entry["backend"] = "s3"
        registry_put(reg, entry)
        self.assertEqual(asset_env_for_exp(reg, "exp_0002"),
                         {"EVO_ASSET_MODEL": "s3://b/model.bin"})


if __name__ == "__main__":
    unittest.main()
