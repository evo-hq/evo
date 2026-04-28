"""Remote sandbox provider registry.

Each provider lives in this package as a single module (`modal.py`, `e2b.py`,
`ssh.py`, ...) implementing the `SandboxProvider` protocol from
`..protocol`. Loaders below lazy-import each provider module so a user who
only wants Modal doesn't carry E2B's SDK as a hard dependency.

To register a new provider: add a new module here, add a loader that calls
its constructor, and append a `(name, loader)` entry to `_LOADERS`.
"""
from __future__ import annotations

from typing import Any, Callable

from ..protocol import RemoteBackendUnavailable, SandboxProvider


def _load_modal(config: dict[str, Any]) -> SandboxProvider:
    # The Modal provider module itself lazy-imports the `modal` SDK; it
    # raises a custom error if the SDK is missing. This loader's `except`
    # covers (a) the provider module not yet existing in this build, and
    # (b) the SDK import inside it failing. Both surface as `ImportError`
    # (`ModuleNotFoundError` is a subclass) -- catch the parent.
    try:
        from . import modal as _modal_module
    except ImportError as exc:
        raise RemoteBackendUnavailable(
            "Modal provider requested but the 'modal' Python SDK is not "
            "installed. Install it with: pip install modal "
            "(or pipx install --pip-args=--pre --force 'evo-hq-cli[modal]' "
            "once the optional dep group is published)."
        ) from exc
    return _modal_module.ModalProvider(config)


_LOADERS: dict[str, Callable[[dict[str, Any]], SandboxProvider]] = {
    "modal": _load_modal,
}


def load_provider(name: str, config: dict[str, Any]) -> SandboxProvider:
    """Resolve and construct the provider by name.

    Raises `RemoteBackendUnavailable` if the provider is unknown or its
    dependencies are missing. The error message is the user-facing surface
    -- keep it actionable.
    """
    if name not in _LOADERS:
        known = ", ".join(sorted(_LOADERS)) or "(none)"
        raise RemoteBackendUnavailable(
            f"Unknown remote provider {name!r}. Known providers: {known}."
        )
    return _LOADERS[name](config)


def known_providers() -> list[str]:
    """For diagnostics / `evo workspace status` output."""
    return sorted(_LOADERS)
