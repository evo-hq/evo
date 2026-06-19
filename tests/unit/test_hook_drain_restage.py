"""Hook-drain binary must survive a host plugin re-stage.

Hosts re-stage the plugin cache by copying the marketplace snapshot:
codex does it on its own when the snapshot changes; claude-code on
`claude plugin update`. The snapshot is a git tree without the binary
(release assets aren't committed), so before the mirror fix a re-stage
silently dropped `bin/evo-hook-drain` from the cache and every hook
fired exit 127, with `evo doctor` passing right up until the re-stage.

The fix: installers mirror the fetched binary into the snapshot, so a
re-stage carries it. These tests run the real installer file-copy paths
in sandboxed CODEX_HOME / CLAUDE_CONFIG_DIR roots, with
EVO_HOOK_DRAIN_BINARY pointing at a local file (the documented bypass
for the GitHub release fetch), then simulate the host's re-stage and
assert the binary is still there.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))

from evo.host_install import claude_code, codex  # noqa: E402
from evo.host_install._hook_drain import (  # noqa: E402
    hook_drain_binary_name,
    is_wrapper_script,
    mirror_hook_drain_binary,
)

HOOK_NAME = hook_drain_binary_name()
REPO_WRAPPER = REPO_ROOT / "plugins" / "evo" / "bin" / "evo-hook-drain"


class _SandboxBase(unittest.TestCase):
    """Temp home + EVO_HOOK_DRAIN_BINARY pointing at a fake binary."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        # Non-script bytes: the staging code distinguishes the committed
        # shell wrapper (starts with "#!") from a native binary.
        fake_binary = self.root / "fake-evo-hook-drain"
        fake_binary.write_bytes(b"\x7fELF-fake-evo-hook-drain\n")
        fake_binary.chmod(0o755)
        self._saved_env = {
            k: os.environ.get(k)
            for k in ("EVO_HOOK_DRAIN_BINARY", "CODEX_HOME",
                      "CLAUDE_CONFIG_DIR", "EVO_HOME")
        }
        os.environ["EVO_HOOK_DRAIN_BINARY"] = str(fake_binary)
        os.environ["EVO_HOME"] = str(self.root / ".evo")

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()


class _CodexSandbox(_SandboxBase):

    def setUp(self):
        super().setUp()
        self.codex_home = self.root / ".codex"
        os.environ["CODEX_HOME"] = str(self.codex_home)
        # Minimal marketplace snapshot, as `codex plugin marketplace add`
        # leaves it: git tree contents, no binary in bin/.
        self.snapshot = self.codex_home / ".tmp" / "marketplaces" / "evo-hq"
        plugin = self.snapshot / "plugins" / "evo"
        (self.snapshot / ".claude-plugin").mkdir(parents=True)
        (self.snapshot / ".claude-plugin" / "marketplace.json").write_text(
            json.dumps({"name": "evo-hq-evo", "owner": {"name": "evo-hq"}})
        )
        (plugin / ".codex-plugin").mkdir(parents=True)
        (plugin / ".codex-plugin" / "plugin.json").write_text(
            json.dumps({"name": "evo", "version": "9.9.9"})
        )
        (plugin / "bin").mkdir()
        (plugin / "bin" / "evo").write_text("#!/bin/sh\n")
        (plugin / "hooks").mkdir()
        (plugin / "hooks" / "hooks.json").write_text(json.dumps({
            "hooks": {
                "PreToolUse": [{
                    "matcher": ".*",
                    "hooks": [{
                        "type": "command",
                        "command": "${CLAUDE_PLUGIN_ROOT}/bin/evo-hook-drain",
                    }],
                }],
                "SessionStart": [{
                    "hooks": [{
                        "type": "command",
                        "command": "${CLAUDE_PLUGIN_ROOT}/bin/evo-hook-drain",
                    }],
                }],
                "PostToolUse": [{
                    "matcher": "Bash",
                    "hooks": [{
                        "type": "command",
                        "command": "bash ${CLAUDE_PLUGIN_ROOT}/hooks/wait_hint.sh",
                    }],
                }],
            }
        }))
        (self.codex_home / "config.toml").write_text(
            "[features]\nplugin_hooks = true\n"
        )

    def _config(self) -> str:
        return (self.codex_home / "config.toml").read_text()

    def _cache_plugin_dir(self) -> Path:
        return self.codex_home / "plugins" / "cache" / "evo-hq" / "evo" / "9.9.9"


