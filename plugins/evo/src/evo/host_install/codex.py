"""Codex install — enables `plugin_hooks` and the evo plugin in
config.toml. Codex has no `plugin install` CLI command; activation is
done by adding a `[plugins."<name>@<marketplace>"]` section."""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
from pathlib import Path

from ._hook_drain import (
    ensure_hook_drain_binary,
    hook_drain_binary_name,
    is_wrapper_script,
    mirror_hook_drain_binary,
    stable_binary_path,
)


_PLUGIN_KEY = '[plugins."evo@evo-hq"]'

_INSTALL_HINT = """\
Codex install (run `codex plugin marketplace add evo-hq/evo` first if
not already done). This command:
  - sets [features] plugin_hooks = true  (required for `evo direct`)
  - adds [plugins."evo@evo-hq"]          (enables the plugin)
in ~/.codex/config.toml.
"""


def _stable_hook_command() -> str:
    """Codex-safe hook command.

    Codex does not guarantee Claude Code's `${CLAUDE_PLUGIN_ROOT}` env var.
    Materialize the user's stable evo hook binary path at install time so
    Codex hook execution does not depend on plugin-root expansion, cwd, or PATH.
    The Node wrapper deliberately fails open: hooks run in every Codex session,
    and a killed/corrupt helper binary must not break unrelated work.
    """
    stable_json = json.dumps(str(stable_binary_path().resolve()))
    script = (
        "const fs=require('fs'),cp=require('child_process');"
        "const input=fs.readFileSync(0);"
        f"const bin={stable_json};"
        "try{const r=cp.spawnSync(bin,[],{input});"
        "if(r.status===0&&r.stdout&&r.stdout.length){"
        "process.stdout.write(r.stdout);process.exit(0)}}catch(e){}"
        "process.stdout.write('{}\\n')"
    )
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    return f"node -e \"eval(Buffer.from('{encoded}','base64').toString())\""


