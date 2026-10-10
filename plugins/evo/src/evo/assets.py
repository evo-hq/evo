"""Workspace asset registry (#55, local-first).

Names workspace artifacts so experiments can reuse them by handle/tag instead
of hardcoding brittle absolute paths, and records produced/consumed lineage.

This module keeps the *pure* registry logic (operating on a plain dict) separate
from disk I/O so the core is unit-testable without a workspace. Disk wrappers
live at the bottom and mirror the locking/atomic-write conventions used by
`evo config set`.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any, Callable

from .core import atomic_write_json, workspace_path

REGISTRY_VERSION = 1
REGISTRY_FILE = "assets.json"


def empty_registry() -> dict[str, Any]:
    return {"version": REGISTRY_VERSION, "assets": {}}


# --- pure registry logic (no I/O) ------------------------------------------

def normalize_asset_name(name: str) -> str:
    """Canonical form of an asset handle (trimmed). Raises on empty/blank so the
    stored key, the entry's name field, and every lookup agree on one form.
    The name is also used as a directory (`put --copy`, remote cache), so path
    separators and dot-names are rejected to keep it inside the assets dir."""
    normalized = str(name or "").strip()
    if not normalized:
        raise ValueError("asset name must be non-empty")
    if normalized in {".", ".."} or "/" in normalized or "\\" in normalized:
        raise ValueError(f"asset name must not contain path separators: {normalized!r}")
    return normalized


def registry_put(reg: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """Insert or replace an asset by name. Returns the stored entry.

    Keys the entry under its normalized name and rewrites ``entry['name']`` to
    match, so the storage key and the name field can never diverge.
    """
    name = normalize_asset_name(entry.get("name") or "")
    if not str(entry.get("kind") or "").strip():
        raise ValueError("asset kind must be non-empty")
    # Distinct names can slug to one env var ('a-b' / 'a_b'); one would silently
    # shadow the other in a run's environment, so refuse the second.
    key = asset_env_var(name)
    clash = next((n for n in reg.get("assets", {})
                  if n != name and asset_env_var(n) == key), None)
    if clash:
        raise ValueError(f"asset name {name!r} maps to {key}, already used by {clash!r}")
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


def asset_env_for_exp(
    reg: dict[str, Any],
    exp_id: str,
    resolve: Callable[[dict[str, Any]], str | None] | None = None,
) -> dict[str, str]:
    """Env vars to inject for a run: one EVO_ASSET_<NAME> per asset the
    experiment consumes. `resolve` maps an entry to the value the run sees; the
    default is its stored location (path, else uri), which is all this pure
    layer knows. The CLI passes a resolver that fetches remote assets into the
    local cache so the variable is a usable local path."""
    resolve = resolve or asset_location
    out: dict[str, str] = {}
    for e in registry_filter(reg, consumed_by=exp_id):
        value = resolve(e)
        if value:
            out[asset_env_var(e["name"])] = value
    return out


def asset_location(entry: dict[str, Any]) -> str | None:
    """Where an asset lives: its local path, else its remote uri (remote assets
    have `path: None`, so callers must not read `entry['path']` directly)."""
    return entry.get("path") or entry.get("uri")


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


NAME_LOCK_TIMEOUT_SECONDS = 10.0


def asset_name_lock(root: Path, name: str) -> Path:
    """Lock file that reserves one asset name for the whole of a `put`, including
    its slow remote upload, without holding the registry-wide lock. Hashed so any
    valid asset name is a safe filename on every platform."""
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    return assets_dir(root) / "_locks" / f"{digest}.lock"


def _cache_root(root: Path, name: str) -> Path:
    return assets_dir(root) / "_cache" / name


def assets_cache_dir(root: Path, name: str, uri: str) -> Path:
    """Local cache dir where a remote asset is downloaded on `get`/`use`. Keyed
    by uri as well as name, so a handle re-pointed at another uri never serves
    the previous uri's cached bytes (same-uri re-puts: see clear_asset_cache)."""
    digest = hashlib.sha256(uri.encode("utf-8")).hexdigest()[:16]
    return _cache_root(root, name) / digest


def clear_asset_cache(root: Path, name: str) -> None:
    """Drop every downloaded copy of `name` so the cache never outlives its
    registry entry: a later put at the SAME uri uploads new bytes there, and a
    stale copy would keep being served. Raises if a copy can't be deleted, so the
    caller must not drop the registry entry in that case."""
    try:
        shutil.rmtree(_cache_root(root, name))
    except FileNotFoundError:
        pass  # local asset, or never fetched: nothing cached


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


def discard_copy(path: Path) -> None:
    """Best-effort removal of exactly the copy `materialize` returned (never
    raises, so it can't mask the error that triggered it). The asset's directory
    is removed only if that leaves it empty: anything else there isn't ours."""
    with contextlib.suppress(OSError):
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        path.parent.rmdir()  # fails (and is ignored) unless now empty


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
