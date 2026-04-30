"""Remote sandbox provider registry.

Each provider lives in this package as a single module (`modal.py`, `e2b.py`,
`ssh.py`, `cloudflare.py`, ...) implementing the `SandboxProvider` protocol from
`..protocol`. Loaders below lazy-import each provider module so a user who
only wants Modal doesn't carry E2B's SDK as a hard dependency.

To register a new provider: add a new module here, add a loader that calls
its constructor, and append a `(name, loader)` entry to `_LOADERS`.
"""
from __future__ import annotations

import importlib
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
            "installed. Install it with: python -m pip install "
            "'evo-hq-cli[modal]' "
            "(or `pipx inject evo-hq-cli modal` if evo itself was installed "
            "with pipx)."
        ) from exc
    return _modal_module.ModalProvider(config)


def _load_manual(config: dict[str, Any]) -> SandboxProvider:
    """`manual` provider: bring-your-own sandbox-agent.

    Skips provisioning and tear-down; just wraps a pre-existing
    (base_url, bearer_token) pair as a SandboxHandle. Useful for users
    who run sandbox-agent on their own VM, and for tests that spawn a
    real sandbox-agent on localhost.
    """
    from . import manual as _manual_module
    return _manual_module.ManualProvider(config)


def _load_ssh(config: dict[str, Any]) -> SandboxProvider:
    from . import ssh as _ssh_module
    return _ssh_module.SSHProvider(config)


def _load_e2b(config: dict[str, Any]) -> SandboxProvider:
    try:
        from . import e2b as _e2b_module
    except ImportError as exc:
        raise RemoteBackendUnavailable(
            "E2B provider requested but the 'e2b' Python SDK is not "
            "installed. Install it with: python -m pip install "
            "'evo-hq-cli[e2b]' "
            "(or `pipx inject evo-hq-cli e2b` if evo itself was installed "
            "with pipx)."
        ) from exc
    return _e2b_module.E2BProvider(config)


def _load_daytona(config: dict[str, Any]) -> SandboxProvider:
    try:
        from . import daytona as _daytona_module
    except ImportError as exc:
        raise RemoteBackendUnavailable(
            "Daytona provider requested but the 'daytona' Python SDK is not "
            "installed. Install it with: python -m pip install "
            "'evo-hq-cli[daytona]' "
            "(or `pipx inject evo-hq-cli daytona` if evo itself was installed "
            "with pipx)."
        ) from exc
    return _daytona_module.DaytonaProvider(config)


def _load_aws(config: dict[str, Any]) -> SandboxProvider:
    try:
        from . import aws as _aws_module
    except ImportError as exc:
        raise RemoteBackendUnavailable(
            "AWS provider requested but the 'boto3' Python SDK is not installed. "
            "Install it with: python -m pip install 'evo-hq-cli[aws]' "
            "(or `pipx inject evo-hq-cli boto3` if evo itself was installed "
            "with pipx)."
        ) from exc
    return _aws_module.AWSProvider(config)


def _load_hetzner(config: dict[str, Any]) -> SandboxProvider:
    try:
        from . import hetzner as _hetzner_module
    except ImportError as exc:
        raise RemoteBackendUnavailable(
            "Hetzner provider requested but the 'hcloud' Python SDK is not installed. "
            "Install it with: python -m pip install 'evo-hq-cli[hetzner]' "
            "(or `pipx inject evo-hq-cli hcloud` if evo itself was installed "
            "with pipx)."
        ) from exc
    return _hetzner_module.HetznerProvider(config)


def _load_cloudflare(config: dict[str, Any]) -> SandboxProvider:
    from . import cloudflare as _cloudflare_module
    return _cloudflare_module.CloudflareProvider(config)


_LOADERS: dict[str, Callable[[dict[str, Any]], SandboxProvider]] = {
    "modal": _load_modal,
    "manual": _load_manual,
    "ssh": _load_ssh,
    "e2b": _load_e2b,
    "daytona": _load_daytona,
    "aws": _load_aws,
    "hetzner": _load_hetzner,
    "cloudflare": _load_cloudflare,
}


def load_provider(name: str, config: dict[str, Any]) -> SandboxProvider:
    """Resolve and construct the provider by name.

    Raises `RemoteBackendUnavailable` if the provider is unknown or its
    dependencies are missing. The error message is the user-facing surface
    -- keep it actionable.
    """
    if name in _LOADERS:
        return _LOADERS[name](config)
    if ":" in name or "." in name:
        return _load_dotted_path_provider(name, config)
    known = ", ".join(sorted(_LOADERS)) or "(none)"
    raise RemoteBackendUnavailable(
        f"Unknown remote provider {name!r}. Known providers: {known}."
    )


def _load_dotted_path_provider(name: str, config: dict[str, Any]) -> SandboxProvider:
    if ":" in name:
        module_name, _, attr_name = name.partition(":")
    else:
        module_name, _, attr_name = name.rpartition(".")
    if not module_name or not attr_name:
        raise RemoteBackendUnavailable(
            f"Provider import path {name!r} must look like "
            "`pkg.module:ClassName` or `pkg.module.ClassName`."
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise RemoteBackendUnavailable(
            f"Could not import provider module {module_name!r} from {name!r}: {exc}"
        ) from exc
    try:
        provider_cls = getattr(module, attr_name)
    except AttributeError as exc:
        raise RemoteBackendUnavailable(
            f"Provider import path {name!r} resolved module {module_name!r} but "
            f"has no attribute {attr_name!r}."
        ) from exc
    try:
        provider = provider_cls(config)
    except Exception as exc:
        raise RemoteBackendUnavailable(
            f"Provider {name!r} could not be constructed: {exc}"
        ) from exc
    return provider


def known_providers() -> list[str]:
    """For diagnostics / `evo workspace status` output."""
    return sorted(_LOADERS)
