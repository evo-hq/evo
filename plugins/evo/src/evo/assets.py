"""Workspace asset registry (#55, local-first).

Names workspace artifacts so experiments can reuse them by handle/tag instead
of hardcoding brittle absolute paths, and records produced/consumed lineage.

This module keeps the *pure* registry logic (operating on a plain dict) separate
from disk I/O so the core is unit-testable without a workspace. Disk wrappers
live at the bottom and mirror the locking/atomic-write conventions used by
`evo config set`.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from .core import atomic_write_json, workspace_path

REGISTRY_VERSION = 1
REGISTRY_FILE = "assets.json"


def empty_registry() -> dict[str, Any]:
    return {"version": REGISTRY_VERSION, "assets": {}}


# --- pure registry logic (no I/O) ------------------------------------------

def normalize_asset_name(name: str) -> str:
    """Canonical form of an asset handle (trimmed). Raises on empty/blank so the
    stored key, the entry's name field, and every lookup agree on one form."""
    normalized = str(name or "").strip()
    if not normalized:
        raise ValueError("asset name must be non-empty")
    return normalized


def registry_put(reg: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """Insert or replace an asset by name. Returns the stored entry.

    Keys the entry under its normalized name and rewrites ``entry['name']`` to
    match, so the storage key and the name field can never diverge.
    """
    name = normalize_asset_name(entry.get("name") or "")
    if not str(entry.get("kind") or "").strip():
        raise ValueError("asset kind must be non-empty")
    entry["name"] = name
    reg.setdefault("assets", {})[name] = entry
    return entry


def registry_filter(
    reg: dict[str, Any],
    *,
    kind: str | None = None,
    tags: dict[str, str] | None = None,
    produced_by: str | None = None,
    consumed_by: str | None = None,
) -> list[dict[str, Any]]:
    """Return assets matching all supplied criteria. `tags` is an AND-match."""
    out = []
    for entry in reg.get("assets", {}).values():
        if kind is not None and entry.get("kind") != kind:
            continue
        if produced_by is not None and entry.get("produced_by") != produced_by:
            continue
        if consumed_by is not None and consumed_by not in (entry.get("consumed_by") or []):
            continue
        if tags:
            entry_tags = entry.get("tags") or {}
            if any(entry_tags.get(k) != v for k, v in tags.items()):
                continue
        out.append(entry)
    return out


def registry_record_use(reg: dict[str, Any], name: str, exp_id: str) -> dict[str, Any]:
    """Record that `exp_id` consumes asset `name` (idempotent)."""
    entry = reg.get("assets", {}).get(name)
    if entry is None:
        raise KeyError(name)
    consumed = entry.setdefault("consumed_by", [])
    if exp_id not in consumed:
        consumed.append(exp_id)
    return entry


def registry_remove(reg: dict[str, Any], name: str, force: bool = False) -> dict[str, Any]:
    """Remove asset `name`. Refuses if still consumed unless `force`."""
    assets = reg.get("assets", {})
    entry = assets.get(name)
    if entry is None:
        raise KeyError(name)
    consumers = entry.get("consumed_by") or []
    if consumers and not force:
        raise RuntimeError(
            f"asset {name!r} is consumed by {', '.join(consumers)}; "
            f"pass --force to remove anyway"
        )
    return assets.pop(name)


def asset_env_for_exp(reg: dict[str, Any], exp_id: str) -> dict[str, str]:
    """Env vars to inject for a run: one EVO_ASSET_<NAME> per asset the
    experiment consumes, mapping to the asset's canonical path."""
    return {
        asset_env_var(e["name"]): e["path"]
        for e in registry_filter(reg, consumed_by=exp_id)
    }


def asset_env_var(name: str) -> str:
    """Map an asset name to its run env var: 'base-model' -> EVO_ASSET_BASE_MODEL."""
    slug = re.sub(r"[^0-9A-Za-z]+", "_", name).strip("_").upper()
    return f"EVO_ASSET_{slug}"


def parse_tag(spec: str) -> tuple[str, str]:
    """Parse a 'k=v' tag spec. The value may itself contain '='."""
    key, sep, value = spec.partition("=")
    if not sep or not key.strip():
        raise ValueError(f"tag must be k=v (got {spec!r})")
    return key.strip(), value


# --- disk layer ------------------------------------------------------------

def assets_path(root: Path) -> Path:
    """Path to the workspace asset registry file (per active run)."""
    return workspace_path(root) / REGISTRY_FILE


def assets_dir(root: Path) -> Path:
    """Directory holding materialized (`put --copy` / `use`) asset copies."""
    return workspace_path(root) / "assets"


def load_registry(root: Path) -> dict[str, Any]:
    path = assets_path(root)
    if not path.exists():
        return empty_registry()
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("version", REGISTRY_VERSION)
    data.setdefault("assets", {})
    return data


def save_registry(root: Path, reg: dict[str, Any]) -> None:
    atomic_write_json(assets_path(root), reg)


def materialize(root: Path, name: str, source: Path) -> Path:
    """Copy `source` under the workspace assets dir and return the new path.
    Used by `put --copy` so the registered asset survives moves of the source."""
    dest_dir = assets_dir(root) / name
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / source.name
    if source.is_dir():
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(source, dest)
    else:
        shutil.copy2(source, dest)
    return dest.resolve()