class TestCodexRestageSurvival(_CodexSandbox):

    def test_install_stages_binary_into_cache_and_snapshot(self):
        rc = codex._install_via_filecopy(None)
        self.assertEqual(rc, 0)
        self.assertTrue((self._cache_plugin_dir() / "bin" / HOOK_NAME).exists())
        self.assertTrue(
            (self.snapshot / "plugins" / "evo" / "bin" / HOOK_NAME).exists()
        )

    def test_install_materializes_codex_hooks_in_cache_and_snapshot(self):
        rc = codex._install_via_filecopy(None)
        self.assertEqual(rc, 0)

        for hooks_json in (
            self._cache_plugin_dir() / "hooks" / "hooks.json",
            self.snapshot / "plugins" / "evo" / "hooks" / "hooks.json",
        ):
            text = hooks_json.read_text()
            self.assertNotIn("wait_hint.sh", text)
            self.assertNotIn("evo-wait-hint", text)
            data = json.loads(text)
            commands = [
                handler["command"]
                for groups in data["hooks"].values()
                for group in groups
                for handler in group.get("hooks", [])
                if handler.get("type") == "command"
            ]
            self.assertTrue(commands)
            for command in commands:
                self.assertIn("node -e", command)
                self.assertIn("Buffer.from", command)

    @unittest.skipIf(sys.platform == "win32", "shell-script smoke is posix-only")
    def test_materialized_hook_runs_without_claude_plugin_root(self):
        import subprocess

        self.assertEqual(codex._install_via_filecopy(None), 0)
        stable = self.root / ".evo" / "bin" / HOOK_NAME
        stable.write_text("#!/bin/sh\ncat >/dev/null\nprintf '{}'\n")
        stable.chmod(0o755)

        hooks = json.loads(
            (self._cache_plugin_dir() / "hooks" / "hooks.json").read_text()
        )
        command = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        env = os.environ.copy()
        env.pop("CLAUDE_PLUGIN_ROOT", None)
        result = subprocess.run(
            command,
            input=b'{"hook_event_name":"SessionStart","session_id":"s"}',
            capture_output=True,
            env=env,
            shell=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), b"{}")

    def test_materialized_hook_fails_open_when_stable_binary_is_missing(self):
        import subprocess

        self.assertEqual(codex._install_via_filecopy(None), 0)
        stable = self.root / ".evo" / "bin" / HOOK_NAME
        stable.unlink()

        hooks = json.loads(
            (self._cache_plugin_dir() / "hooks" / "hooks.json").read_text()
        )
        command = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        env = os.environ.copy()
        env.pop("CLAUDE_PLUGIN_ROOT", None)
        result = subprocess.run(
            command,
            input=b'{"hook_event_name":"SessionStart","session_id":"s"}',
            capture_output=True,
            env=env,
            shell=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), b"{}")

    @unittest.skipIf(sys.platform == "win32", "shell-script smoke is posix-only")
    def test_materialized_hook_preserves_real_hook_output(self):
        self.assertEqual(codex._install_via_filecopy(None), 0)
        stable = self.root / ".evo" / "bin" / HOOK_NAME
        stable.write_text(
            "#!/bin/sh\n"
            "cat >/dev/null\n"
            "printf '{\"decision\":\"block\",\"reason\":\"evo-test\"}'\n"
        )
        stable.chmod(0o755)

        hooks = json.loads(
            (self._cache_plugin_dir() / "hooks" / "hooks.json").read_text()
        )
        command = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        result = subprocess.run(
            command,
            input=b'{"hook_event_name":"SessionStart","session_id":"s"}',
            capture_output=True,
            env=os.environ.copy(),
            shell=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {"decision": "block", "reason": "evo-test"},
        )

    @unittest.skipIf(sys.platform == "win32", "shell-script smoke is posix-only")
    def test_materialized_hook_fails_open_when_stable_binary_is_killed(self):
        self.assertEqual(codex._install_via_filecopy(None), 0)
        stable = self.root / ".evo" / "bin" / HOOK_NAME
        stable.write_text("#!/bin/sh\ncat >/dev/null\nkill -9 $$\n")
        stable.chmod(0o755)

        hooks = json.loads(
            (self._cache_plugin_dir() / "hooks" / "hooks.json").read_text()
        )
        command = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        result = subprocess.run(
            command,
            input=b'{"hook_event_name":"SessionStart","session_id":"s"}',
            capture_output=True,
            env=os.environ.copy(),
            shell=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), b"{}")

    def test_binary_survives_codex_restage(self):
        """The regression: codex wipes the cache dir and re-copies the
        snapshot. With the binary mirrored into the snapshot, the
        re-staged cache still has it."""
        self.assertEqual(codex._install_via_filecopy(None), 0)
        cache_dir = self._cache_plugin_dir()
        shutil.rmtree(cache_dir)
        shutil.copytree(self.snapshot / "plugins" / "evo", cache_dir)
        restaged = cache_dir / "bin" / HOOK_NAME
        self.assertTrue(
            restaged.exists(),
            "codex re-stage from the snapshot dropped evo-hook-drain "
            "(hooks would exit 127)",
        )
        self.assertTrue(os.access(restaged, os.X_OK))
        text = (cache_dir / "hooks" / "hooks.json").read_text()
        self.assertNotIn("CLAUDE_PLUGIN_ROOT", text)

    @unittest.skipIf(sys.platform == "win32", "shell-script smoke is posix-only")
    def test_upgrade_repairs_previous_codex_install_after_host_restage(self):
        """User path: an older evo install is present, Codex re-stages its
        cache from that old marketplace snapshot, then the new installer
        must repair both hook commands and the snapshot for future starts."""
        stale_cache = self._cache_plugin_dir()
        shutil.copytree(self.snapshot / "plugins" / "evo", stale_cache)
        self.assertIn(
            "CLAUDE_PLUGIN_ROOT",
            (stale_cache / "hooks" / "hooks.json").read_text(),
        )

        self.assertEqual(codex._install_via_filecopy(None), 0)
        stable = self.root / ".evo" / "bin" / HOOK_NAME
        stable.write_text("#!/bin/sh\ncat >/dev/null\nprintf '{}'\n")
        stable.chmod(0o755)

        for hooks_json in (
            self._cache_plugin_dir() / "hooks" / "hooks.json",
            self.snapshot / "plugins" / "evo" / "hooks" / "hooks.json",
        ):
            text = hooks_json.read_text()
            self.assertNotIn("CLAUDE_PLUGIN_ROOT", text)
            self.assertNotIn("wait_hint.sh", text)

        hooks = json.loads((self._cache_plugin_dir() / "hooks" / "hooks.json").read_text())
        command = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        env = os.environ.copy()
        env.pop("CLAUDE_PLUGIN_ROOT", None)
        result = subprocess.run(
            command,
            input=b'{"hook_event_name":"SessionStart","session_id":"upgrade"}',
            capture_output=True,
            env=env,
            shell=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), b"{}")

    def test_doctor_warns_when_snapshot_binary_missing(self):
        import argparse
        import contextlib
        import io

        self.assertEqual(codex._install_via_filecopy(None), 0)
        (self.snapshot / "plugins" / "evo" / "bin" / HOOK_NAME).unlink()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = codex.doctor(argparse.Namespace())
        self.assertIn("not mirrored in the marketplace snapshot", out.getvalue())
        # Latent issue only: doctor must still pass so `evo update`
        # (which skips unhealthy hosts) can re-mirror it.
        self.assertEqual(rc, 0)


