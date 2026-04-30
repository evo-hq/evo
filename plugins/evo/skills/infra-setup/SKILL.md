---
name: infra-setup
description: Non-user-invocable provider/setup reference for evo backend switching, prerequisite checks, and auth/install guidance.
disable-model-invocation: true
---

# Infra Setup

Use this when the user wants to change where experiments run: local worktrees, pool slots, Modal, Daytona, AWS, Hetzner, SSH, or later providers.

## Goals

- Be explicit about the target backend/provider.
- Check prerequisites before mutating evo config.
- Never install provider SDKs silently.
- Give one actionable auth command per provider.

## Flow

1. Identify the target:
   - `worktree` or `pool` means local backends.
   - `modal`, `e2b`, `ssh:...`, or another remote spec means `backend=remote`.
2. If the target is remote, parse the provider choice the same way evo CLI does:
- `modal`
- `e2b`
- `daytona`
- `aws`
- `hetzner`
- `ssh:user@host[:port]`
   - built-in provider name
   - dotted import path
3. Check whether evo itself appears to be installed via `pipx`, `uv tool`, or a venv by calling `python -c "from evo.providers import detect_install_method; print(detect_install_method())"`.
4. For SDK-backed providers, verify the SDK import. If missing, ask the user before installing it.
   - If `evo` was installed with `uv tool` or `pip`/`venv`, prefer the matching extra on `evo-hq-cli`:
     - `uv-tool`: `uv tool install --reinstall 'evo-hq-cli[<provider-extra>]'`
     - `venv` / `pip`: `python -m pip install 'evo-hq-cli[<provider-extra>]'`
   - If `evo` was installed with `pipx`, inject the provider SDK into the same `evo-hq-cli` environment:
     - `pipx`: `pipx inject evo-hq-cli <provider-sdk>`
5. Check auth and show exactly one provider-specific auth command. Reference `references/<provider>-auth-prompt.md`.
6. Once prerequisites are satisfied, run the explicit config command:

```bash
evo config backend remote --provider <provider> --provider-config ...
```

Or for local backends:

```bash
evo config backend worktree
evo config backend pool --workspaces /abs/slot-a,/abs/slot-b
```

## Provider notes

See `references/provider-matrix.md` for the compact provider summary. Use the provider-specific auth prompt file for the exact command once you know the provider.