def _materialize_codex_hooks(hooks_json_path: Path) -> bool:
    """Rewrite an installed Codex hooks.json to use machine-local commands.

    The source plugin's hooks.json is shared with Claude Code and intentionally
    keeps `${CLAUDE_PLUGIN_ROOT}`. Codex-installed copies get rewritten because
    recent Codex versions may run plugin hooks without that variable, turning
    `${CLAUDE_PLUGIN_ROOT}/bin/evo-hook-drain` into `/bin/evo-hook-drain`
    (exit 127).
    """
    try:
        data = json.loads(hooks_json_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False

    changed = False
    drain_cmd = _stable_hook_command()
    hooks = data.get("hooks") or {}
    for event_name, groups in list(hooks.items()):
        updated_groups = []
        for group in groups or []:
            handlers = []
            for handler in group.get("hooks", []) or []:
                if handler.get("type") != "command":
                    handlers.append(handler)
                    continue
                cmd = str(handler.get("command") or "")
                if "evo-hook-drain" in cmd:
                    if cmd != drain_cmd:
                        handler = {**handler, "command": drain_cmd}
                        changed = True
                    handlers.append(handler)
                elif "wait_hint.sh" in cmd or "evo-wait-hint" in cmd:
                    # Optional UX hint. Keeping a Claude-only path here makes
                    # Codex emit hook failures, which is worse than omitting it.
                    changed = True
                    continue
                else:
                    handlers.append(handler)
            group["hooks"] = handlers
            if handlers:
                updated_groups.append(group)
            else:
                changed = True
        hooks[event_name] = updated_groups

    if changed:
        hooks_json_path.write_text(json.dumps(data, indent=2) + "\n")
    return changed


def _codex_base() -> Path:
    home_override = os.environ.get("CODEX_HOME")
    return Path(home_override) if home_override else Path.home() / ".codex"


def _marketplace_cache() -> Path:
    return _codex_base() / ".tmp" / "marketplaces" / "evo-hq"


def _marketplace_plugin_version(mkt_root: Path) -> str | None:
    manifest = mkt_root / "plugins" / "evo" / ".codex-plugin" / "plugin.json"
    try:
        data = json.loads(manifest.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    version = data.get("version")
    return str(version) if version else None


def _toggle_plugin_hooks(enable: bool) -> tuple[bool, Path]:
    cfg = _codex_base() / "config.toml"
    if not cfg.exists():
        return False, cfg
    text = cfg.read_text()
    has_features = "[features]" in text
    has_pluginhooks_true = "plugin_hooks = true" in text
    has_pluginhooks_false = "plugin_hooks = false" in text

    if enable:
        if has_pluginhooks_true:
            return False, cfg
        if has_pluginhooks_false:
            text = text.replace("plugin_hooks = false", "plugin_hooks = true")
        elif has_features:
            text = text.replace("[features]", "[features]\nplugin_hooks = true", 1)
        else:
            text = text.rstrip() + "\n\n[features]\nplugin_hooks = true\n"
    else:
        if not has_pluginhooks_true:
            return False, cfg
        text = text.replace("plugin_hooks = true", "plugin_hooks = false")

    cfg.write_text(text)
    return True, cfg


def _strip_toml_sections(text: str, should_strip) -> str:
    """Remove whole TOML sections whose header matches ``should_strip``.

    The Codex config is user-owned, so keep this deliberately text-based and
    narrow: when a matching table header is seen, drop that header and every
    following line until the next table header.
    """
    new_lines: list[str] = []
    skip = False
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("["):
            header = stripped.rstrip()
            skip = bool(should_strip(header))
            if skip:
                continue
        if skip:
            continue
        new_lines.append(line)
    return "".join(new_lines)


def _enable_plugin(enable: bool) -> tuple[bool, Path]:
    """Add or remove the `[plugins."evo@evo-hq"]` section in
    config.toml. Codex 0.130+ uses owner-only marketplace names, so the
    plugin key is `evo@evo-hq` (not `evo@evo-hq-evo`)."""
    cfg = _codex_base() / "config.toml"
    if not cfg.exists() and not enable:
        return False, cfg
    text = cfg.read_text() if cfg.exists() else ""

    stripped = _strip_toml_sections(text, lambda header: header == _PLUGIN_KEY)
    if enable:
        text = stripped.rstrip() + f"\n\n{_PLUGIN_KEY}\nenabled = true\n"
    else:
        if stripped == text:
            return False, cfg
        text = stripped.rstrip() + "\n"

    if text == (cfg.read_text() if cfg.exists() else ""):
        return False, cfg
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(text)
    return True, cfg


def install(args: argparse.Namespace) -> int:
    print(_INSTALL_HINT)

    import shutil as _shutil
    if _shutil.which("codex") is None:
        print(
            "ERROR: `codex` binary not on PATH. Install Codex first:\n"
            "  npm install -g @openai/codex",
            file=__import__("sys").stderr,
        )
        return 2

    from_path = getattr(args, "from_path", None)
    trust_hooks = bool(getattr(args, "trust_hooks", True))
    force = bool(getattr(args, "force", False))

    # Drive `codex plugin marketplace add` automatically. Skip if:
    #   - --from-path is set (user is testing a local marketplace)
    #   - marketplace clone already exists and --force not set (avoids
    #     overwriting a tag-pinned install with unpinned default-branch
    #     content; e.g. release-smoke tests do their own tag-pinned
    #     marketplace add and would lose the pin if we re-added unpinned)
    mkt_cache = _marketplace_cache()
    if from_path:
        pass
    elif mkt_cache.exists() and not force and not getattr(args, "version", None):
        print(
            f"codex marketplace cache already at {mkt_cache}; "
            "skipping `codex plugin marketplace add` prereq (use --force to refresh)"
        )
    else:
        import subprocess as _sp
        import sys as _sys
        version = getattr(args, "version", None)
        source = "evo-hq/evo"
        if version:
            import re as _re
            ref = f"v{version}" if _re.match(r"^\d+\.\d+\.\d+", version) else version
            source = f"{source}@{ref}"
        mkt_cmd = ["codex", "plugin", "marketplace", "add", source]
        print(f"$ {' '.join(mkt_cmd)}")
        rc = _sp.call(mkt_cmd)
        if rc != 0 and (force or version):
            rm_cmd = ["codex", "plugin", "marketplace", "remove", "evo-hq"]
            print(f"$ {' '.join(rm_cmd)}")
            _sp.call(rm_cmd)
            print(f"$ {' '.join(mkt_cmd)}")
            rc = _sp.call(mkt_cmd)
        if rc != 0:
            print(
                f"ERROR: failed to add Codex marketplace source {source!r}",
                file=_sys.stderr,
            )
            return 2
        if version:
            actual = _marketplace_plugin_version(mkt_cache)
            if actual != version:
                print(
                    "ERROR: Codex marketplace snapshot version mismatch: "
                    f"expected {version}, found {actual or 'unknown'} at {mkt_cache}",
                    file=_sys.stderr,
                )
                return 2

    # Ensure config.toml exists. On freshly-npm-installed codex (never run
    # interactively), the marketplace add above creates ~/.codex/ but may
    # not create config.toml until the first `codex` invocation. Write a
    # stub so the toggle/enable helpers below have something to edit.
    cfg = _codex_base() / "config.toml"
    if not cfg.exists():
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("# created by `evo install codex`\n")
        print(f"created stub {cfg}")

    # Replicate what codex's TUI `plugin/install` RPC does: copy the plugin
    # contents into `~/.codex/plugins/cache/<marketplace>/<plugin>/<ver>/`
    # and add `[plugins."<plugin>@<marketplace>"] enabled = true` to
    # config.toml. Without this, `codex plugin marketplace add` alone
    # leaves the plugin discoverable but inert — skills aren't in codex's
    # tool list, the hooks/hooks.json drain script never fires, and
    # `evo direct` directives go undelivered.
    #
    # The TUI uses an RPC method `plugin/install` (verified by inspecting
    # `codex app-server generate-json-schema`). We do not drive that RPC
    # because `codex app-server` exits early on missing bubblewrap inside
    # minimal containers (e2b sandboxes etc.). File-copy replicates the
    # same disk state and works anywhere.
    return _install_via_filecopy(from_path, trust_hooks=trust_hooks)


def _resolve_marketplace_json(from_path: str | None) -> Path | None:
    """Return the path to the marketplace.json file `plugin/install`
    needs. `from_path` (when set) can be either the marketplace root
    (contains `.claude-plugin/marketplace.json`) or a plugin dir
    (`<root>/plugins/evo/`). For the latter we walk up to the root.
    Falls back to the PyPI/GitHub cache otherwise.
    """
    if from_path:
        p = Path(from_path).resolve()
        # plugin-dir form: <root>/plugins/<name>
        if p.name and p.parent.name == "plugins":
            p = p.parent.parent
        mj = p / ".claude-plugin" / "marketplace.json"
        return mj if mj.exists() else None
    mj = _marketplace_cache() / ".claude-plugin" / "marketplace.json"
    return mj if mj.exists() else None


def _install_via_filecopy(from_path: str | None, *, trust_hooks: bool = True) -> int:
    """Mirror what codex's `plugin/install` RPC writes to disk:
      1. read marketplace name from `<root>/.claude-plugin/marketplace.json`
      2. read plugin version from `<plugin-root>/.codex-plugin/plugin.json`
      3. copy plugin contents to
         `~/.codex/plugins/cache/<marketplace>/<plugin>/<version>/`
      4. ensure `[plugins."<plugin>@<marketplace>"] enabled = true`
         is in config.toml

    If `trust_hooks` is True, additionally write
    `[hooks.state."<key>"] trusted_hash = "..."` entries that match what
    codex's TUI writes when the user trusts each hook via `/hooks`.
    Without trust, hooks remain in `untrusted` state and never fire —
    `evo direct` directives won't reach the agent.
    """
    import json as _json
    import shutil as _shutil
    import sys as _sys

    # Locate marketplace root + plugin root. `from_path` can be the
    # marketplace root (preferred) or a plugin dir (`<root>/plugins/<n>`).
    if from_path:
        p = Path(from_path).resolve()
        if p.name and p.parent.name == "plugins":
            mkt_root = p.parent.parent
        else:
            mkt_root = p
    else:
        mkt_root = _marketplace_cache()
    plugin_root = mkt_root / "plugins" / "evo"
    marketplace_json = mkt_root / ".claude-plugin" / "marketplace.json"
    codex_manifest = plugin_root / ".codex-plugin" / "plugin.json"

    for required, label in [
        (marketplace_json, "marketplace.json"),
        (codex_manifest, ".codex-plugin/plugin.json"),
    ]:
        if not required.exists():
            print(
                f"✗ missing {label}: {required}\n"
                "  PyPI/GitHub: run `codex plugin marketplace add evo-hq/evo`\n"
                "  Local mode:  pass `--from-path <marketplace-root>`",
                file=_sys.stderr,
            )
            return 2

    # Marketplace name: ALWAYS use marketplace.json's `"owner"."name"`
    # field (`evo-hq`). Codex 0.130+ names the marketplace after the repo
    # OWNER, so `codex plugin marketplace add evo-hq/evo` registers
    # `[plugins."evo@evo-hq"]` and resolves `${CLAUDE_PLUGIN_ROOT}` to
    # `cache/evo-hq/evo/<ver>/`. The cache dir + binary staging + plugin
    # registration here MUST land under that same name, or codex fires
    # hooks against a cache dir we never populated → exit 127 on every
    # hook event. (Using the top-level `"name"` field — `evo-hq-evo` —
    # staged the binary into a sibling cache dir codex never reads, which
    # is exactly that failure.) This matches `_PLUGIN_KEY` above.
    try:
        mkt = _json.loads(marketplace_json.read_text())
    except (OSError, _json.JSONDecodeError) as exc:
        print(f"✗ could not parse {marketplace_json}: {exc}", file=_sys.stderr)
        return 2
    mkt_name = (mkt.get("owner") or {}).get("name") or "evo-hq"
    try:
        manifest = _json.loads(codex_manifest.read_text())
        version = manifest.get("version") or "0.0.0"
    except (OSError, _json.JSONDecodeError) as exc:
        print(f"✗ could not parse {codex_manifest}: {exc}", file=_sys.stderr)
        return 2

    cache_dst = (_codex_base() / "plugins" / "cache" / mkt_name / "evo" / version)
    if cache_dst.exists():
        print(f"removing previous install at {cache_dst}")
        _shutil.rmtree(cache_dst)
    cache_dst.parent.mkdir(parents=True, exist_ok=True)
    print(f"copying {plugin_root} → {cache_dst}")
    _shutil.copytree(plugin_root, cache_dst, symlinks=False,
                     ignore=_shutil.ignore_patterns(
                         ".git", ".venv", "__pycache__", "build", "dist",
                         ".pytest_cache", "*.egg-info"))

    # Stage the platform-native evo-hook-drain binary. Codex hook commands
    # are materialized below to call the stable per-user binary directly,
    # avoiding host-managed plugin-root env vars.
    ensure_hook_drain_binary(cache_dst)

    # Mirror the binary into the marketplace snapshot so codex's next
    # cache re-stage carries the native binary instead of the tracked
    # wrapper. Best-effort: codex re-clones the snapshot from git at
    # session start, which removes the mirrored copy and restores the
    # wrapper (still a working entry point via the stable copy). Skipped
    # for --from-path: the source is the user's checkout and the binary
    # would dirty it.
    if not from_path:
        mirror_hook_drain_binary(cache_dst, plugin_root)

    nested_hooks = cache_dst / "hooks" / "hooks.json"
    if nested_hooks.exists() and _materialize_codex_hooks(nested_hooks):
        print(f"updated {nested_hooks}: materialized Codex hook commands")
    if not from_path:
        snapshot_hooks = plugin_root / "hooks" / "hooks.json"
        if snapshot_hooks.exists() and _materialize_codex_hooks(snapshot_hooks):
            print(f"updated {snapshot_hooks}: materialized Codex hook commands")

    # Update config.toml: ensure `[features] plugin_hooks = true`
    # (gates whether codex fires plugin hooks at all) AND
    # `[plugins."evo@<mkt>"] enabled = true` (activates the plugin).
    # Without plugin_hooks=true the hooks.json declaration is ignored
    # and `evo direct` directives never get drained.
    changed_h, cfg = _toggle_plugin_hooks(enable=True)
    if changed_h:
        print(f"updated {cfg}: enabled plugin_hooks")
    else:
        print(f"plugin_hooks already enabled in {cfg}")

    changed_p, cfg = _enable_plugin(enable=True)
    if changed_p:
        print(f"updated {cfg}: enabled [plugins.\"evo@{mkt_name}\"]")
    else:
        print(f"[plugins.\"evo@{mkt_name}\"] already enabled in {cfg}")

    # Hooks are codex's most security-sensitive surface (run on every
    # tool call). Codex installs them in `untrusted` state — they're
    # registered but won't fire. Two paths to enable:
    #   - Interactive: user opens `codex` → `/hooks` → trusts each
    #   - Headless:    pass `--trust-hooks` to this command
    if not nested_hooks.exists():
        print(
            "\n(no hooks/hooks.json in plugin — skill-only install complete)"
        )
        return 0
    if trust_hooks:
        _trust_plugin_hooks(nested_hooks, plugin_id=f"evo@{mkt_name}", cfg=cfg)
    else:
        if _strip_plugin_hook_state(cfg, f"evo@{mkt_name}"):
            print(f"updated {cfg}: removed stale hook trust for evo@{mkt_name}")
        print(
            "\nPlugin hooks installed UNTRUSTED (--no-trust-hooks). They "
            "register but never fire, so mid-run directives (`evo direct`) "
            "won't be delivered. To enable:\n"
            "  - Start `codex`, then `/hooks`, trust each evo hook, OR\n"
            "  - Re-run: evo install codex"
        )

    _cleanup_legacy_codex_registrations(target_mkt=mkt_name)
    return 0


def _cleanup_legacy_codex_registrations(target_mkt: str) -> None:
    """Remove leftover `evo@<other-mkt>` entries from codex's config.toml
    that don't match the canonical marketplace name (`evo-hq`, the repo
    owner — see `_install_via_filecopy`).

    Pre-0.4.0 installs registered the plugin under `evo-hq-evo`
    (marketplace.json's top-level `name`), while codex itself loads it
    under `evo@evo-hq` (owner-based naming, 0.130+). Users carrying both
    end up with two parallel `[plugins."evo@<X>"]` blocks: both enabled,
    both fire on every event, and the one whose cache dir never had the
    binary staged fails exit 127.

    This cleanup runs at the end of every codex install and removes:
      - `[plugins."evo@<other>"]` blocks and their `enabled` lines
      - `[hooks.state."evo@<other>:..."]` trust entries
      - `[marketplaces.<other>]` blocks ONLY when the corresponding
        evo@<other> plugin entry was also removed (other plugins may
        share that marketplace)
      - `~/.codex/plugins/cache/<other>/` cache directories
    """
    import re
    import shutil
    cfg_path = _codex_base() / "config.toml"
    if not cfg_path.exists():
        return
    text = cfg_path.read_text()

    plugin_header = re.compile(r'^\[plugins\."evo@([^"]+)"\]\s*$')
    hooks_header = re.compile(r'^\[hooks\.state\."evo@([^:]+):[^"]+"\]\s*$')
    mkt_header = re.compile(r'^\[marketplaces\.([^\]\s]+)\]\s*$')
    any_section = re.compile(r'^\[')

    lines = text.splitlines(keepends=True)
    legacy_mkts: set[str] = set()
    for line in lines:
        m = plugin_header.match(line.lstrip())
        if m and m.group(1) != target_mkt:
            legacy_mkts.add(m.group(1))
    if not legacy_mkts:
        return

    new_lines: list[str] = []
    skip = False
    for line in lines:
        stripped = line.lstrip()
        if any_section.match(stripped):
            skip = False
            mp = plugin_header.match(stripped)
            mh = hooks_header.match(stripped)
            mm = mkt_header.match(stripped)
            if mp and mp.group(1) in legacy_mkts:
                skip = True
                continue
            if mh and mh.group(1) in legacy_mkts:
                skip = True
                continue
            if mm and mm.group(1) in legacy_mkts:
                skip = True
                continue
        if skip:
            continue
        new_lines.append(line)

    cfg_path.write_text("".join(new_lines))
    print(
        f"removed legacy evo registrations in {cfg_path}: "
        f"{', '.join(sorted(f'evo@{m}' for m in legacy_mkts))}"
    )
    for mkt in legacy_mkts:
        stale_cache = _codex_base() / "plugins" / "cache" / mkt
        if stale_cache.exists():
            shutil.rmtree(stale_cache)
            print(f"removed legacy cache: {stale_cache}")


def _hook_event_label(event_name: str) -> str | None:
    """Map hooks.json camel-case event name → codex's snake-case label.
    Returns None for unrecognized events (skipped in trust step)."""
    return {
        "PreToolUse": "pre_tool_use",
        "PostToolUse": "post_tool_use",
        "PermissionRequest": "permission_request",
        "PreCompact": "pre_compact",
        "PostCompact": "post_compact",
        "SessionStart": "session_start",
        "UserPromptSubmit": "user_prompt_submit",
        "Stop": "stop",
        "SubagentStop": "subagent_stop",
    }.get(event_name)


def _canonical_json(value):
    """Recursively sort dict keys (mirrors codex's `canonical_json`)."""
    if isinstance(value, dict):
        return {k: _canonical_json(value[k]) for k in sorted(value.keys())}
    if isinstance(value, list):
        return [_canonical_json(v) for v in value]
    return value


def _command_hook_hash(event_label: str, matcher: str | None,
                       command: str, timeout_sec: int = 600,
                       async_: bool = False, status_msg: str | None = None) -> str:
    """Reimplement codex's `command_hook_hash` so we can compute the
    `trusted_hash` value codex's TUI writes. Algorithm (from
    codex-rs/config/src/fingerprint.rs::version_for_toml):
      1. build NormalizedHookIdentity (event_name + flattened MatcherGroup)
      2. drop Option::None fields (TOML has no null)
      3. canonical JSON (sort keys recursively)
      4. compact serde_json::to_vec
      5. SHA256, prefixed with "sha256:"
    Verified empirically against codex's `hooks/list` output across the
    initial 3 hook events (pre_tool_use, session_start, user_prompt_submit);
    extended to also cover post_tool_use, stop, and subagent_stop in 0.4.4.
    """
    import hashlib as _hashlib
    import json as _json
    identity: dict = {"event_name": event_label}
    if matcher is not None:
        identity["matcher"] = matcher
    handler: dict = {"type": "command", "command": command, "async": async_}
    if timeout_sec is not None:
        handler["timeout"] = timeout_sec
    if status_msg is not None:
        handler["statusMessage"] = status_msg
    identity["hooks"] = [handler]
    serialized = _json.dumps(_canonical_json(identity),
                             separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + _hashlib.sha256(serialized).hexdigest()


def _expected_hook_state(hooks_json_path: Path, plugin_id: str) -> dict[str, str] | None:
    """Compute the `{state_key: trusted_hash}` entries codex's TUI would
    write on user approval of every command hook in hooks.json. Returns
    None when hooks.json can't be parsed.

    The key shape codex uses (verified via `hooks/list` RPC):
        <plugin_id>:hooks/hooks.json:<event_label>:<group_idx>:<handler_idx>
    """
    import json as _json
    import sys as _sys
    try:
        hooks_file = _json.loads(hooks_json_path.read_text())
    except (OSError, _json.JSONDecodeError) as exc:
        print(f"✗ could not parse {hooks_json_path}: {exc}", file=_sys.stderr)
        return None

    expected: dict[str, str] = {}
    for event_name, groups in hooks_file.get("hooks", {}).items():
        event_label = _hook_event_label(event_name)
        if event_label is None:
            continue
        for group_idx, group in enumerate(groups or []):
            matcher = group.get("matcher")
            for handler_idx, handler in enumerate(group.get("hooks", []) or []):
                if handler.get("type") != "command":
                    continue
                cmd = handler.get("command")
                if not cmd:
                    continue
                timeout = handler.get("timeout")
                if timeout is None:
                    timeout = 600  # codex's default after normalization
                key = (f'{plugin_id}:hooks/hooks.json:'
                       f'{event_label}:{group_idx}:{handler_idx}')
                expected[key] = _command_hook_hash(
                    event_label, matcher, cmd,
                    timeout_sec=timeout,
                    async_=bool(handler.get("async", False)),
                    status_msg=handler.get("statusMessage"),
                )
    return expected


def _strip_plugin_hook_state(cfg: Path, plugin_id: str) -> bool:
    text = cfg.read_text() if cfg.exists() else ""
    stripped = _strip_toml_sections(
        text,
        lambda header: header.startswith(f'[hooks.state."{plugin_id}:'),
    ).rstrip()
    new_text = (stripped + "\n") if stripped else ""
    if new_text == text:
        return False
    cfg.write_text(new_text)
    return True


def _trust_plugin_hooks(hooks_json_path: Path, plugin_id: str, cfg: Path) -> None:
    """Write `[hooks.state."<key>"] trusted_hash = "..."` entries to
    config.toml, same effect as `codex` → `/hooks` → Trust each.
    """
    expected = _expected_hook_state(hooks_json_path, plugin_id)
    if expected is None:
        return
    if not expected:
        print("(no command hooks to trust)")
        return
    lines = [
        f'[hooks.state."{key}"]\ntrusted_hash = "{trusted_hash}"'
        for key, trusted_hash in expected.items()
    ]

    block = "\n\n".join(lines) + "\n"
    text = cfg.read_text() if cfg.exists() else ""
    # Replace whole existing [hooks.state."..."] blocks for this plugin so
    # repeated --trust-hooks calls don't accumulate stale or disabled entries.
    # Codex may include additional keys such as `enabled = false`; a regex that
    # only removes `trusted_hash` lines leaves duplicate TOML table headers.
    text = _strip_toml_sections(
        text,
        lambda header: header.startswith(f'[hooks.state."{plugin_id}:'),
    ).rstrip() + "\n\n" + block
    cfg.write_text(text)
    events = sorted({key.split(":")[-3] for key in expected})
    print(
        f"updated {cfg}: trusted {len(lines)} hooks for {plugin_id} "
        f"({', '.join(events)})"
    )


def _install_via_rpc(from_path: str | None) -> int:
    """Send `initialize` + `plugin/install` to `codex app-server` via
    stdio JSON-RPC. Returns 0 on success, 2 on failure.
    """
    import json as _json
    import subprocess as _sp
    import sys as _sys

    mj = _resolve_marketplace_json(from_path)
    if mj is None:
        if from_path:
            print(
                f"✗ marketplace.json not found at {from_path} (looked for "
                f"<root>/.claude-plugin/marketplace.json)",
                file=_sys.stderr,
            )
        else:
            print(
                "✗ no marketplace cache; run "
                "`codex plugin marketplace add evo-hq/evo` first, or pass "
                "`--from-path <marketplace-root>` for local source",
                file=_sys.stderr,
            )
        return 2

    init = _json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"clientInfo": {"name": "evo-install", "version": "0.1"}},
    })
    install = _json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "plugin/install",
        "params": {"pluginName": "evo", "marketplacePath": str(mj)},
    })
    input_str = init + "\n" + install + "\n"

    print(f"calling codex plugin/install with marketplacePath={mj}")
    try:
        proc = _sp.run(
            ["codex", "app-server"],
            input=input_str,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, _sp.TimeoutExpired) as e:
        print(f"✗ codex app-server invocation failed: {e}", file=_sys.stderr)
        return 2

    # Diagnostic: surface the raw RPC exchange so failures are debuggable.
    print(f"codex app-server exit={proc.returncode}")
    if proc.stdout:
        print(f"--- stdout ({len(proc.stdout)} bytes) ---")
        print(proc.stdout[:2000])
    if proc.stderr:
        print(f"--- stderr ({len(proc.stderr)} bytes) ---")
        print(proc.stderr[:2000])

    # Parse newline-delimited JSON-RPC responses; surface any error.
    saw_id2 = False
    failed = False
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            msg = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if msg.get("id") == 2:
            saw_id2 = True
            if "error" in msg:
                print(f"✗ plugin/install failed: {msg['error']}", file=_sys.stderr)
                failed = True
            else:
                print(f"✓ plugin/install succeeded: {msg.get('result', {})}")
    if not saw_id2:
        print(
            "✗ plugin/install RPC: no response with id=2 — codex app-server "
            "may have exited before processing, or output got truncated",
            file=_sys.stderr,
        )
        return 2
    if failed:
        return 2
    return 0