class TestCodexMarketplaceRefresh(_CodexSandbox):
    """Version-pinned Codex installs must refresh the marketplace snapshot.

    Codex rejects `marketplace add evo-hq/evo@new-ref` when `evo-hq` is
    already registered to another ref. The installer must remove/re-add and
    must not proceed against the stale snapshot.
    """

    def _with_fake_codex_on_path(self):
        fake_bin = self.root / "fake-path-bin"
        fake_bin.mkdir(exist_ok=True)
        if sys.platform == "win32":
            (fake_bin / "codex.cmd").write_text("@echo off\r\nexit /b 0\r\n")
        else:
            stub = fake_bin / "codex"
            stub.write_text("#!/bin/sh\nexit 0\n")
            stub.chmod(0o755)
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{fake_bin}{os.pathsep}{old_path}"
        return old_path

    def test_version_pin_retries_after_existing_marketplace_source(self):
        import argparse
        from unittest import mock

        target = "0.6.1-alpha.2"
        plugin_json = (
            self.snapshot / "plugins" / "evo" / ".codex-plugin" / "plugin.json"
        )
        plugin_json.write_text(json.dumps({"name": "evo", "version": "0.6.0"}))
        old_path = self._with_fake_codex_on_path()
        calls = []
        add_attempts = 0

        def fake_call(cmd):
            nonlocal add_attempts
            calls.append(cmd)
            if cmd[:4] == ["codex", "plugin", "marketplace", "remove"]:
                return 0
            if cmd[:4] == ["codex", "plugin", "marketplace", "add"]:
                add_attempts += 1
                if add_attempts == 1:
                    return 1
                plugin_json.write_text(json.dumps({"name": "evo", "version": target}))
                return 0
            return 0

        try:
            with mock.patch("subprocess.call", side_effect=fake_call):
                rc = codex.install(argparse.Namespace(
                    from_path=None,
                    trust_hooks=True,
                    force=False,
                    version=target,
                ))
        finally:
            os.environ["PATH"] = old_path

        self.assertEqual(rc, 0)
        self.assertEqual(
            [c[:4] for c in calls],
            [
                ["codex", "plugin", "marketplace", "add"],
                ["codex", "plugin", "marketplace", "remove"],
                ["codex", "plugin", "marketplace", "add"],
            ],
        )
        cache_hooks = (
            self.codex_home / "plugins" / "cache" / "evo-hq" / "evo" /
            target / "hooks" / "hooks.json"
        )
        self.assertTrue(cache_hooks.exists())
        self.assertNotIn("CLAUDE_PLUGIN_ROOT", cache_hooks.read_text())

    def test_non_force_install_repairs_config_without_refreshing_marketplace(self):
        import argparse
        from unittest import mock

        # Mirrors a real broken user config: duplicate canonical sections plus
        # Codex-managed enabled keys inside hook-state tables. TOML parsing fails
        # before Codex can start until these are collapsed to one canonical block.
        (self.codex_home / "config.toml").write_text(
            '[features]\n'
            'plugin_hooks = true\n'
            '\n'
            '[plugins."evo@evo-hq"]\n'
            'enabled = false\n'
            '\n'
            '[hooks.state."evo@evo-hq:hooks/hooks.json:pre_tool_use:0:0"]\n'
            'enabled = false\n'
            'trusted_hash = "sha256:old"\n'
            '\n'
            '[plugins."github@openai-curated"]\n'
            'enabled = true\n'
            '\n'
            '[plugins."evo@evo-hq"]\n'
            'enabled = true\n'
            '\n'
            '[hooks.state."evo@evo-hq:hooks/hooks.json:pre_tool_use:0:0"]\n'
            'enabled = true\n'
            'trusted_hash = "sha256:also-old"\n'
        )
        old_path = self._with_fake_codex_on_path()
        try:
            with mock.patch("subprocess.call") as call:
                rc = codex.install(argparse.Namespace(
                    from_path=None,
                    trust_hooks=True,
                    force=False,
                    version=None,
                ))
        finally:
            os.environ["PATH"] = old_path

        self.assertEqual(rc, 0)
        call.assert_not_called()
        cfg = self._config()
        self.assertEqual(cfg.count('[plugins."evo@evo-hq"]'), 1)
        self.assertIn('[plugins."evo@evo-hq"]\nenabled = true', cfg)
        self.assertEqual(
            cfg.count('[hooks.state."evo@evo-hq:hooks/hooks.json:pre_tool_use:0:0"]'),
            1,
        )
        self.assertNotIn('trusted_hash = "sha256:old"', cfg)
        self.assertNotIn('trusted_hash = "sha256:also-old"', cfg)
        self.assertIn('[plugins."github@openai-curated"]', cfg)

    def test_version_pin_fails_closed_when_snapshot_stays_stale(self):
        import argparse
        from unittest import mock

        plugin_json = (
            self.snapshot / "plugins" / "evo" / ".codex-plugin" / "plugin.json"
        )
        plugin_json.write_text(json.dumps({"name": "evo", "version": "0.6.0"}))
        old_path = self._with_fake_codex_on_path()

        try:
            with mock.patch("subprocess.call", return_value=0):
                rc = codex.install(argparse.Namespace(
                    from_path=None,
                    trust_hooks=True,
                    force=False,
                    version="0.6.1-alpha.2",
                ))
        finally:
            os.environ["PATH"] = old_path

        self.assertEqual(rc, 2)
        self.assertFalse(
            (self.codex_home / "plugins" / "cache" / "evo-hq" / "evo" /
             "0.6.0").exists()
        )


