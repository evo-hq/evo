"""Tests for codex installer's legacy-marketplace cleanup.

Canonical codex marketplace name is `evo-hq` — codex 0.130+ names a
marketplace after the repo OWNER, so `codex plugin marketplace add
evo-hq/evo` registers `[plugins."evo@evo-hq"]` and resolves the plugin
root to `cache/evo-hq/evo/<ver>/`. `evo-hq-evo` (marketplace.json's
top-level `name` field) is the pre-0.4.0 name and is now legacy.

A staging-to-the-wrong-name bug shipped exit-127 hooks: the installer
used `evo-hq-evo` for the cache dir + binary staging while codex loaded
the plugin under `evo@evo-hq`, so every hook fired against a binary-less
cache dir. The fix uses the owner name (`evo-hq`) everywhere.

Cleanup contract:
  1. The canonical mkt name is the owner name (`evo-hq`).
  2. On every install, scan config.toml for `evo@<other>` entries that
     don't match the target name and remove them + their cache dirs.

These tests exercise (2) — synthetic stale configs that mirror real
breakage. Cleanup is text-based regex on TOML lines (avoiding a TOML
parser dep) so the tests check both that bad entries go AND that
unrelated entries stay.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))


class _Base(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.codex_home = Path(self._tmp.name) / ".codex"
        self.codex_home.mkdir(parents=True)
        self.cfg_path = self.codex_home / "config.toml"
        self._prev_codex_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(self.codex_home)
        self._prev_evo_home = os.environ.get("EVO_HOME")
        os.environ["EVO_HOME"] = str(Path(self._tmp.name) / ".evo")

    def tearDown(self):
        if self._prev_codex_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = self._prev_codex_home
        if self._prev_evo_home is None:
            os.environ.pop("EVO_HOME", None)
        else:
            os.environ["EVO_HOME"] = self._prev_evo_home
        self._tmp.cleanup()

    def _write_config(self, body: str) -> None:
        self.cfg_path.write_text(body)

    def _read_config(self) -> str:
        return self.cfg_path.read_text()

    def _make_cache(self, mkt: str, version: str = "0.4.3") -> Path:
        d = self.codex_home / "plugins" / "cache" / mkt / "evo" / version
        d.mkdir(parents=True)
        return d


class TestLegacyCleanup(_Base):

    def test_removes_stale_evo_hq_evo_plugin_block(self):
        from evo.host_install.codex import _cleanup_legacy_codex_registrations

        self._write_config(
            '[plugins."evo@evo-hq"]\n'
            'enabled = true\n'
            '\n'
            '[plugins."evo@evo-hq-evo"]\n'
            'enabled = true\n'
        )
        _cleanup_legacy_codex_registrations(target_mkt="evo-hq")
        text = self._read_config()
        self.assertNotIn('"evo@evo-hq-evo"', text)
        self.assertIn('"evo@evo-hq"', text)

    def test_removes_stale_marketplace_block(self):
        from evo.host_install.codex import _cleanup_legacy_codex_registrations

        self._write_config(
            '[plugins."evo@evo-hq"]\n'
            'enabled = true\n'
            '\n'
            '[plugins."evo@evo-hq-evo"]\n'
            'enabled = true\n'
            '\n'
            '[marketplaces.evo-hq-evo]\n'
            'source = "https://github.com/evo-hq/evo.git"\n'
        )
        _cleanup_legacy_codex_registrations(target_mkt="evo-hq")
        text = self._read_config()
        self.assertNotIn("[marketplaces.evo-hq-evo]", text)

    def test_removes_stale_hook_state_entries(self):
        from evo.host_install.codex import _cleanup_legacy_codex_registrations

        self._write_config(
            '[plugins."evo@evo-hq"]\n'
            'enabled = true\n'
            '\n'
            '[plugins."evo@evo-hq-evo"]\n'
            'enabled = true\n'
            '\n'
            '[hooks.state."evo@evo-hq:hooks/hooks.json:pre_tool_use:0:0"]\n'
            'trusted_hash = "sha256:abc"\n'
            '\n'
            '[hooks.state."evo@evo-hq-evo:hooks/hooks.json:pre_tool_use:0:0"]\n'
            'trusted_hash = "sha256:def"\n'
        )
        _cleanup_legacy_codex_registrations(target_mkt="evo-hq")
        text = self._read_config()
        self.assertNotIn('"evo@evo-hq-evo:hooks', text)
        self.assertIn('"evo@evo-hq:hooks', text)

    def test_removes_stale_cache_directory(self):
        from evo.host_install.codex import _cleanup_legacy_codex_registrations

        self._write_config(
            '[plugins."evo@evo-hq"]\n'
            'enabled = true\n'
            '\n'
            '[plugins."evo@evo-hq-evo"]\n'
            'enabled = true\n'
        )
        target_cache = self._make_cache("evo-hq")
        stale_cache = self._make_cache("evo-hq-evo")

        _cleanup_legacy_codex_registrations(target_mkt="evo-hq")
        self.assertFalse(stale_cache.exists(),
                         "stale evo-hq-evo cache dir must be removed")
        self.assertTrue(target_cache.exists(),
                        "target evo-hq cache dir must remain")

    def test_preserves_unrelated_plugins_and_sections(self):
        """Cleanup must only touch evo@<other> registrations. Other
        plugins (e.g. claude-code's other marketplaces) and unrelated
        sections must remain intact."""
        from evo.host_install.codex import _cleanup_legacy_codex_registrations

        self._write_config(
            '[features]\n'
            'plugin_hooks = true\n'
            '\n'
            '[plugins."evo@evo-hq"]\n'
            'enabled = true\n'
            '\n'
            '[plugins."evo@evo-hq-evo"]\n'
            'enabled = true\n'
            '\n'
            '[plugins."computer-use@openai-bundled"]\n'
            'enabled = true\n'
            '\n'
            '[plugins."github@openai-curated"]\n'
            'enabled = true\n'
            '\n'
            '[marketplaces.openai-bundled]\n'
            'source_type = "local"\n'
            '\n'
            '[tui]\n'
            'status_line = ["model-with-reasoning"]\n'
        )
        _cleanup_legacy_codex_registrations(target_mkt="evo-hq")
        text = self._read_config()
        self.assertNotIn('"evo@evo-hq-evo"', text)
        self.assertIn('"evo@evo-hq"', text)
        self.assertIn('"computer-use@openai-bundled"', text)
        self.assertIn('"github@openai-curated"', text)
        self.assertIn("[marketplaces.openai-bundled]", text)
        self.assertIn("[tui]", text)
        self.assertIn("[features]", text)

    def test_noop_when_no_legacy_entries(self):
        from evo.host_install.codex import _cleanup_legacy_codex_registrations

        original = (
            '[plugins."evo@evo-hq"]\n'
            'enabled = true\n'
            '\n'
            '[plugins."github@openai-curated"]\n'
            'enabled = true\n'
        )
        self._write_config(original)
        _cleanup_legacy_codex_registrations(target_mkt="evo-hq")
        # File untouched when nothing to clean.
        self.assertEqual(self._read_config(), original)

    def test_noop_when_no_config_file(self):
        """First-time install: config.toml may not yet exist."""
        from evo.host_install.codex import _cleanup_legacy_codex_registrations

        self.assertFalse(self.cfg_path.exists())
        # Should silently return, not raise.
        _cleanup_legacy_codex_registrations(target_mkt="evo-hq")
        self.assertFalse(self.cfg_path.exists())

    def test_handles_multiple_legacy_names(self):
        """If a config has three different evo registrations (e.g. user
        bounced between several pre-rename versions), remove all that
        don't match target."""
        from evo.host_install.codex import _cleanup_legacy_codex_registrations

        self._write_config(
            '[plugins."evo@evo-hq"]\n'
            'enabled = true\n'
            '\n'
            '[plugins."evo@old-alpha"]\n'
            'enabled = true\n'
            '\n'
            '[plugins."evo@evo-hq-evo"]\n'
            'enabled = true\n'
        )
        self._make_cache("evo-hq")
        self._make_cache("old-alpha")
        self._make_cache("evo-hq-evo")

        _cleanup_legacy_codex_registrations(target_mkt="evo-hq")
        text = self._read_config()
        self.assertNotIn('"evo@old-alpha"', text)
        self.assertNotIn('"evo@evo-hq-evo"', text)
        self.assertIn('"evo@evo-hq"', text)
        self.assertFalse((self.codex_home / "plugins" / "cache" / "old-alpha").exists())
        self.assertFalse((self.codex_home / "plugins" / "cache" / "evo-hq-evo").exists())
        self.assertTrue((self.codex_home / "plugins" / "cache" / "evo-hq").exists())


class TestDisablePlugin(_Base):
    """`uninstall` → `_enable_plugin(enable=False)` must remove the
    `[plugins."evo@evo-hq"]` header AND the `enabled = true` key written
    beneath it. Removing only the header orphans `enabled = true`, which
    a TOML parser attaches to the preceding table."""

    def test_disable_removes_header_and_enabled_key(self):
        from evo.host_install.codex import _enable_plugin

        self._write_config(
            '[features]\n'
            'plugin_hooks = true\n'
            '\n'
            '[plugins."evo@evo-hq"]\n'
            'enabled = true\n'
            '\n'
            '[plugins."github@openai-curated"]\n'
            'enabled = true\n'
        )
        changed, _ = _enable_plugin(enable=False)
        self.assertTrue(changed)
        text = self._read_config()
        # evo block fully gone — no orphan key.
        self.assertNotIn('"evo@evo-hq"', text)
        self.assertNotIn('[features]\nplugin_hooks = true\nenabled = true', text)
        # Surrounding sections + the other plugin's own enabled key intact.
        self.assertIn('[features]', text)
        self.assertIn('plugin_hooks = true', text)
        self.assertIn('[plugins."github@openai-curated"]', text)
        self.assertIn('enabled = true', text)  # github's, not evo's

    def test_disable_noop_when_absent(self):
        from evo.host_install.codex import _enable_plugin

        original = (
            '[features]\n'
            'plugin_hooks = true\n'
            '\n'
            '[plugins."github@openai-curated"]\n'
            'enabled = true\n'
        )
        self._write_config(original)
        changed, _ = _enable_plugin(enable=False)
        self.assertFalse(changed)
        self.assertEqual(self._read_config(), original)


class TestCodexConfigConvergence(_Base):

    def test_enable_plugin_replaces_disabled_and_duplicate_blocks(self):
        from evo.host_install.codex import _enable_plugin

        self._write_config(
            '[features]\n'
            'plugin_hooks = true\n'
            '\n'
            '[plugins."evo@evo-hq"]\n'
            'enabled = false\n'
            '\n'
            '[plugins."github@openai-curated"]\n'
            'enabled = true\n'
            '\n'
            '[plugins."evo@evo-hq"]\n'
            'enabled = true\n'
        )
        changed, _ = _enable_plugin(enable=True)
        self.assertTrue(changed)
        text = self._read_config()
        self.assertEqual(text.count('[plugins."evo@evo-hq"]'), 1)
        self.assertIn('[plugins."evo@evo-hq"]\nenabled = true', text)
        self.assertNotIn('[plugins."evo@evo-hq"]\nenabled = false', text)
        self.assertIn('[plugins."github@openai-curated"]', text)

    def test_trust_hooks_replaces_disabled_duplicate_hook_state_blocks(self):
        from evo.host_install.codex import _trust_plugin_hooks

        hooks = self.codex_home / "plugins" / "cache" / "evo-hq" / "evo" / "0.6.2" / "hooks" / "hooks.json"
        hooks.parent.mkdir(parents=True)
        hooks.write_text(
            '{"hooks":{"SessionStart":[{"hooks":[{"type":"command",'
            '"command":"node -e \\"process.stdout.write(\\\\\\"{}\\\\\\\\n\\\\\\")\\""}]}]}}'
        )
        self._write_config(
            '[features]\n'
            'plugin_hooks = true\n'
            '\n'
            '[hooks.state."evo@evo-hq:hooks/hooks.json:session_start:0:0"]\n'
            'enabled = false\n'
            'trusted_hash = "sha256:old"\n'
            '\n'
            '[plugins."github@openai-curated"]\n'
            'enabled = true\n'
            '\n'
            '[hooks.state."evo@evo-hq:hooks/hooks.json:session_start:0:0"]\n'
            'enabled = true\n'
            'trusted_hash = "sha256:also-old"\n'
        )

        _trust_plugin_hooks(hooks, plugin_id="evo@evo-hq", cfg=self.cfg_path)

        text = self._read_config()
        self.assertEqual(
            text.count('[hooks.state."evo@evo-hq:hooks/hooks.json:session_start:0:0"]'),
            1,
        )
        self.assertNotIn("enabled = false", text)
        self.assertNotIn("sha256:old", text)
        self.assertNotIn("sha256:also-old", text)
        self.assertIn('trusted_hash = "sha256:', text)
        self.assertIn('[plugins."github@openai-curated"]', text)

    def test_no_trust_cleanup_removes_stale_hook_state(self):
        from evo.host_install.codex import _strip_plugin_hook_state

        self._write_config(
            '[hooks.state."evo@evo-hq:hooks/hooks.json:pre_tool_use:0:0"]\n'
            'enabled = false\n'
            'trusted_hash = "sha256:old"\n'
            '\n'
            '[plugins."github@openai-curated"]\n'
            'enabled = true\n'
        )
        self.assertTrue(_strip_plugin_hook_state(self.cfg_path, "evo@evo-hq"))
        text = self._read_config()
        self.assertNotIn("[hooks.state.", text)
        self.assertIn('[plugins."github@openai-curated"]', text)


class TestDoctorHookBinary(_Base):
    """`doctor()` resolves the hook-drain binary at the cache dir codex
    actually loads. These exercise the version-dir selection (numeric, not
    lexicographic) and comment-line handling in that resolution."""

    def _healthy_config(self) -> None:
        self._write_config(
            '[features]\n'
            'plugin_hooks = true\n'
            '\n'
            '[plugins."evo@evo-hq"]\n'
            'enabled = true\n'
        )
        # `cache` check in doctor() looks at the marketplace clone.
        (self.codex_home / ".tmp" / "marketplaces" / "evo-hq").mkdir(parents=True)

    def _stage_binary(self, version: str) -> None:
        name = "evo-hook-drain.exe" if sys.platform == "win32" else "evo-hook-drain"
        b = (self.codex_home / "plugins" / "cache" / "evo-hq" / "evo"
             / version / "bin" / name)
        b.parent.mkdir(parents=True, exist_ok=True)
        # Non-script bytes: doctor treats a "#!" file as the committed
        # fallback wrapper and then also requires the stable copy.
        b.write_bytes(b"\x7fELF-fake\n")
        os.chmod(b, 0o755)

    def _stage_hooks(self, version: str, command: str) -> None:
        hooks = (
            self.codex_home / "plugins" / "cache" / "evo-hq" / "evo"
            / version / "hooks" / "hooks.json"
        )
        hooks.parent.mkdir(parents=True, exist_ok=True)
        hooks.write_text(
            '{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":'
            + repr(command).replace("'", '"')
            + '}]}]}}\n'
        )

    def _run_doctor(self) -> int:
        import argparse
        import contextlib
        import io
        from evo.host_install.codex import doctor
        with contextlib.redirect_stdout(io.StringIO()):
            return doctor(argparse.Namespace())

    def test_picks_numerically_latest_version_dir(self):
        # Binary only in 0.10.0. Lexicographic sort would pick 0.9.0 and
        # wrongly report the binary missing.
        self._healthy_config()
        (self.codex_home / "plugins" / "cache" / "evo-hq" / "evo"
         / "0.9.0").mkdir(parents=True)
        self._stage_binary("0.10.0")
        self.assertEqual(self._run_doctor(), 0)

    def test_missing_binary_fails(self):
        self._healthy_config()
        (self.codex_home / "plugins" / "cache" / "evo-hq" / "evo"
         / "0.4.5" / "bin").mkdir(parents=True)
        self.assertEqual(self._run_doctor(), 1)

    def test_ignores_commented_plugin_entry(self):
        # A commented-out registration must not be chased to a missing
        # cache dir.
        self._write_config(
            '[features]\n'
            'plugin_hooks = true\n'
            '\n'
            '[plugins."evo@evo-hq"]\n'
            'enabled = true\n'
            '\n'
            '# [plugins."evo@ghost"]\n'
            '# enabled = true\n'
        )
        (self.codex_home / ".tmp" / "marketplaces" / "evo-hq").mkdir(parents=True)
        self._stage_binary("0.4.5")
        self.assertEqual(self._run_doctor(), 0)

    def test_doctor_fails_when_codex_hooks_still_depend_on_claude_plugin_root(self):
        self._healthy_config()
        self._stage_binary("0.6.0")
        self._stage_hooks("0.6.0", "${CLAUDE_PLUGIN_ROOT}/bin/evo-hook-drain")
        self.assertEqual(self._run_doctor(), 1)


if __name__ == "__main__":
    unittest.main()
