"""File-locked reader/writer for keyed remote state files.

Schema:
{
  "provider": "modal",
  "provider_config": {...},
  "sandboxes": [
    {
      "id": 0,
      "native_id": "sb-abc123",
      "base_url": "https://...",
      "leased_by": null | {"exp_id": "exp_NNNN", "pid": 12345, "leased_at": "..."},
      "last_branch": "evo/run_NNNN/exp_NNNN" | null,
      "provisioned_at": "..."
    }
  ]
}

Bearer tokens are encrypted at rest with the workspace-level `.evo/keyfile`
so separate evo processes can share them without persisting plaintext
secrets in `remote_state.json`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..locking import advisory_lock


def _state_dir(root: Path) -> Path:
    from ..core import workspace_path

    return workspace_path(root) / "backend_state"


def remote_state_path(root: Path, state_key: str | None = None) -> Path:
    """Path to this remote config's state file."""
    if state_key is None:
        from ..core import workspace_path

        return workspace_path(root) / "remote_state.json"
    return _state_dir(root) / f"remote-{state_key}.json"


def _migrate_legacy_if_needed(root: Path, state_key: str) -> Path:
    keyed = remote_state_path(root, state_key)
    if keyed.exists():
        return keyed
    legacy = remote_state_path(root, None)
    if legacy.exists():
        keyed.parent.mkdir(parents=True, exist_ok=True)
        legacy.replace(keyed)
    return keyed


def _resolve_state_path(root: Path, state_key: str | None) -> Path:
    if state_key is not None:
        return _migrate_legacy_if_needed(root, state_key)
    legacy = remote_state_path(root, None)
    if legacy.exists():
        return legacy
    state_dir = _state_dir(root)
    matches = sorted(state_dir.glob("remote-*.json")) if state_dir.exists() else []
    if len(matches) == 1:
        return matches[0]
    if not matches:
        return legacy
    raise RuntimeError(
        "multiple remote state files exist for this run; pass an explicit state key"
    )


def init_state(
    root: Path,
    provider: str,
    provider_config: dict[str, Any],
    state_key: str,
) -> None:
    """Create a fresh keyed remote-state file with no sandboxes yet.
    Sandboxes are spun up lazily on first `evo new`.
    """
    state_path = remote_state_path(root, state_key)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "provider": provider,
        "provider_config": provider_config,
        "next_id": 0,
        "sandboxes": [],
    }
    from ..core import atomic_write_json

    atomic_write_json(state_path, state)


def _lock_path(state_path: Path) -> Path:
    return state_path.with_suffix(state_path.suffix + ".lock")


@contextmanager
def locked_state(root: Path, state_key: str) -> Iterator[dict[str, Any]]:
    """Open this remote config's state file under a file lock for RMW.

    Mirrors `pool_state.locked_state`. The caller mutates the dict in place;
    on exit the state is written via tmp-and-rename.
    """
    from ..core import atomic_write_json

    state_path = _migrate_legacy_if_needed(root, state_key)
    if not state_path.exists():
        raise FileNotFoundError(f"remote_state.json missing at {state_path}")
    with advisory_lock(_lock_path(state_path)):
        state = _load_validated(root, state_path, migrate_plaintext=True)
        yield state
        atomic_write_json(state_path, _encode_state_for_disk(root, state))


def read_state(root: Path, state_key: str | None = None) -> dict[str, Any]:
    """Read-only snapshot of this remote config's state file."""
    state_path = _resolve_state_path(root, state_key)
    if not state_path.exists():
        raise FileNotFoundError(f"remote_state.json missing at {state_path}")
    with advisory_lock(_lock_path(state_path)):
        return _load_validated(root, state_path, migrate_plaintext=True)


def _load_validated(
    root: Path,
    state_path: Path,
    *,
    migrate_plaintext: bool,
) -> dict[str, Any]:
    """Read + minimally validate remote_state.json. Surface a recovery error
    rather than letting JSON / KeyError percolate up to the user."""
    from ..core import atomic_write_json

    try:
        raw = state_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"remote_state.json at {state_path} is corrupted ({exc}). "
            f"This usually indicates an interrupted write. Inspect the file; "
            f"if recovery is impossible, restore from a backup or re-init."
        ) from exc
    if not isinstance(data, dict) or "sandboxes" not in data or "provider" not in data:
        raise RuntimeError(
            f"remote_state.json at {state_path} has unexpected shape "
            f"(missing 'provider' or 'sandboxes' key). File may have been "
            f"hand-edited or corrupted."
        )
    data.setdefault("next_id", _next_id_from_state(data))
    decoded, needs_migrate = _decode_state_from_disk(root, data)
    if migrate_plaintext and needs_migrate:
        atomic_write_json(state_path, _encode_state_for_disk(root, decoded))
    return decoded