class TestWrapperFallback(_SandboxBase):
    """The committed bin/evo-hook-drain wrapper keeps hooks working when
    a host re-stage replaces the staged binary with the git tree's
    contents."""

    @unittest.skipIf(sys.platform == "win32", "sh wrapper is posix-only")
    def test_wrapper_execs_stable_copy(self):
        import subprocess
        stable = self.root / ".evo" / "bin" / "evo-hook-drain"
        stable.parent.mkdir(parents=True)
        stable.write_text("#!/bin/sh\necho from-stable\n")
        stable.chmod(0o755)
        r = subprocess.run(
            [str(REPO_WRAPPER)], input=b"{}", capture_output=True,
            env=os.environ.copy(), timeout=10,
        )
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), b"from-stable")

    @unittest.skipIf(sys.platform == "win32", "sh wrapper is posix-only")
    def test_wrapper_noops_with_hint_when_stable_missing(self):
        import subprocess
        r = subprocess.run(
            [str(REPO_WRAPPER)], input=b"{}", capture_output=True,
            env=os.environ.copy(), timeout=10,
        )
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), b"{}")
        self.assertIn(b"binary not staged", r.stderr)

    @unittest.skipIf(sys.platform == "win32", "shell-script smoke is posix-only")
    def test_committed_hooks_do_not_require_claude_plugin_root(self):
        """Codex may refresh its cache from the raw marketplace source after
        `evo install codex` has already materialized hooks. The committed hook
        command must still work when CLAUDE_PLUGIN_ROOT is absent."""
        import subprocess
        stable = self.root / ".evo" / "bin" / "evo-hook-drain"
        stable.parent.mkdir(parents=True)
        stable.write_text("#!/bin/sh\ncat >/dev/null\nprintf '{}'\n")
        stable.chmod(0o755)

        hooks = json.loads((REPO_ROOT / "plugins" / "evo" / "hooks" / "hooks.json").read_text())
        command = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertIn("node -e", command)
        self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", command)
        env = os.environ.copy()
        env.pop("CLAUDE_PLUGIN_ROOT", None)
        r = subprocess.run(
            command,
            input=b'{"hook_event_name":"SessionStart","session_id":"s"}',
            capture_output=True,
            env=env,
            shell=True,
            timeout=10,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), b"{}")

    @unittest.skipIf(sys.platform == "win32", "shell-script smoke is posix-only")
    def test_committed_wait_hint_hook_is_safe_without_claude_plugin_root(self):
        import subprocess

        hooks = json.loads((REPO_ROOT / "plugins" / "evo" / "hooks" / "hooks.json").read_text())
        command = hooks["hooks"]["PostToolUse"][1]["hooks"][0]["command"]
        self.assertIn("evo-wait-hint", command)
        self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", command)
        env = os.environ.copy()
        env.pop("CLAUDE_PLUGIN_ROOT", None)
        r = subprocess.run(
            command,
            input=b'{"hook_event_name":"PostToolUse","tool_name":"Bash","tool_input":{"command":"python train.py"}}',
            capture_output=True,
            env=env,
            shell=True,
            timeout=10,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), b"{}")

    @unittest.skipIf(sys.platform == "win32", "shell-script smoke is posix-only")
    def test_committed_wait_hint_hook_runs_for_claude_plugin_root(self):
        import subprocess

        plugin_root = self.root / "plugin"
        hooks_dir = plugin_root / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "wait_hint.sh").write_text(
            "#!/bin/sh\n"
            "cat >/dev/null\n"
            "printf '[evo-hint] test\\n'\n"
        )
        (hooks_dir / "wait_hint.sh").chmod(0o755)

        hooks = json.loads((REPO_ROOT / "plugins" / "evo" / "hooks" / "hooks.json").read_text())
        command = hooks["hooks"]["PostToolUse"][1]["hooks"][0]["command"]
        env = os.environ.copy()
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
        r = subprocess.run(
            command,
            input=b'{"hook_event_name":"PostToolUse","tool_name":"Bash","tool_input":{"command":"python train.py"}}',
            capture_output=True,
            env=env,
            shell=True,
            timeout=10,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), b"[evo-hint] test")

    @unittest.skipIf(sys.platform == "win32", "shell-script smoke is posix-only")
    def test_committed_hooks_preserve_real_hook_output(self):
        import subprocess
        stable = self.root / ".evo" / "bin" / "evo-hook-drain"
        stable.parent.mkdir(parents=True)
        stable.write_text(
            "#!/bin/sh\n"
            "cat >/dev/null\n"
            "printf '{\"hookSpecificOutput\":{\"permissionDecision\":\"deny\"}}'\n"
        )
        stable.chmod(0o755)

        hooks = json.loads((REPO_ROOT / "plugins" / "evo" / "hooks" / "hooks.json").read_text())
        command = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        env = os.environ.copy()
        env.pop("CLAUDE_PLUGIN_ROOT", None)
        r = subprocess.run(
            command,
            input=b'{"hook_event_name":"SessionStart","session_id":"s"}',
            capture_output=True,
            env=env,
            shell=True,
            timeout=10,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            json.loads(r.stdout),
            {"hookSpecificOutput": {"permissionDecision": "deny"}},
        )

    @unittest.skipIf(sys.platform == "win32", "shell-script smoke is posix-only")
    def test_committed_hooks_fail_open_when_stable_binary_is_killed(self):
        import subprocess
        stable = self.root / ".evo" / "bin" / "evo-hook-drain"
        stable.parent.mkdir(parents=True)
        stable.write_text("#!/bin/sh\ncat >/dev/null\nkill -9 $$\n")
        stable.chmod(0o755)

        hooks = json.loads((REPO_ROOT / "plugins" / "evo" / "hooks" / "hooks.json").read_text())
        command = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        env = os.environ.copy()
        env.pop("CLAUDE_PLUGIN_ROOT", None)
        r = subprocess.run(
            command,
            input=b'{"hook_event_name":"SessionStart","session_id":"s"}',
            capture_output=True,
            env=env,
            shell=True,
            timeout=10,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), b"{}")