def uninstall(args: argparse.Namespace) -> int:
    changed_h, cfg = _toggle_plugin_hooks(enable=False)
    if changed_h:
        print(f"updated {cfg}: disabled plugin_hooks")
    changed_p, _ = _enable_plugin(enable=False)
    if changed_p:
        print(f"updated {cfg}: removed {_PLUGIN_KEY}")
    print("To remove the marketplace itself: `codex plugin marketplace remove evo-hq`")
    return 0


def doctor(args: argparse.Namespace) -> int:
    cfg = _codex_base() / "config.toml"
    cache = _marketplace_cache()

    rc = 0
    cfg_text = cfg.read_text() if cfg.exists() else ""

    if "plugin_hooks = true" in cfg_text:
        print(f"✓ plugin_hooks = true in {cfg}")
    else:
        print(f"✗ plugin_hooks not enabled in {cfg}")
        print("  Run: evo install codex")
        rc = 1

    if _PLUGIN_KEY in cfg_text:
        print(f"✓ {_PLUGIN_KEY} in {cfg}")
    else:
        print(f"✗ {_PLUGIN_KEY} not in {cfg}")
        print("  Run: evo install codex")
        rc = 1

    if cache.exists():
        print(f"✓ evo marketplace cached at {cache}")
        # Warning only (no rc bump): the active cache binary check below
        # covers the live install, and `evo update` skips hosts whose
        # doctor fails; failing here would block the self-heal path.
        snapshot_binary = cache / "plugins" / "evo" / "bin" / hook_drain_binary_name()
        if not snapshot_binary.exists():
            print(
                f"! evo-hook-drain not mirrored in the marketplace snapshot "
                f"({snapshot_binary})\n"
                f"  A codex plugin re-stage would drop the hook binary "
                f"(hooks exit 127). Run: evo install codex"
            )
    else:
        print(f"✗ no marketplace cache at {cache}")
        print("  Run: codex plugin marketplace add evo-hq/evo")
        rc = 1

    # The hook drain binary is what every hooks.json command resolves to.
    # config.toml can have plugin_hooks=true and the plugin enabled while
    # the binary is missing from the cache dir codex actually loads — then
    # every hook fires exit 127. Verify it exists + is executable at the
    # plugin root codex resolves for the active `evo@<owner>` selector.
    import re as _re
    plugin_cache_root = _codex_base() / "plugins" / "cache"
    # Strip comment lines so a commented-out `# [plugins."evo@old"]` isn't
    # matched and chased to a non-existent cache dir.
    uncommented = "\n".join(
        ln for ln in cfg_text.splitlines() if not ln.lstrip().startswith("#")
    )
    active_mkts = _re.findall(r'\[plugins\."evo@([^"]+)"\]', uncommented)
    if not active_mkts:
        # No enabled plugin entry to resolve a binary path against; the
        # checks above already flagged the missing _PLUGIN_KEY.
        return rc

    def _ver_key(name: str):
        # Numeric-aware so 0.10.0 sorts after 0.9.0 (plain sorted() is
        # lexicographic and would invert that). Non-numeric segments
        # (pre-release tags) fall into a separate rank so int/str never
        # compare.
        return [(0, int(s)) if s.isdigit() else (1, s)
                for s in _re.split(r"[.-]", name)]

    for mkt_name in active_mkts:
        mkt_cache = plugin_cache_root / mkt_name / "evo"
        versions = (
            sorted((p for p in mkt_cache.iterdir() if p.is_dir()),
                   key=lambda p: _ver_key(p.name))
            if mkt_cache.is_dir() else []
        )
        if not versions:
            print(f"✗ no plugin cache for evo@{mkt_name} at {mkt_cache}")
            print("  Run: evo install codex --force")
            rc = 1
            continue
        binary = versions[-1] / "bin" / hook_drain_binary_name()
        stable = stable_binary_path()
        if not binary.exists():
            print(f"✗ evo-hook-drain missing at {binary} (hooks fire exit 127)")
            print("  Run: evo install codex --force")
            rc = 1
        elif not os.access(binary, os.X_OK):
            print(f"✗ evo-hook-drain at {binary} is not executable")
            print("  Run: evo install codex --force")
            rc = 1
        elif not is_wrapper_script(binary):
            print(f"✓ evo-hook-drain present + executable at {binary}")
        elif stable.exists() and os.access(stable, os.X_OK):
            print(
                f"✓ evo-hook-drain fallback wrapper at {binary} "
                f"(execs stable binary at {stable})"
            )
        else:
            print(
                f"✗ evo-hook-drain at {binary} is the fallback wrapper and "
                f"no stable binary exists at {stable} (hooks no-op)"
            )
            print("  Run: evo install codex --force")
            rc = 1

        # Trust state. Untrusted hooks register but never fire, so `evo
        # direct` silently does nothing. Three states:
        #   - all expected entries present with matching hashes → ✓
        #   - no entries at all → warning only (deliberate
        #     --no-trust-hooks installs await the user's /hooks review)
        #   - some entries missing or hash-mismatched → ✗ (hooks.json
        #     changed since trust was written, silent breakage the
        #     user didn't choose)
        hooks_json = versions[-1] / "hooks" / "hooks.json"
        if not hooks_json.exists():
            continue
        try:
            hooks_text = hooks_json.read_text()
        except OSError:
            hooks_text = ""
        if "CLAUDE_PLUGIN_ROOT" in hooks_text:
            print(
                f"✗ Codex hooks still reference CLAUDE_PLUGIN_ROOT in {hooks_json}; "
                f"recent Codex versions may run them as /bin/evo-hook-drain "
                f"(exit 127)\n"
                f"  Run: evo install codex --force"
            )
            rc = 1
        expected = _expected_hook_state(hooks_json, f"evo@{mkt_name}")
        if not expected:
            continue
        actual = dict(_re.findall(
            r'\[hooks\.state\."([^"]+)"\]\s*\ntrusted_hash = "([^"]+)"',
            uncommented,
        ))
        stale = {
            key for key, h in expected.items()
            if key in actual and actual[key] != h
        }
        missing = {key for key in expected if key not in actual}
        if not stale and not missing:
            print(f"✓ {len(expected)} hooks trusted for evo@{mkt_name}")
        elif len(missing) == len(expected) and not stale:
            print(
                f"! hooks installed but untrusted for evo@{mkt_name}: "
                f"they never fire, so `evo direct` won't be delivered\n"
                f"  Trust via `/hooks` inside codex, or run: evo install codex"
            )
        else:
            print(
                f"✗ hook trust is stale for evo@{mkt_name} "
                f"({len(stale)} mismatched, {len(missing)} missing): "
                f"hooks.json changed since trust was written; those hooks "
                f"never fire\n"
                f"  Run: evo install codex"
            )
            rc = 1
    return rc