def _decode_state_from_disk(
    root: Path,
    data: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    state = {
        key: value
        for key, value in data.items()
        if key not in {"sandboxes", "provider_config_secret_enc"}
    }
    provider_config = dict(state.get("provider_config", {}) or {})
    provider_secret = data.get("provider_config_secret_enc")
    needs_migrate = False
    if provider_secret is not None:
        provider_config["bearer_token"] = _decrypt_token(root, provider_secret)
    elif "bearer_token" in provider_config:
        needs_migrate = True
    state["provider_config"] = provider_config
    state["sandboxes"] = []
    for raw_sandbox in data.get("sandboxes", []):
        sandbox = dict(raw_sandbox)
        token_payload = sandbox.pop("bearer_token_enc", None)
        if token_payload is not None:
            sandbox["bearer_token"] = _decrypt_token(root, token_payload)
        elif "bearer_token" in sandbox:
            needs_migrate = True
        state["sandboxes"].append(sandbox)
    return state, needs_migrate


def _encode_state_for_disk(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    encoded = {
        key: value
        for key, value in state.items()
        if key not in {"sandboxes", "provider_config"}
    }
    provider_config = dict(state.get("provider_config", {}) or {})
    provider_token = provider_config.pop("bearer_token", None)
    encoded["provider_config"] = provider_config
    if provider_token is not None:
        encoded["provider_config_secret_enc"] = _encrypt_token(root, str(provider_token))
    encoded["next_id"] = _next_id_from_state(state)
    encoded["sandboxes"] = []
    for raw_sandbox in state.get("sandboxes", []):
        sandbox = dict(raw_sandbox)
        token = sandbox.pop("bearer_token", None)
        if token is not None:
            sandbox["bearer_token_enc"] = _encrypt_token(root, str(token))
        encoded["sandboxes"].append(sandbox)
    return encoded


def _next_id_from_state(state: dict[str, Any]) -> int:
    configured = state.get("next_id")
    if isinstance(configured, int) and configured >= 0:
        return configured
    ids = [int(s.get("id", -1)) for s in state.get("sandboxes", [])]
    return (max(ids) + 1) if ids else 0


def _encrypt_token(root: Path, token: str) -> dict[str, str | int]:
    from ..core import ensure_workspace_keyfile

    key = ensure_workspace_keyfile(root).read_bytes()
    token_bytes = token.encode("utf-8")
    nonce = secrets.token_bytes(16)
    ciphertext = bytes(
        a ^ b for a, b in zip(token_bytes, _keystream(key, nonce, len(token_bytes)))
    )
    mac = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
    return {
        "v": 1,
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        "mac": base64.b64encode(mac).decode("ascii"),
    }


def _decrypt_token(root: Path, payload: dict[str, Any]) -> str:
    from ..core import ensure_workspace_keyfile

    if payload.get("v") != 1:
        raise RuntimeError(f"unsupported bearer_token_enc version: {payload.get('v')!r}")
    key = ensure_workspace_keyfile(root).read_bytes()
    nonce = base64.b64decode(payload["nonce"])
    ciphertext = base64.b64decode(payload["ciphertext"])
    expected_mac = base64.b64decode(payload["mac"])
    actual_mac = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(expected_mac, actual_mac):
        raise RuntimeError("remote_state.json bearer token MAC verification failed")
    plaintext = bytes(
        a ^ b for a, b in zip(ciphertext, _keystream(key, nonce, len(ciphertext)))
    )
    return plaintext.decode("utf-8")


def _keystream(key: bytes, nonce: bytes, size: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < size:
        out.extend(
            hmac.new(
                key,
                nonce + counter.to_bytes(8, "big"),
                hashlib.sha256,
            ).digest()
        )
        counter += 1
    return bytes(out[:size])