@unittest.skipIf(sys.platform == "win32", "sh wrapper is posix-only")
class TestWrapperPreservation(_CodexSandbox):
    """Snapshot refreshes restore the tracked wrapper; installs and
    doctor must treat it as a working entry point."""

    def _snapshot_hook(self) -> Path:
        return self.snapshot / "plugins" / "evo" / "bin" / "evo-hook-drain"

    def test_restage_after_snapshot_refresh_keeps_working_entry(self):
        shutil.copy2(REPO_WRAPPER, self._snapshot_hook())
        self.assertEqual(codex._install_via_filecopy(None), 0)
        cache_hook = self._cache_plugin_dir() / "bin" / HOOK_NAME
        # Install replaces the wrapper with the native binary in the cache.
        self.assertFalse(is_wrapper_script(cache_hook))
        # Codex refreshes the snapshot from git (tracked wrapper restored,
        # mirrored binary gone), then re-stages the cache from it.
        shutil.copy2(REPO_WRAPPER, self._snapshot_hook())
        shutil.rmtree(self._cache_plugin_dir())
        shutil.copytree(
            self.snapshot / "plugins" / "evo", self._cache_plugin_dir()
        )
        self.assertTrue(is_wrapper_script(cache_hook))
        self.assertTrue(os.access(cache_hook, os.X_OK))
        # Stable copy still exists, so doctor accepts the wrapper.
        import argparse
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = codex.doctor(argparse.Namespace())
        self.assertEqual(rc, 0)
        self.assertIn("fallback wrapper", out.getvalue())

    def test_doctor_fails_when_wrapper_has_no_stable_copy(self):
        shutil.copy2(REPO_WRAPPER, self._snapshot_hook())
        self.assertEqual(codex._install_via_filecopy(None), 0)
        # Re-stage from a refreshed snapshot, then lose the stable copy.
        shutil.copy2(REPO_WRAPPER, self._snapshot_hook())
        shutil.rmtree(self._cache_plugin_dir())
        shutil.copytree(
            self.snapshot / "plugins" / "evo", self._cache_plugin_dir()
        )
        shutil.rmtree(self.root / ".evo")
        import argparse
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = codex.doctor(argparse.Namespace())
        self.assertEqual(rc, 1)
        self.assertIn("no stable binary", out.getvalue())

    def test_from_path_install_keeps_wrapper_in_source_tree(self):
        shutil.copy2(REPO_WRAPPER, self._snapshot_hook())
        self.assertEqual(
            codex._install_via_filecopy(str(self.snapshot)), 0
        )
        # The "source tree" (from_path) keeps its tracked wrapper; only
        # the evo-managed cache gets the native binary.
        self.assertTrue(is_wrapper_script(self._snapshot_hook()))
        self.assertFalse(
            is_wrapper_script(self._cache_plugin_dir() / "bin" / HOOK_NAME)
        )


