---
name: infra-setup
description: Switch evo's execution backend or remote provider with explicit prerequisite checks and one-command auth/install guidance.
argument-hint: <provider or backend to switch to>
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
   - `pipx`: `pipx inject evo-hq-cli <pkg>`
   - `uv-tool`: `uv tool install --reinstall 'evo-hq-cli[<extra>]'`
   - `venv` / `pip`: `python -m pip install <pkg>`
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

- `modal`: requires the `modal` Python package and a Modal token. Use `references/modal-auth-prompt.md`.
- `e2b`: requires the `e2b` Python package and an `E2B_API_KEY`. Use `references/e2b-auth-prompt.md`.
- `daytona`: requires the `daytona` Python package and a Daytona API key. Use `references/daytona-auth-prompt.md`.
- `aws`: requires `boto3`, AWS credentials, and an EC2 key pair/private key. Use `references/aws-auth-prompt.md`.
- `hetzner`: requires `hcloud`, a Hetzner token, and an SSH key. Use `references/hetzner-auth-prompt.md`.
- `ssh`: requires local `ssh`, remote SSH access, and a reachable `host`. No Python SDK. Use `references/ssh-auth-prompt.md`.
- `manual`: no provisioning. Only ask for the sandbox-agent URL/token if the user explicitly wants manual mode.

## Troubleshooting

Use `references/provider-troubleshooting.md` for the short list of common failures.
