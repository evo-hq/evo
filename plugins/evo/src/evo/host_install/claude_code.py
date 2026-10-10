"""Claude Code install — drives `claude plugin` non-interactive subcommands.

The `claude` binary exposes `marketplace add`, `install`, `update`, and
`uninstall` as shell-driveable subcommands (verified in
tests/live/test_install_sandbox.py + test_release_smoke.py). So `evo install
claude-code`, `evo update claude-code`, and `evo uninstall claude-code` can
all run end-to-end without the user typing slash commands inside an
interactive Claude Code session.

The `--force` flag on update wipes the plugin cache before reinstall,
working around an upstream cache-invalidation bug
(anthropics/claude-code#14061) that leaves the cache stale after a normal
`/plugin update`. Without --force, the standard update path is used; with
--force, the path that's guaranteed to land fresh code is used.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from ._hook_drain import (
    ensure_hook_drain_binary,
    hook_drain_binary_name,
    mirror_hook_drain_binary,
)


_MARKETPLACE = "evo-hq/evo"
_PLUGIN = "evo@evo-hq-evo"
_MARKETPLACE_NAME = "evo-hq-evo"


def _claude_config_dir() -> Path:
    """Return the Claude Code config root. Honors `CLAUDE_CONFIG_DIR` (used
    by Claude Code to relocate state outside `~/.claude`, e.g. in cloud
    containers where the home dir is ephemeral and a persistent volume is
    mounted elsewhere). Falls back to `~/.claude`.

    Without this, every helper here that hardcodes `Path.home() / ".claude"`
    misses the plugin cache when `CLAUDE_CONFIG_DIR` is set -- silently
    skipping `ensure_hook_drain_binary` and breaking `evo direct` delivery.
    """
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(cfg) if cfg else Path.home() / ".claude"


def _ver_key(name: str):
    """Numeric-aware sort key so 0.10.0 sorts after 0.9.0 (plain sorted() is
    lexicographic and would invert that). Non-numeric segments (pre-release
    tags) fall into a separate rank so int/str never compare."""
    return [(0, int(s)) if s.isdigit() else (1, s)
            for s in re.split(r"[.-]", name)]


def _latest_cache_dir() -> Path | None:
    """Return the latest-version plugin cache dir, or None if not installed.
    Cache layout: <claude-config-dir>/plugins/cache/<mkt>/evo/<version>/
    """
    root = _claude_config_dir() / "plugins" / "cache" / _MARKETPLACE_NAME / "evo"
    if not root.exists():
        return None
    versions = sorted((p for p in root.iterdir() if p.is_dir()),
                      key=lambda p: _ver_key(p.name))
    return versions[-1] if versions else None


def _hook_drain_staging_dir(from_path: str | None) -> Path | None:
    """Return the plugin dir whose `bin/evo-hook-drain` the runtime hook will
    actually exercise, or None if it can't be located.

    A `--from-path` install runs the plugin straight from the source tree:
    `claude plugin marketplace add <from_path>` makes claude resolve
    `${CLAUDE_PLUGIN_ROOT}` to `<from_path>/plugins/evo`, NOT to the version
    cache. Staging into `_latest_cache_dir()` in that case drops the binary
    where no hook ever looks, so every hook fire is `not found` (exit 127).
    Target the source tree instead. A normal (cache-backed) install has no
    `from_path`, so it falls through to the version cache.
    """
    if from_path:
        src = Path(from_path)
        # from_path is the marketplace root (repo dir); the plugin lives at
        # plugins/evo within it. Tolerate being handed the plugin dir directly.
        candidate = src / "plugins" / "evo"
        if candidate.exists():
            return candidate
        if (src / "bin").exists() or (src / ".claude-plugin").exists():
            return src
        return candidate  # let ensure_* create bin/ under the expected path
    return _latest_cache_dir()


def _stage_hook_drain(from_path: str | None, *, force: bool = False) -> None:
    """Fetch the binary into the plugin dir the hooks resolve, then mirror
    it into the marketplace clone for cache-backed installs.

    `claude plugin update` re-copies the marketplace clone over the version
    cache; the clone is a git tree without the binary, so an update run
    outside `evo update` would otherwise drop it and hooks exit 127.
    """
    plugin_dir = _hook_drain_staging_dir(from_path)
    if plugin_dir is None:
        return
    # For --from-path the destination is the user's source tree: leave
    # the committed wrapper in place (it execs the stable copy) instead
    # of dirtying their checkout with a binary.
    ensure_hook_drain_binary(
        plugin_dir, force=force, overwrite_wrapper=not from_path
    )
    if not from_path:
        clone_dir = (
            _claude_config_dir() / "plugins" / "marketplaces"
            / _MARKETPLACE_NAME / "plugins" / "evo"
        )
        if clone_dir.is_dir():
            mirror_hook_drain_binary(plugin_dir, clone_dir)

# Strict release-tag shape: 'X.Y.Z' optionally followed by a pre-release
# suffix ('0.4.0-alpha.5', '0.4.0a5', '0.4.0rc1'). Only this shape gets
# auto-prefixed with 'v' to match the repo's release-tag convention.
# Everything else (branch names like 'main', commit SHAs, already-v-prefixed
# tags) passes through verbatim.
_RELEASE_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+([.\-+a-zA-Z0-9]*)$")


def _marketplace_source(version: str | None, from_path: str | None) -> str:
    """Return the source string for `claude plugin marketplace add`.

    Precedence: `--from-path` > `--version` > unpinned latest.
    `claude plugin marketplace add` accepts 'owner/repo@<ref>' where <ref>
    can be a tag, branch, or commit SHA (verified in tests/live/test_release_smoke.py).
    """
    if from_path:
        return from_path
    if not version:
        return _MARKETPLACE
    # Release-version shape → auto-prefix with 'v' (repo's tag convention).
    # Anything else (branch, sha, already-prefixed) passes through as-is.
    ref = f"v{version}" if _RELEASE_VERSION_RE.match(version) else version
    return f"{_MARKETPLACE}@{ref}"


def _looks_like_pypi_release(version: str) -> bool:
    """True iff `version` could plausibly resolve to an evo-hq-cli release
    on PyPI. Branches/SHAs are git-only refs that PyPI doesn't know about,
    so we don't try `uv tool install evo-hq-cli==<branch>` for them.
    """
    return bool(_RELEASE_VERSION_RE.match(version))


def _claude_bin_or_error() -> str | None:
    claude = shutil.which("claude")
    if claude is None:
        print(
            "ERROR: `claude` binary not on PATH. Install Claude Code first:\n"
            "  npm install -g @anthropic-ai/claude-code",
            file=sys.stderr,
        )
    return claude


def _run(cmd: list[str]) -> int:
    print(f"$ {' '.join(cmd)}")
    return subprocess.call(cmd)


def install(args: argparse.Namespace) -> int:
    """`claude plugin marketplace add` + `claude plugin install`.

    `from_path` (optional): local marketplace dir to install from instead of
    the GitHub repo. Useful for testing unreleased changes.
    `scope` (optional, default "user"): plugin scope.
    """
    if _claude_bin_or_error() is None:
        return 2
    source = _marketplace_source(
        getattr(args, "version", None),
        getattr(args, "from_path", None),
    )
    scope = getattr(args, "scope", None) or "user"
    rc = _run(["claude", "plugin", "marketplace", "add", source])
    if rc != 0:
        # Marketplace may already be added; try install anyway. claude reports
        # "already added" with non-zero in some versions — don't bail yet.
        print("(marketplace add returned non-zero; continuing to install)")
    rc = _run(["claude", "plugin", "install", _PLUGIN, "--scope", scope])
    if rc != 0:
        return rc
    # Stage the platform-native evo-hook-drain binary where the runtime hook
    # resolves it. hooks.json points at ${CLAUDE_PLUGIN_ROOT}/bin/evo-hook-drain;
    # for a --from-path install that's the source tree, not the version cache.
    _stage_hook_drain(
        getattr(args, "from_path", None),
        force=bool(getattr(args, "force", False)),
    )
    print(
        f"\n✓ evo installed for claude-code at scope={scope}.\n"
        "  If you're inside an active claude session, run `/reload-plugins` "
        "to pick up the changes."
    )
    return 0


def update(args: argparse.Namespace) -> int:
    """Refresh the marketplace clone, then update the installed plugin.

    With `force=True`: wipe the plugin cache and reinstall from scratch
    (workaround for anthropics/claude-code#14061, the upstream cache
    invalidation bug that leaves stale files after a normal update).
    """
    if _claude_bin_or_error() is None:
        return 2
    scope = getattr(args, "scope", None) or "user"
    force = bool(getattr(args, "force", False))
    version = getattr(args, "version", None)
    from_path = getattr(args, "from_path", None)

    # If pinning a specific version (or a local path), re-add the marketplace
    # at that ref. `claude plugin marketplace add` with an existing name is
    # idempotent + replaces the source on re-add (verified in live tests).
    if version or from_path:
        source = _marketplace_source(version, from_path)
        rc = _run(["claude", "plugin", "marketplace", "add", source])
        if rc != 0:
            print(f"(marketplace add returned non-zero for {source}; continuing)")
    else:
        # Refresh the marketplace clone — that's where the new version
        # metadata lives.
        rc = _run(["claude", "plugin", "marketplace", "update", _MARKETPLACE_NAME])
        if rc != 0:
            print("(marketplace update returned non-zero; continuing)")

    if force:
        # Sidestep the upstream cache-invalidation bug: wipe cache, reinstall.
        cache = _claude_config_dir() / "plugins" / "cache" / _MARKETPLACE_NAME
        if cache.exists():
            print(f"$ rm -rf {cache}")
            shutil.rmtree(cache)
        # Uninstall first so install registers cleanly. Ignore failure — the
        # plugin may not be installed at all in some recovery scenarios.
        _run(["claude", "plugin", "uninstall", _PLUGIN])
        rc = _run(["claude", "plugin", "install", _PLUGIN, "--scope", scope])
    else:
        rc = _run(["claude", "plugin", "update", _PLUGIN, "--scope", scope])

    if rc != 0:
        return rc
    # Re-stage the hook-drain binary where the runtime hook resolves it
    # (source tree for --from-path, version cache otherwise).
    # --force will re-download even if a binary's already there.
    _stage_hook_drain(from_path, force=force)
    print(
        "\n✓ evo updated for claude-code.\n"
        "  If you're inside an active claude session, run `/reload-plugins` "
        "to pick up the changes."
    )
    return 0


def uninstall(args: argparse.Namespace) -> int:
    if _claude_bin_or_error() is None:
        return 2
    return _run(["claude", "plugin", "uninstall", _PLUGIN])


def doctor(args: argparse.Namespace) -> int:
    import json

    home = _claude_config_dir()
    settings = home / "settings.json"
    if not settings.exists():
        print(f"✗ {settings} not found — Claude Code not installed?")
        return 1

    try:
        data = json.loads(settings.read_text())
    except json.JSONDecodeError as exc:
        print(f"✗ could not parse {settings}: {exc}")
        return 1

    enabled = data.get("enabledPlugins", {})
    marketplaces = data.get("extraKnownMarketplaces", {})
    rc = 0
    if _PLUGIN in enabled:
        print(f"✓ {_PLUGIN} enabled in claude settings")
    else:
        print(f"✗ {_PLUGIN} not in enabledPlugins")
        print("  Run: evo install claude-code")
        rc = 1

    src = marketplaces.get(_MARKETPLACE_NAME, {}).get("source", {})
    src_type = src.get("source")
    if src_type == "github":
        cache = home / "plugins" / "marketplaces" / _MARKETPLACE_NAME
        if cache.exists():
            print(f"✓ marketplace cached at {cache}")
            # Warning only (no rc bump): `evo update` skips hosts whose
            # doctor fails, and update is exactly what re-mirrors this.
            clone_binary = (
                cache / "plugins" / "evo" / "bin" / hook_drain_binary_name()
            )
            if not clone_binary.exists():
                print(
                    f"! evo-hook-drain not mirrored in the marketplace clone "
                    f"({clone_binary})\n"
                    f"  `claude plugin update` would drop the hook binary "
                    f"(hooks exit 127). Run: evo update claude-code"
                )
        else:
            print(f"✗ source=github but no cache at {cache} — try restarting claude")
            rc = 1
    elif src_type == "directory":
        path = Path(src.get("path", ""))
        if path.exists():
            print(f"✓ source=directory: {path} (CC reads directly, no cache)")
        else:
            print(f"✗ source=directory points at {path} which does not exist")
            rc = 1
    else:
        print(f"? unknown marketplace source type: {src_type}")

    # Detect cache-staleness (#35-shape): marketplace clone version vs cache.
    plugin_cache_root = home / "plugins" / "cache" / _MARKETPLACE_NAME / "evo"
    mkt_manifest = (
        home / "plugins" / "marketplaces" / _MARKETPLACE_NAME
        / "plugins" / "evo" / ".claude-plugin" / "plugin.json"
    )
    if plugin_cache_root.exists() and mkt_manifest.exists():
        installed_versions = sorted(
            (p.name for p in plugin_cache_root.iterdir() if p.is_dir()),
            key=_ver_key,
        )
        if installed_versions:
            installed = installed_versions[-1]
            try:
                mkt = json.loads(mkt_manifest.read_text())
                mkt_ver = mkt.get("version")
            except (OSError, json.JSONDecodeError):
                mkt_ver = None
            if mkt_ver and mkt_ver != installed:
                print(
                    f"✗ cache stale: installed={installed}, marketplace={mkt_ver}\n"
                    f"  Run: evo update claude-code --force"
                )
                rc = 1
            else:
                print(f"✓ cache up to date ({installed})")
    return rc