class TestCodexTrustHooks(_CodexSandbox):
    """Hooks are trusted by default at install; --no-trust-hooks defers
    to codex's `/hooks` review; doctor verifies the trust state."""

    def _doctor(self) -> tuple[int, str]:
        import argparse
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = codex.doctor(argparse.Namespace())
        return rc, out.getvalue()

    def test_install_trusts_hooks_by_default(self):
        self.assertEqual(codex._install_via_filecopy(None), 0)
        cfg = self._config()
        self.assertIn(
            '[hooks.state."evo@evo-hq:hooks/hooks.json:pre_tool_use:0:0"]', cfg
        )
        self.assertIn(
            '[hooks.state."evo@evo-hq:hooks/hooks.json:session_start:0:0"]', cfg
        )

    def test_no_trust_hooks_leaves_untrusted(self):
        self.assertEqual(
            codex._install_via_filecopy(None, trust_hooks=False), 0
        )
        self.assertNotIn("[hooks.state.", self._config())

    def test_doctor_passes_when_trusted(self):
        self.assertEqual(codex._install_via_filecopy(None), 0)
        rc, out = self._doctor()
        self.assertEqual(rc, 0)
        self.assertIn("2 hooks trusted for evo@evo-hq", out)

    def test_doctor_warns_when_untrusted(self):
        """Deliberate --no-trust-hooks installs await the user's /hooks
        review; doctor surfaces it without failing."""
        self.assertEqual(
            codex._install_via_filecopy(None, trust_hooks=False), 0
        )
        rc, out = self._doctor()
        self.assertEqual(rc, 0)
        self.assertIn("untrusted for evo@evo-hq", out)

    def test_doctor_fails_on_stale_trust(self):
        """hooks.json changed after trust was written: hashes no longer
        match, the hooks silently never fire, doctor must fail."""
        import re
        self.assertEqual(codex._install_via_filecopy(None), 0)
        cfg_path = self.codex_home / "config.toml"
        cfg_path.write_text(re.sub(
            r'trusted_hash = "sha256:[a-f0-9]{8}',
            'trusted_hash = "sha256:00000000',
            cfg_path.read_text(),
            count=1,
        ))
        rc, out = self._doctor()
        self.assertEqual(rc, 1)
        self.assertIn("hook trust is stale", out)


class TestInstallRunsDoctor(_CodexSandbox):
    """`evo install <host>` finishes with the host's doctor so a broken
    install is visible immediately instead of at hook-fire time."""

    def setUp(self):
        super().setUp()
        # codex.install gates on `codex` being on PATH but never invokes
        # it for --from-path installs (marketplace add is skipped). CI
        # runners have no codex CLI; satisfy the gate with a stub.
        fake_bin = self.root / "fake-path-bin"
        fake_bin.mkdir()
        if sys.platform == "win32":
            (fake_bin / "codex.cmd").write_text("@echo off\r\nexit /b 0\r\n")
        else:
            stub = fake_bin / "codex"
            stub.write_text("#!/bin/sh\nexit 0\n")
            stub.chmod(0o755)
        self._saved_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{fake_bin}{os.pathsep}{self._saved_path}"

    def tearDown(self):
        os.environ["PATH"] = self._saved_path
        super().tearDown()

    def test_install_runs_doctor_and_returns_its_rc(self):
        import argparse
        import contextlib
        import io

        from evo import host_install

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = host_install.install(
                "codex",
                argparse.Namespace(
                    from_path=str(self.snapshot), trust_hooks=True
                ),
            )
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("verifying: evo doctor codex", text)
        self.assertIn("evo-hook-drain present + executable", text)


class TestClaudeCodeCloneMirror(_SandboxBase):

    def setUp(self):
        super().setUp()
        self.claude_home = self.root / ".claude"
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.claude_home)
        self.cache_plugin = (
            self.claude_home / "plugins" / "cache" / "evo-hq-evo" / "evo" / "9.9.9"
        )
        (self.cache_plugin / "bin").mkdir(parents=True)
        self.clone_plugin = (
            self.claude_home / "plugins" / "marketplaces" / "evo-hq-evo"
            / "plugins" / "evo"
        )
        (self.clone_plugin / "bin").mkdir(parents=True)

    def test_stage_mirrors_into_marketplace_clone(self):
        claude_code._stage_hook_drain(None)
        self.assertTrue((self.cache_plugin / "bin" / HOOK_NAME).exists())
        self.assertTrue((self.clone_plugin / "bin" / HOOK_NAME).exists())

    def test_binary_survives_claude_plugin_update_restage(self):
        """`claude plugin update` re-copies the clone over the cache."""
        claude_code._stage_hook_drain(None)
        shutil.rmtree(self.cache_plugin)
        shutil.copytree(self.clone_plugin, self.cache_plugin)
        self.assertTrue(
            (self.cache_plugin / "bin" / HOOK_NAME).exists(),
            "claude plugin update re-stage dropped evo-hook-drain",
        )

    def test_from_path_does_not_touch_clone(self):
        src_tree = self.root / "src-marketplace"
        (src_tree / "plugins" / "evo" / "bin").mkdir(parents=True)
        claude_code._stage_hook_drain(str(src_tree))
        self.assertTrue(
            (src_tree / "plugins" / "evo" / "bin" / HOOK_NAME).exists()
        )
        self.assertFalse((self.clone_plugin / "bin" / HOOK_NAME).exists())


class TestMirrorHelper(_SandboxBase):

    def test_missing_source_returns_false(self):
        src = self.root / "a"
        dst = self.root / "b"
        (src / "bin").mkdir(parents=True)
        self.assertFalse(mirror_hook_drain_binary(src, dst))

    def test_same_dir_is_noop_true(self):
        src = self.root / "a"
        (src / "bin").mkdir(parents=True)
        binary = src / "bin" / HOOK_NAME
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        self.assertTrue(mirror_hook_drain_binary(src, src))

    def test_copies_and_marks_executable(self):
        src = self.root / "a"
        dst = self.root / "b"
        (src / "bin").mkdir(parents=True)
        (src / "bin" / HOOK_NAME).write_text("#!/bin/sh\necho hi\n")
        self.assertTrue(mirror_hook_drain_binary(src, dst))
        out = dst / "bin" / HOOK_NAME
        self.assertTrue(out.exists())
        if sys.platform != "win32":
            self.assertTrue(os.access(out, os.X_OK))


if __name__ == "__main__":
    unittest.main()
